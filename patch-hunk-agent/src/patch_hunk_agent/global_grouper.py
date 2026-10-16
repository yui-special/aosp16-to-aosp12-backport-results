"""Global work-package partitioning (the primary grouping decision).

Empirical motivation: pairwise dependency judgement plus a transitive closure of
STRONG edges produced one 22-hunk package on a real 33-hunk patch
(over_grouping_rate 0.81 against the reviewed grouping), while the same model asked
once to divide the whole patch reached 0.07.  So the *global* decision belongs to
the model, and the graph machinery is only used to validate and repair it:

    hunks + evidence ─► model partition (1 call) ─► constraint repair ─► size guard
                     ─► validated partition with provenance for every change

The deterministic connected-component engine stays available as
``grouping.mode: components`` and as the fallback when the model output is unusable.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence_extractor import signature_changes, touched_functions
from .grouping_engine import GroupingConfig, effective_strengths
from .schemas import canonical_pair, hunk_sort_key, sort_hunk_ids
from .utils.logging_utils import get_logger

__all__ = ["GlobalGroupingConfig", "GlobalGrouper", "load_partition_prompt"]

LOGGER = get_logger("global_grouper")

DEFAULT_PROMPT_FILE = "global_partition.txt"


def load_partition_prompt(name: str = DEFAULT_PROMPT_FILE) -> str:
    path = Path(__file__).resolve().parents[2] / "prompts" / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:  # pragma: no cover - prompt ships with the repository
        LOGGER.warning("prompt %s missing; using a minimal built-in instruction", path)
        return (
            'Divide the hunks of this patch into work packages. Every hunk exactly once. '
            'Return JSON: {"groups": [{"name", "hunk_ids", "why"}]}.'
        )


@dataclass
class GlobalGroupingConfig:
    """Knobs from ``configs/default.yaml`` (``grouping`` section)."""

    mode: str = "global"
    max_group_size: int = 8
    max_split_rounds: int = 2
    max_partition_retries: int = 1
    # How many candidate partitions to generate per run; the most layer-coherent
    # one is kept (best-of-N), which is what makes the result stable.
    partition_candidates: int = 3
    digest_line_limit: int = 4
    include_conditional_edges: bool = True
    max_evidence_edges: int = 60
    prompt_file: str = DEFAULT_PROMPT_FILE

    @classmethod
    def from_dict(cls, data: dict | None) -> GlobalGroupingConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if hasattr(data, "__dataclass_fields__"):  # accept GroupingConfig directly
            payload = {
                field: getattr(data, field)
                for field in data.__dataclass_fields__  # type: ignore[attr-defined]
                if field in known
            }
            return cls(**payload)
        return cls(**{k: v for k, v in data.items() if k in known})


class GlobalGrouper:
    """Model-driven partitioning with deterministic validation and repair."""

    def __init__(
        self,
        llm: Any,
        *,
        config: GlobalGroupingConfig | dict | None = None,
        engine: Callable[..., dict] | None = None,
        recorder: Any | None = None,
        prompt: str | None = None,
    ) -> None:
        self.llm = llm
        self.config = (
            config
            if isinstance(config, GlobalGroupingConfig)
            else GlobalGroupingConfig.from_dict(config or {})
        )
        self.engine = engine
        self.recorder = recorder
        self.prompt = prompt or load_partition_prompt(self.config.prompt_file)

    # -- public API (same result shape as GroupingEngine.group) ------------ #

    def group(
        self,
        hunk_ids: list[str],
        dependencies: list[dict],
        *,
        resolutions: dict[str, str] | None = None,
        hunks: list[dict] | None = None,
        contexts: Any = None,
        patch_id: str | None = None,
        grouping_config: GroupingConfig | None = None,
    ) -> dict:
        ordered = sort_hunk_ids(hunk_ids)
        if not hunks:
            return self._fallback(ordered, dependencies, resolutions, "no hunk digests available")

        strengths, promotions, not_promoted = effective_strengths(
            dependencies, grouping_config or GroupingConfig(), resolutions
        )
        constraints = [
            record
            for record in dependencies
            if strengths.get(str(record.get("dependency_id"))) == "STRONG"
        ]
        hunk_map = {str(hunk.get("hunk_id")): hunk for hunk in hunks}
        context_map = _context_map(contexts)

        started = time.perf_counter()
        calls: list[dict] = []
        # Best-of-N with a deterministic, reference-free selector.  The observed bad
        # failure mode is a package mixing different layers (interface with a backend,
        # or a test with unrelated code), so candidates are ranked by how little layer
        # mixing they contain, then by size, then by session count.
        layer_of = {
            hunk_id: _hunk_digest(hunk, 0, None)["layer"] for hunk_id, hunk in hunk_map.items()
        }

        def pair_cost(left: str, right: str) -> int:
            """Cost of keeping two hunks from different layers in one package."""
            if left == right:
                return 0
            return 1

        # Two hunks that the dependency analysis tied together by STRONG evidence
        # (directly, or through a chain of STRONG edges) may legitimately live in one
        # package, so those pairs are not counted as "mixing".  Everything here is
        # derived from the patch and the code - no external grouping is consulted.
        protected = {frozenset((hunk, hunk)) for hunk in ordered}
        for record in constraints:
            left, right = record.get("source_hunk"), record.get("target_hunk")
            if left in layer_of and right in layer_of:
                protected.add(frozenset((left, right)))
        changed = True
        while changed:  # transitive closure over the STRONG edges
            changed = False
            for first in list(protected):
                for second in list(protected):
                    if first & second and (first | second) not in protected:
                        protected.add(first | second)
                        changed = True

        def coherence(groups: list[dict]) -> tuple:
            mixing = 0
            for group in groups:
                members = group["hunk_ids"]
                for position, left in enumerate(members):
                    for right in members[position + 1 :]:
                        if frozenset((left, right)) in protected:
                            continue
                        mixing += pair_cost(layer_of.get(left, "?"), layer_of.get(right, "?"))
            return (
                mixing,
                max((len(group["hunk_ids"]) for group in groups), default=0),
                len(groups),
            )

        candidates = max(1, int(getattr(self.config, "partition_candidates", 3)))
        best: tuple | None = None
        for attempt in range(1, candidates + 1):
            raw_groups, questions, calls = self._partition(
                ordered, hunks, constraints, dependencies, calls, context_map
            )
            if not raw_groups:
                continue
            candidate_groups, candidate_problems = self._normalise_groups(
                raw_groups, ordered, hunk_map
            )
            candidate_groups, _ = self._repair_constraints(
                candidate_groups, constraints, max(2, self.config.max_group_size)
            )
            covered = {hunk for group in candidate_groups for hunk in group["hunk_ids"]}
            if len(covered) != len(ordered):
                # An incomplete partition is not a valid candidate: dropping it also
                # keeps scripted/partial model answers from winning by accident.
                calls.append(
                    {
                        "stage": "candidate_rejected",
                        "candidate": attempt,
                        "covered": len(covered),
                        "expected": len(ordered),
                    }
                )
                continue
            value = coherence(candidate_groups)
            calls.append(
                {"stage": "candidate_scored", "candidate": attempt, "score": list(value)}
            )
            if best is None or value < best[0]:
                best = (value, raw_groups, candidate_problems, questions)
        if best is None:
            fallback = self._fallback(
                ordered, dependencies, resolutions, "no usable partition from any candidate"
            )
            fallback["unresolved_questions"] = list(fallback.get("unresolved_questions") or []) + questions
            return fallback
        _score, raw_groups, problems, questions = best
        groups, problems = self._normalise_groups(raw_groups, ordered, hunk_map)
        if not groups:
            return self._fallback(
                ordered, dependencies, resolutions, "; ".join(problems) or "empty partition"
            )

        repairs: list[dict] = []
        before = len(groups)
        limit = max(2, self.config.max_group_size)
        groups, deferred = self._repair_constraints(groups, constraints, limit)
        # Deterministic rule: a test-update hunk belongs with the package that holds
        # the API it exercises, not with other tests.  (The prompt asks for this but
        # observed runs kept merging the test hunks across responsibilities.)
        test_group_of = {}
        for group in groups:
            for hunk_id in group["hunk_ids"]:
                if layer_of.get(hunk_id) == "tests":
                    test_group_of[hunk_id] = group
        moved: list[dict] = []
        if not getattr(self.config, "test_hunk_follows_api", False):
            # Off by default: this rule was inferred from a reviewed grouping rather
            # than from the patch, so it must not influence the agent's own decision.
            test_group_of = {}
        for hunk_id, source_group in list(test_group_of.items()):
            partners: dict[int, int] = {}
            for record in dependencies:
                if str(record.get("strength")) in ("NONE",):
                    continue
                other = None
                if record.get("source_hunk") == hunk_id:
                    other = record.get("target_hunk")
                elif record.get("target_hunk") == hunk_id:
                    other = record.get("source_hunk")
                if not other:
                    continue
                for candidate in groups:
                    if other in candidate["hunk_ids"] and candidate is not source_group:
                        weight = 2 if str(record.get("strength")) == "STRONG" else 1
                        partners[id(candidate)] = partners.get(id(candidate), 0) + weight
            if not partners:
                # No usable dependency edge: fall back to symbol overlap, because a
                # test hunk normally touches the same symbols as the API it exercises.
                symbols = {
                    str(entry.get("name"))
                    for entry in (hunk_map.get(hunk_id) or {}).get("symbols") or []
                }
                if not symbols:
                    continue
                for candidate in groups:
                    if candidate is source_group:
                        continue
                    others = {
                        str(entry.get("name"))
                        for member in candidate["hunk_ids"]
                        for entry in (hunk_map.get(member) or {}).get("symbols") or []
                    }
                    shared = symbols & others
                    if shared:
                        partners[id(candidate)] = partners.get(id(candidate), 0) + len(shared)
                if not partners:
                    continue
            best_id = max(partners, key=lambda key: partners[key])
            target_group = next(group for group in groups if id(group) == best_id)
            if len(target_group["hunk_ids"]) + 1 > limit:
                deferred.append(
                    {
                        "dependency_id": None,
                        "category": "SHARED_STATE",
                        "strength": "CONDITIONAL",
                        "pair": [hunk_id, target_group["hunk_ids"][0]],
                        "groups": [source_group.get("name") or "", target_group.get("name") or ""],
                        "reason": "test hunk follows its API but the package is full",
                    }
                )
                continue
            source_group["hunk_ids"] = [h for h in source_group["hunk_ids"] if h != hunk_id]
            target_group["hunk_ids"] = [*target_group["hunk_ids"], hunk_id]
            moved.append(
                {
                    "hunk_id": hunk_id,
                    "from": source_group.get("name") or "",
                    "to": target_group.get("name") or "",
                    "reason": "test hunk follows the API it exercises",
                }
            )
        groups = [group for group in groups if group["hunk_ids"]]
        groups.sort(key=lambda group: [hunk_sort_key(h) for h in group["hunk_ids"]])
        if len(groups) < before:
            repairs.append(
                {
                    "reason": "strong dependency crossed model groups",
                    "merged_groups": before - len(groups),
                }
            )
        # Constraints that cannot be enforced inside the size budget are reported
        # instead of being merged into an unmanageable package.
        deferred_ids = [str(item.get("dependency_id")) for item in deferred]
        for item in deferred:
            questions.append(
                {
                    "question": (
                        f"{item.get('dependency_id')} ({item.get('category')}) links "
                        f"{item['pair'][0]} and {item['pair'][1]}, which the model placed in "
                        "different packages; merging them would exceed the package size limit"
                    ),
                    "hunk_ids": list(item["pair"]),
                    "reason": "deferred constraint: enforcing it would exceed max_group_size",
                    "needs_more_evidence": True,
                }
            )

        groups, split_questions, splits = self._guard_size(
            groups, constraints, hunk_map, calls
        )
        questions.extend(split_questions)
        groups = self._annotate(groups, constraints, dependencies)
        plan = _build_execution_plan(groups, constraints, deferred, hunk_map)
        satisfaction = _constraint_satisfaction(groups, constraints, deferred_ids)
        cross = [
            item
            for item in _cross_group_constraints(groups, constraints)
            if item["dependency_id"] not in deferred_ids
        ]
        if cross:
            questions.append(
                {
                    "question": (
                        "the partition still separates hunks the dependency analysis marked "
                        "as STRONG; it was reported instead of merging further"
                    ),
                    "hunk_ids": sort_hunk_ids(h for item in cross for h in item["pair"]),
                    "reason": "constraint repair could not merge without exceeding max_group_size",
                    "needs_more_evidence": True,
                }
            )

        metadata = {
            "grouping_mode": "global",
            "hunk_count": len(ordered),
            "group_count": len(groups),
            "group_sizes": [len(group["hunk_ids"]) for group in groups],
            "max_group_size": self.config.max_group_size,
            "constraint_edge_count": len(constraints),
            "promotions": promotions,
            "not_promoted": not_promoted,
            "partition_problems": problems,
            "repairs": repairs,
            "deferred_constraints": deferred,
            "execution_plan": plan,
            "constraint_satisfaction": satisfaction,
            "splits": splits,
            "model_calls": calls,
            "duration_s": round(time.perf_counter() - started, 6),
            "weak_dependencies_not_merged": [
                str(record.get("dependency_id"))
                for record in dependencies
                if str(strengths.get(str(record.get("dependency_id")))) in ("WEAK", "NONE")
            ],
        }
        if self.recorder is not None:
            self.recorder.record(
                "global_partition_done",
                patch_id=patch_id,
                groups=len(groups),
                splits=len(splits),
                max_size=max(metadata["group_sizes"] or [0]),
                duration_s=metadata["duration_s"],
            )
        return {
            "groups": groups,
            "cross_group_dependencies": cross,
            "unresolved_questions": questions,
            "metadata": metadata,
        }

    # -- model interaction ------------------------------------------------- #

    def _partition(
        self,
        ordered: list[str],
        hunks: list[dict],
        constraints: list[dict],
        dependencies: list[dict],
        calls: list[dict],
        context_map: dict[str, dict] | None = None,
    ) -> tuple[list[Any], list[dict], list[dict]]:
        payload = self._build_payload(ordered, hunks, constraints, dependencies, context_map)
        questions: list[dict] = []
        extra_messages: list[dict] | None = None
        attempts = max(1, self.config.max_partition_retries + 1)
        for attempt in range(1, attempts + 1):
            call = self.llm.analyze_dependencies(
                payload, extra_messages=extra_messages, temperature=0.0
            )
            calls.append(
                {
                    "stage": "global_partition",
                    "attempt": attempt,
                    "status": call.get("status"),
                    "error": call.get("error"),
                    "usage": call.get("usage"),
                    "latency_s": call.get("latency_s"),
                }
            )
            parsed = call.get("parsed") if isinstance(call.get("parsed"), dict) else None
            raw_groups = (parsed or {}).get("groups")
            if call.get("status") == "ok" and isinstance(raw_groups, list) and raw_groups:
                return (
                    raw_groups,
                    _normalise_questions(parsed.get("unresolved_questions"), ordered),
                    calls,
                )
            if attempt < attempts:
                extra_messages = [
                    {"role": "assistant", "content": (call.get("raw_text") or "")[:4000]},
                    {
                        "role": "user",
                        "content": (
                            "That answer was not usable. Reply with a single JSON object with a "
                            "'groups' array that covers every hunk id exactly once."
                        ),
                    },
                ]
        questions.append(
            {
                "question": "the model did not return a usable partition",
                "hunk_ids": [],
                "reason": "global partition failed",
                "needs_more_evidence": False,
            }
        )
        return [], questions, calls

    def _build_payload(
        self,
        ordered: list[str],
        hunks: list[dict],
        constraints: list[dict],
        dependencies: list[dict],
        context_map: dict[str, dict] | None = None,
    ) -> dict:
        by_id = {str(hunk.get("hunk_id")): hunk for hunk in hunks}
        digest = [
            _hunk_digest(
                by_id[hunk_id],
                self.config.digest_line_limit,
                (context_map or {}).get(hunk_id),
            )
            for hunk_id in ordered
            if hunk_id in by_id
        ]
        others = [
            record for record in dependencies if str(record.get("strength")) == "CONDITIONAL"
        ]
        return {
            "task": "global_work_package_partition",
            "instructions": [
                "Use only the hunk ids listed in hunks.",
                "Every hunk must appear in exactly one group.",
                f"Never put more than {self.config.max_group_size} hunks in one group.",
                "Cut at responsibility and layer boundaries; prefer the smallest safe group.",
                "List needed cross-package code in context_hunks instead of merging.",
                "target_status tells you what the TARGET version already contains for the symbols "
                "this hunk touches: 'present' means that prerequisite already exists there and does "
                "NOT by itself justify grouping a hunk with the one that defines it; 'absent' means "
                "the definition has to be ported together with its users; 'unknown' means it could "
                "not be checked.",
                "target_scope is the target-side method the hunk is expected to land in. Two hunks "
                "with the SAME target file AND target_scope will edit the same place: prefer keeping "
                "them together or listing one in context_hunks, and never assume they are independent.",
            ],
            "output_schema": {
                "groups": [
                    {
                        "name": "short title",
                        "hunk_ids": ["H1", "H2"],
                        "why": "why they cannot be ported apart",
                        "independent_verification": "how to verify this group alone",
                        "context_hunks": ["H9"],
                    }
                ],
                "unresolved_questions": [{"question": "...", "hunk_ids": ["H3"], "reason": "..."}],
            },
            "hunks": digest,
            "strong_dependency_evidence": [
                _edge_view(record) for record in _bounded(constraints, self.config.max_evidence_edges)
            ],
            "conditional_dependency_evidence": (
                [_edge_view(record) for record in _bounded(others, self.config.max_evidence_edges)]
                if self.config.include_conditional_edges
                else []
            ),
            "notes": [
                "strong_dependency_evidence lists pairs the earlier analysis believes must "
                "change together; treat it as evidence, not as an instruction to merge "
                "everything transitively",
                "the target version code is not available in this run",
            ],
        }

    # -- validation / repair ---------------------------------------------- #

    def _normalise_groups(
        self, raw_groups: Iterable[Any], ordered: list[str], hunk_map: dict[str, dict]
    ) -> tuple[list[dict], list[str]]:
        known = set(ordered)
        problems: list[str] = []
        seen: set[str] = set()
        groups: list[dict] = []
        for index, raw in enumerate(raw_groups or []):
            if not isinstance(raw, dict):
                problems.append(f"group {index} is not an object")
                continue
            members: list[str] = []
            for hunk_id in raw.get("hunk_ids") or []:
                text = str(hunk_id)
                if text not in known:
                    problems.append(f"unknown hunk id {text!r} in group {index}")
                    continue
                if text in seen:
                    problems.append(f"{text} appears in more than one group")
                    continue
                seen.add(text)
                members.append(text)
            if not members:
                problems.append(f"group {index} has no usable hunks")
                continue
            groups.append(
                {
                    "hunk_ids": sorted(members, key=hunk_sort_key),
                    "name": str(raw.get("name") or "").strip(),
                    "group_reason": str(raw.get("why") or "").strip(),
                    "independent_verification": str(
                        raw.get("independent_verification") or ""
                    ).strip(),
                    "context_hunks": [
                        str(item) for item in raw.get("context_hunks") or [] if str(item) in known
                    ],
                }
            )
        for hunk_id in ordered:
            if hunk_id in seen:
                continue
            problems.append(f"{hunk_id} was not assigned by the model; kept as its own group")
            hunk = hunk_map.get(hunk_id) or {}
            section = str(hunk.get("section") or "")
            groups.append(
                {
                    "hunk_ids": [hunk_id],
                    "name": f"{_basename(hunk.get('file_path'))} :: {section or 'single hunk'}",
                    "group_reason": "The model's partition did not cover this hunk.",
                    "independent_verification": "",
                    "context_hunks": [],
                }
            )
        groups.sort(key=lambda group: [hunk_sort_key(h) for h in group["hunk_ids"]])
        return groups, problems

    def _repair_constraints(
        self, groups: list[dict], constraints: list[dict], max_size: int = 0
    ) -> tuple[list[dict], list[dict]]:
        """Merge the two groups a STRONG edge directly connects, within a size budget.

        A merge that would exceed ``max_size`` is refused and returned as a deferred
        constraint, so a dense chain of strong edges can no longer collapse the whole
        patch into one package (observed: 11 model packages merged down to 1).
        """
        deferred: list[dict] = []
        for _ in range(max(1, len(constraints))):
            group_of = {hunk: group for group in groups for hunk in group["hunk_ids"]}
            crossing = [
                record
                for record in constraints
                if record.get("source_hunk") in group_of
                and record.get("target_hunk") in group_of
                and group_of[record["source_hunk"]] is not group_of[record["target_hunk"]]
            ]
            if not crossing:
                break
            mergeable = None
            for record in crossing:
                left = group_of[record["source_hunk"]]
                right = group_of[record["target_hunk"]]
                if max_size and len(left["hunk_ids"]) + len(right["hunk_ids"]) > max_size:
                    continue
                mergeable = record
                break
            if mergeable is None:
                for record in crossing:
                    left = group_of[record["source_hunk"]]
                    right = group_of[record["target_hunk"]]
                    deferred.append(
                        {
                            "dependency_id": record.get("dependency_id"),
                            "category": record.get("category"),
                            "strength": record.get("strength"),
                            "pair": list(
                                canonical_pair(record.get("source_hunk"), record.get("target_hunk"))
                            ),
                            "groups": [left.get("name") or "", right.get("name") or ""],
                            "reason": "merge would exceed max_group_size",
                        }
                    )
                break
            record = mergeable
            left = group_of[record["source_hunk"]]
            right = group_of[record["target_hunk"]]
            merged = _merge_two(left, right)
            merged["group_reason"] = (
                f"{str(merged.get('group_reason') or '').rstrip()} Merged because "
                f"{record.get('dependency_id')} ({record.get('category')}) links "
                f"{record.get('source_hunk')} and {record.get('target_hunk')}."
            ).strip()
            merged["_merged"] = True
            groups = [group for group in groups if group is not left and group is not right]
            groups.append(merged)
            groups.sort(key=lambda group: [hunk_sort_key(h) for h in group["hunk_ids"]])
        return groups, deferred

    def _guard_size(
        self,
        groups: list[dict],
        constraints: list[dict],
        hunk_map: dict[str, dict],
        calls: list[dict],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        questions: list[dict] = []
        splits: list[dict] = []
        limit = max(2, self.config.max_group_size)
        for _ in range(max(0, self.config.max_split_rounds)):
            oversized = [group for group in groups if len(group["hunk_ids"]) > limit]
            if not oversized:
                break
            target = max(oversized, key=lambda group: len(group["hunk_ids"]))
            pieces = self._ask_split(target, constraints, hunk_map, calls)
            if not pieces:
                break
            splits.append({"group": list(target["hunk_ids"]), "into": [p["hunk_ids"] for p in pieces]})
            groups = [group for group in groups if group is not target] + pieces
            groups.sort(key=lambda group: [hunk_sort_key(h) for h in group["hunk_ids"]])
        for group in [group for group in groups if len(group["hunk_ids"]) > limit]:
            questions.append(
                {
                    "question": (
                        f"a package still has {len(group['hunk_ids'])} hunks (limit {limit}); "
                        "its STRONG dependencies forbid splitting it further"
                    ),
                    "hunk_ids": list(group["hunk_ids"]),
                    "reason": "size guard could not split without breaking a constraint",
                    "needs_more_evidence": True,
                }
            )
        return groups, questions, splits

    def _ask_split(
        self,
        group: dict,
        constraints: list[dict],
        hunk_map: dict[str, dict],
        calls: list[dict],
    ) -> list[dict]:
        members = list(group["hunk_ids"])
        payload = {
            "task": "split_one_oversized_group",
            "instructions": [
                f"Split these {len(members)} hunks into 2 or more groups of at most "
                f"{max(2, self.config.max_group_size)} hunks each.",
                "Every hunk must appear exactly once.",
                "Hunks linked by strong_dependency_evidence must stay in the same group.",
                "Cut at responsibility boundaries.",
            ],
            "output_schema": {"groups": [{"name": "short title", "hunk_ids": ["H1"], "why": "..."}]},
            "hunks": [
                _hunk_digest(hunk_map[hunk_id], self.config.digest_line_limit)
                for hunk_id in members
                if hunk_id in hunk_map
            ],
            "strong_dependency_evidence": [
                _edge_view(record)
                for record in constraints
                if record.get("source_hunk") in members and record.get("target_hunk") in members
            ],
        }
        call = self.llm.analyze_dependencies(payload, temperature=0.0)
        calls.append(
            {
                "stage": "split_oversized_group",
                "group_size": len(members),
                "status": call.get("status"),
                "usage": call.get("usage"),
                "latency_s": call.get("latency_s"),
            }
        )
        parsed = call.get("parsed") if isinstance(call.get("parsed"), dict) else None
        raw_groups = (parsed or {}).get("groups")
        if call.get("status") != "ok" or not isinstance(raw_groups, list) or len(raw_groups) < 2:
            return []
        pieces, _problems = self._normalise_groups(raw_groups, members, hunk_map)
        pieces = [piece for piece in pieces if len(piece["hunk_ids"]) < len(members)]
        if len(pieces) < 2:
            return []
        pieces, _deferred = self._repair_constraints(
            pieces, constraints, max(2, self.config.max_group_size)
        )
        if len(pieces) == 1 and len(pieces[0]["hunk_ids"]) == len(members):
            return []
        for piece in pieces:
            piece["split_from"] = group.get("name") or ""
        return pieces

    # -- output shaping ---------------------------------------------------- #

    def _annotate(
        self, groups: list[dict], constraints: list[dict], dependencies: list[dict]
    ) -> list[dict]:
        annotated: list[dict] = []
        for position, group in enumerate(groups, start=1):
            members = set(group["hunk_ids"])
            relevant = [
                str(record.get("dependency_id"))
                for record in constraints
                if record.get("source_hunk") in members and record.get("target_hunk") in members
            ]
            internal_other = [
                str(record.get("dependency_id"))
                for record in dependencies
                if str(record.get("dependency_id")) not in relevant
                and record.get("source_hunk") in members
                and record.get("target_hunk") in members
                and str(record.get("strength")) != "NONE"
            ]
            external = [
                str(record.get("dependency_id"))
                for record in dependencies
                if str(record.get("strength")) != "NONE"
                and (record.get("source_hunk") in members)
                != (record.get("target_hunk") in members)
            ]
            item = {
                "group_id": f"G{position}",
                "hunk_ids": list(group["hunk_ids"]),
                "group_reason": group.get("group_reason") or "",
                "name": group.get("name") or "",
                "independent_verification": group.get("independent_verification") or "",
                "context_hunks": sorted(set(group.get("context_hunks") or []), key=hunk_sort_key),
                "dependency_ids": sorted(relevant, key=_dependency_sort_key),
                "other_dependency_ids": sorted(internal_other, key=_dependency_sort_key),
                "external_dependency_ids": sorted(external, key=_dependency_sort_key),
            }
            if group.get("split_from"):
                item["split_from"] = group["split_from"]
            annotated.append(item)
        return annotated

    def _fallback(
        self,
        ordered: list[str],
        dependencies: list[dict],
        resolutions: dict[str, str] | None,
        reason: str,
    ) -> dict:
        LOGGER.warning("global partition unavailable (%s); using the components engine", reason)
        if self.engine is not None:
            result = self.engine(ordered, dependencies, resolutions=resolutions)
        else:  # pragma: no cover - the pipeline always injects the engine
            result = {
                "groups": [
                    {"group_id": f"G{i}", "hunk_ids": [hunk_id]}
                    for i, hunk_id in enumerate(ordered, start=1)
                ]
            }
        result = dict(result)
        metadata = dict(result.get("metadata") or {})
        metadata.update({"grouping_mode": "components_fallback", "fallback_reason": reason})
        result["metadata"] = metadata
        questions = list(result.get("unresolved_questions") or [])
        questions.append(
            {
                "question": "the model partition was unusable, so the deterministic "
                "connected-component grouping was used instead",
                "hunk_ids": [],
                "reason": reason,
                "needs_more_evidence": False,
            }
        )
        result["unresolved_questions"] = questions
        return result


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _hunk_digest(hunk: dict, line_limit: int, context: dict | None = None) -> dict:
    digest = {
        "hunk_id": hunk.get("hunk_id"),
        "file": hunk.get("file_path"),
        "scope": str(hunk.get("section") or "") or None,
        "touched_functions": touched_functions(hunk),
        "added_lines": list(hunk.get("added_lines") or [])[:line_limit],
        "removed_lines": list(hunk.get("removed_lines") or [])[:line_limit],
        "signature_changes": [
            {"name": change["name"], "before": change["before"], "after": change["after"]}
            for change in signature_changes(hunk)[:2]
        ],
    }
    if context:
        # What the target version already has, and where this hunk lands there.
        digest["target_status"] = {
            str(key): str(value)
            for key, value in (context.get("target_symbol_status") or {}).items()
        }
        digest["target_scope"] = {
            "file": context.get("target_file_path"),
            "method": (context.get("target_context") or {}).get("scope_function"),
            "retrieval": context.get("retrieval_status"),
        }
        definitions = context.get("target_definition_text") or {}
        if definitions:
            digest["target_definition_text"] = {
                str(key): str(value)[:160] for key, value in list(definitions.items())[:3]
            }
    if len(hunk.get("added_lines") or []) > line_limit:
        digest["added_lines_truncated"] = True
    # Module key: a cheap, deterministic, language-agnostic ownership hint built
    # only from the path shape (top-level directories, extension family, test
    # folders).  Two hunks in different modules are usually different
    # responsibilities, which is exactly where the model used to guess.
    path = str(hunk.get("file_path") or "")
    segments = [part for part in path.split("/") if part and part != "."]
    directories = segments[:-1]
    stem = ""
    if segments:
        name = segments[-1]
        stem = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    family = {
        "c": "c", "h": "c",
        "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp",
        "java": "jvm", "kt": "jvm", "kts": "jvm", "scala": "jvm", "groovy": "jvm",
        "py": "python", "pyi": "python",
        "js": "js", "jsx": "js", "ts": "js", "tsx": "js",
        "go": "go", "rs": "rust", "cs": "dotnet", "swift": "swift", "rb": "ruby",
        "php": "php", "sh": "shell", "sql": "sql",
    }.get(stem, stem or "noext")
    is_test = any(part.lower() in ("test", "tests", "testing", "spec", "specs") for part in directories)
    module_root = "/".join(directories[:2]) or "."
    digest["layer"] = f"{module_root}|{family}" + ("|tests" if is_test else "")
    return digest


def _edge_view(record: dict) -> dict:
    return {
        "id": record.get("dependency_id"),
        "pair": list(canonical_pair(record.get("source_hunk"), record.get("target_hunk"))),
        "category": record.get("category"),
        "strength": record.get("strength"),
        "symbols": _symbols(record),
        "why": str(record.get("reasoning_summary") or record.get("separation_risk") or "")[:240],
    }


def _symbols(record: dict) -> list[str]:
    symbols: list[str] = []
    for item in record.get("evidence") or []:
        symbol = str(item.get("symbol") or "").strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols[:4]


def _merge_two(left: dict, right: dict) -> dict:
    merged = dict(left)
    merged["hunk_ids"] = sort_hunk_ids(list(left["hunk_ids"]) + list(right["hunk_ids"]))
    names = [name for name in (left.get("name"), right.get("name")) if name]
    merged["name"] = " + ".join(names) if names else (left.get("name") or "")
    merged["context_hunks"] = sort_hunk_ids(
        list(set(left.get("context_hunks") or []) | set(right.get("context_hunks") or []))
    )
    return merged


def _anchors_of(hunk: dict) -> list[tuple[str, str]]:
    """Source-side anchor proxies: method level when known, file level always."""
    file_path = str(hunk.get("file_path") or "")
    if not file_path:
        return []
    section = str(hunk.get("section") or "").strip()
    name = ""
    if section:
        match = re.search(r"([A-Za-z_]\w*)\s*\(", section) or re.search(r"([A-Za-z_]\w*)", section)
        name = match.group(1) if match else ""
    anchors: list[tuple[str, str]] = [(file_path, name)] if name else []
    # The file-level anchor always applies: overlapping files are a weaker but real
    # signal (the reviewed grouping in 48649 used file/module boundaries too).
    anchors.append((file_path, ""))
    return anchors


def _topological_order(
    group_ids: list[str], edges: list[tuple[str, str]], groups: list[dict]
) -> tuple[list[str], list[str]]:
    """Stable topological order; returns ``(order, groups_left_in_a_cycle)``."""
    rank = {
        group["group_id"]: min(hunk_sort_key(h) for h in group["hunk_ids"])
        for group in groups
    }
    outgoing: dict[str, list[str]] = {gid: [] for gid in group_ids}
    indegree: dict[str, int] = dict.fromkeys(group_ids, 0)
    for left, right in edges:
        if left == right or left not in outgoing or right not in indegree:
            continue
        outgoing[left].append(right)
        indegree[right] += 1
    ready = sorted([gid for gid in group_ids if indegree[gid] == 0], key=lambda gid: rank[gid])
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in sorted(outgoing[current], key=lambda gid: rank[gid]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda gid: rank[gid])
    leftover = sorted([gid for gid in group_ids if gid not in order], key=lambda gid: rank[gid])
    return order + leftover, leftover


def _build_execution_plan(
    groups: list[dict],
    constraints: list[dict],
    deferred: list[dict],
    hunk_map: dict[str, dict],
    *,
    target_available: bool = False,
) -> dict:
    """Turn the partition into something the downstream scheduler can act on.

    Without target-version code the anchors are a *source-side proxy* (same file,
    same enclosing function); they are always labelled as such so nobody mistakes
    them for confirmed target locations.
    """
    group_of = {hunk: group["group_id"] for group in groups for hunk in group["hunk_ids"]}
    anchors: dict[tuple[str, str], list[str]] = {}
    for group in groups:
        for hunk_id in group["hunk_ids"]:
            hunk = hunk_map.get(hunk_id)
            if not hunk:
                continue
            for anchor in _anchors_of(hunk):
                anchors.setdefault(anchor, []).append(group["group_id"])

    shared: list[dict] = []
    for (file_path, name), group_ids in sorted(anchors.items()):
        unique = sorted(set(group_ids))
        if len(unique) < 2:
            continue
        shared.append(
            {
                "file": file_path,
                "method": name or None,
                "level": "method" if name else "file",
                "group_ids": unique,
                "evidence": "source_side_proxy",
                "requires_target_confirmation": not target_available,
            }
        )

    precedence: list[tuple[str, str]] = []
    for record in list(constraints) + list(deferred):
        left = record.get("source_hunk")
        right = record.get("target_hunk")
        if left not in group_of or right not in group_of:
            continue
        if group_of[left] == group_of[right]:
            continue
        if str(record.get("category")) not in ("DEFINITION_USE", "INTERFACE_SIGNATURE"):
            continue
        if str(record.get("target_version_status") or "unknown") == "present":
            continue
        # The declaring side goes first so the callee exists when callers change.
        precedence.append((group_of[left], group_of[right]))

    order, cycles = _topological_order([group["group_id"] for group in groups], precedence, groups)
    directives: dict[str, list[str]] = {
        gid: [f"PORT_ORDER {position}/{len(order)}"]
        for position, gid in enumerate(order, start=1)
    }
    for item in shared:
        others = ", ".join(gid for gid in item["group_ids"])
        for gid in item["group_ids"]:
            directives[gid].append(
                f"SHARED_TARGET_{item['level'].upper()} {item['file']}"
                + (f"#{item['method']}" if item["method"] else "")
                + f" is also touched by {others}: re-read the current file state before editing "
                "and keep the signature/parameter contract consistent with what those groups applied."
            )
    joint: list[dict] = [
        {
            "groups": item["group_ids"],
            "kind": "shared_target_region",
            "file": item["file"],
            "method": item["method"],
            "reason": "packages edit the same target anchor; verify them together after both are applied",
        }
        for item in shared
    ]
    for item in deferred:
        pair = list(item.get("pair") or [])
        gids = sorted({group_of[h] for h in pair if h in group_of})
        if len(gids) < 2:
            continue
        joint.append(
            {
                "groups": gids,
                "kind": "deferred_constraint",
                "dependency_id": item.get("dependency_id"),
                "category": item.get("category"),
                "reason": item.get("reason") or "merge would exceed max_group_size",
            }
        )
        for gid in gids:
            directives[gid].append(
                f"DEFERRED_CONSTRAINT {item.get('dependency_id')} ({item.get('category')}) links "
                f"{pair[0]} and {pair[1]} across {', '.join(gids)}: after those packages are applied, "
                "verify them together (compile and test) instead of assuming independence."
            )
    if cycles:
        directives.setdefault(cycles[0], []).append(
            "PRECEDENCE_CYCLE among " + ", ".join(cycles) + ": no valid order exists, so these "
            "packages must be applied with shared context and verified jointly."
        )
    return {
        "order": order,
        "shared_target_regions": shared,
        "joint_verification": joint,
        "directives": directives,
        "precedence_cycles": cycles,
        "anchor_evidence": "source_side_proxy",
        "requires_target_confirmation": not target_available,
    }


def _constraint_satisfaction(
    groups: list[dict], constraints: list[dict], deferred_ids: list[str]
) -> dict:
    """How many coupling constraints the partition actually honours."""
    group_of = {hunk: group["group_id"] for group in groups for hunk in group["hunk_ids"]}
    deferred = set(deferred_ids)
    inside = deferred_count = violated = dissolved = 0
    for record in constraints:
        left = record.get("source_hunk")
        right = record.get("target_hunk")
        if left not in group_of or right not in group_of:
            continue
        if str(record.get("target_version_status") or "") == "present":
            dissolved += 1
            continue
        if group_of[left] == group_of[right]:
            inside += 1
        elif str(record.get("dependency_id")) in deferred:
            deferred_count += 1
        else:
            violated += 1
    active = inside + deferred_count + violated
    return {
        "total_constraints": len(constraints),
        "dissolved_by_target_version": dissolved,
        "active_constraints": active,
        "enforced_in_package": inside,
        "deferred_reported": deferred_count,
        "violated": violated,
        "satisfaction_rate": round(inside / active, 6) if active else 1.0,
        "note": (
            "dissolved_by_target_version counts constraints whose prerequisite already exists in "
            "the target version; without target code every constraint stays 'unknown' and is kept active"
        ),
    }


def _cross_group_constraints(groups: list[dict], constraints: list[dict]) -> list[dict]:
    group_of = {hunk: group["group_id"] for group in groups for hunk in group["hunk_ids"]}
    out: list[dict] = []
    for record in constraints:
        left = group_of.get(record.get("source_hunk"))
        right = group_of.get(record.get("target_hunk"))
        if left and right and left != right:
            out.append(
                {
                    "dependency_id": record.get("dependency_id"),
                    "pair": list(
                        canonical_pair(record.get("source_hunk"), record.get("target_hunk"))
                    ),
                    "category": record.get("category"),
                    "groups": [left, right],
                    "reason": "STRONG dependencies must live inside a single group",
                }
            )
    return out


def _normalise_questions(raw: Any, ordered: list[str]) -> list[dict]:
    known = set(ordered)
    out: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        if not question:
            continue
        out.append(
            {
                "question": question,
                "hunk_ids": sort_hunk_ids(
                    str(hunk) for hunk in item.get("hunk_ids") or [] if str(hunk) in known
                ),
                "reason": str(item.get("reason") or ""),
                "needs_more_evidence": bool(item.get("needs_more_evidence", True)),
            }
        )
    return out


def _bounded(items: list[dict], limit: int) -> list[dict]:
    return items[: max(0, limit)] if limit else items


def _dependency_sort_key(dependency_id: str) -> int:
    text = str(dependency_id)
    return int(text[1:]) if text[1:].isdigit() else 1 << 30


def _basename(path: Any) -> str:
    text = str(path or "")
    return text.rsplit("/", 1)[-1] if text else "unknown"


def _context_map(contexts: Any) -> dict[str, dict]:
    if not contexts:
        return {}
    if isinstance(contexts, dict):
        if "hunk_id" in contexts:
            return {str(contexts["hunk_id"]): contexts}
        return {str(key): value for key, value in contexts.items()}
    return {str(item.get("hunk_id")): item for item in contexts if item.get("hunk_id")}
