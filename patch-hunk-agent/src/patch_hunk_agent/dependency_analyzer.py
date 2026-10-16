"""LLM-backed cross-hunk dependency analysis (sections 7 and 12.4)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from collections.abc import Iterable

from . import ANALYSIS_VERSION, AnalysisError
from .evidence_extractor import extract, enrich_hunks, summarize_hunk
from .schemas import (
    STRENGTH_ORDER,
    check_dependency,
    hunk_sort_key,
    new_analysis_result,
    normalize_dependency,
    sort_hunk_ids,
)
from .utils.logging_utils import get_logger

__all__ = ["AnalyzerConfig", "DependencyAnalyzer", "OUTPUT_SCHEMA", "COMPACT_OUTPUT_SCHEMA"]

LOGGER = get_logger("analyzer")

REPAIR_INSTRUCTION = (
    "The previous answer could not be used. It must be a single JSON object with a "
    "'dependencies' array and an 'unresolved_questions' array, and every dependency must "
    "use hunk ids that exist in this patch, one of the documented categories and "
    "strengths, and non-empty evidence. Reply with corrected JSON only."
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["dependencies"],
    "properties": {
        "status": {"enum": ["complete", "partial", "failed"]},
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "source_hunk",
                    "target_hunk",
                    "category",
                    "strength",
                    "evidence",
                ],
                "properties": {
                    "source_hunk": {"type": "string", "example": "H1"},
                    "target_hunk": {"type": "string", "example": "H2"},
                    "category": {
                        "enum": [
                            "DEFINITION_USE",
                            "INTERFACE_SIGNATURE",
                            "CALL_GRAPH",
                            "SHARED_STATE",
                            "CONTROL_FLOW",
                            "SEMANTIC_INVARIANT",
                            "CONTEXT_DEPENDENCY",
                        ]
                    },
                    "strength": {"enum": ["STRONG", "CONDITIONAL", "WEAK", "NONE"]},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "symbol": {"type": "string"},
                                "line": {"type": ["integer", "null"]},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "separation_risk": {"type": "string"},
                    "target_version_status": {
                        "enum": ["present", "absent", "partially_present", "unknown"]
                    },
                    "reasoning_summary": {"type": "string"},
                    "needs_more_evidence": {"type": "boolean"},
                },
            },
        },
        "unresolved_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "hunk_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "needs_more_evidence": {"type": "boolean"},
                    "suggested_query": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "side": {"enum": ["source", "target"]},
                            "file_path": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}

# The per-call copy of the answer contract: the full schema lives in the system
# prompt, so repeating it in every payload only burns tokens.
COMPACT_OUTPUT_SCHEMA: dict[str, Any] = {
    "dependencies": [
        {
            "source_hunk": "H1",
            "target_hunk": "H2",
            "category": "DEFINITION_USE|INTERFACE_SIGNATURE|CALL_GRAPH|SHARED_STATE|"
            "CONTROL_FLOW|SEMANTIC_INVARIANT|CONTEXT_DEPENDENCY",
            "strength": "STRONG|CONDITIONAL|WEAK|NONE",
            "evidence": [{"file_path": "...", "symbol": "...", "line": 1, "description": "..."}],
            "separation_risk": "...",
            "target_version_status": "present|absent|partially_present|unknown",
            "reasoning_summary": "...",
            "needs_more_evidence": False,
        }
    ],
    "unresolved_questions": [{"question": "...", "hunk_ids": ["H1"], "reason": "..."}],
}

PAYLOAD_INSTRUCTIONS = [
    "Judge only the candidate pairs listed in candidate_pairs; do not invent pairs.",
    "Each candidate carries machine signals and machine_evidence. Those are hypotheses, not proof.",
    "Emit one dependency record per candidate pair that has a real dependency. It is valid to "
    "emit no record for a pair, or a record with strength NONE, when no meaningful dependency "
    "is established.",
    "Only STRONG forces the two hunks into one group. Reserve it for a concrete need for "
    "coordinated handling supported by the evidence and by the target version state.",
    "Use CONDITIONAL when the need for grouping depends on missing context or an unresolved "
    "target-version state.",
    "target_version_status must describe the target version, not the source patch: present, "
    "absent, partially_present or unknown.",
    "Set needs_more_evidence to true and ask a targeted question when a specific lookup would "
    "settle the judgement.",
    "Every evidence entry must point at something locatable (symbol, file path with line, or a "
    "quoted line).",
]


@dataclass
class AnalyzerConfig:
    """Knobs from ``configs/default.yaml`` (``analyzer`` section)."""

    temperature: float = 0.0
    max_tokens: int = 4096
    max_format_retries: int = 2
    max_evidence_rounds: int = 1
    max_pairs_per_call: int = 8
    max_candidate_pairs: int = 200
    include_no_signal_pairs: bool = True
    log_raw_responses: bool = True
    code_context_chars: int = 1600
    # Cost knobs: the full output schema is already in the system prompt, so the
    # per-call copy can be compact, and unavailable context adds tokens without
    # adding information.
    compact_schema: bool = True
    include_unavailable_context: bool = False
    max_payload_chars: int = 120000

    @classmethod
    def from_dict(cls, data: dict | None) -> AnalyzerConfig:
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class DependencyAnalyzer:
    """Orchestrates the LLM analysis and validates everything it returns."""

    def __init__(
        self,
        llm: Any,
        *,
        config: AnalyzerConfig | dict | None = None,
        context_provider: Any | None = None,
        recorder: Any | None = None,
        evidence: Iterable[dict] | None = None,
    ) -> None:
        self.llm = llm
        self.config = (
            config if isinstance(config, AnalyzerConfig) else AnalyzerConfig.from_dict(config or {})
        )
        self.context_provider = context_provider
        self.recorder = recorder
        self._reusable_evidence = list(evidence or [])

    # -- public API -------------------------------------------------------- #

    def analyze(
        self,
        hunks: list[dict],
        evidence: list[dict] | None = None,
        *,
        patch_id: str | None = None,
        source_revision: str | None = None,
        target_revision: str | None = None,
        contexts: list[dict] | dict | None = None,
        existing_analysis: list[dict] | None = None,
    ) -> dict:
        started = time.perf_counter()
        if not hunks:
            raise AnalysisError("cannot analyse an empty hunk list")

        prepared = enrich_hunks(hunks)
        hunk_ids = sort_hunk_ids(hunk["hunk_id"] for hunk in prepared)
        if len(hunk_ids) != len(prepared):
            raise AnalysisError("hunk ids must be unique")
        context_map = self._context_map(contexts)
        if evidence is None:
            # Omitting the argument means "derive the candidates from the hunks";
            # passing an empty list means "there are no candidates to analyse".
            candidates = list(self._reusable_evidence or extract(prepared, contexts))
        else:
            candidates = list(evidence)

        rejected: list[dict] = []
        questions: list[dict] = []
        call_records: list[dict] = []
        records: list[dict] = []
        batches = 0

        seeded, seed_rejected = self._seed_existing_analysis(existing_analysis, hunk_ids)
        records.extend(seeded)
        rejected.extend(seed_rejected)

        selected, skipped = self._select_candidates(candidates)
        if skipped:
            questions.append(
                {
                    "question": (
                        f"{len(skipped)} low-signal hunk pair(s) were not sent to the model "
                        "because of the max_candidate_pairs budget"
                    ),
                    "hunk_ids": sort_hunk_ids(
                        hunk_id for pair in skipped for hunk_id in pair.get("pair", [])
                    ),
                    "reason": "candidate budget exhausted",
                    "needs_more_evidence": True,
                    "suggested_query": {"symbol": "", "side": "target"},
                }
            )

        target_revision_label = target_revision
        for batch in self._batch(selected):
            batches += 1
            payload = self._build_payload(
                patch_id=patch_id,
                hunks=prepared,
                pairs=batch,
                context_map=context_map,
                source_revision=source_revision,
                target_revision=target_revision_label,
                existing_analysis=existing_analysis,
            )
            batch_records, batch_questions, batch_rejected, calls = self._run_batch(
                payload, hunk_ids, batch_label=f"batch{batches}"
            )
            records.extend(batch_records)
            questions.extend(batch_questions)
            rejected.extend(batch_rejected)
            call_records.extend(calls)

        if self.config.max_evidence_rounds > 0 and self.context_provider is not None:
            extra_records, extra_questions, extra_calls, extra_rejected = self._evidence_round(
                records=records,
                hunks=prepared,
                hunk_ids=hunk_ids,
                patch_id=patch_id,
                source_revision=source_revision,
                target_revision=target_revision_label,
                context_map=context_map,
                candidates=selected,
            )
            if extra_records or extra_questions or extra_calls:
                records = extra_records
                questions.extend(extra_questions)
                rejected.extend(extra_rejected)
                call_records.extend(extra_calls)

        normalised, dedupe_notes = self._normalise_records(records, hunk_ids)
        rejected.extend(dedupe_notes)
        status = self._status(batches, call_records, questions, rejected, normalised)

        metadata = {
            "analysis_version": ANALYSIS_VERSION,
            "model": getattr(self.llm, "model", None),
            "provider": getattr(self.llm, "name", None),
            "candidate_pair_count": len(candidates),
            "analysed_pair_count": len(selected),
            "skipped_pair_count": len(skipped),
            "batch_count": batches,
            "duration_s": round(time.perf_counter() - started, 6),
            "usage": self.llm.total_usage() if hasattr(self.llm, "total_usage") else {},
            "source_revision": source_revision,
            "target_revision": target_revision,
        }
        result = new_analysis_result(
            patch_id or "patch_001",
            hunk_ids,
            normalised,
            status=status,
            unresolved_questions=questions,
            metadata=metadata,
            rejected_records=rejected,
            call_records=call_records,
        )
        if self.recorder is not None:
            self.recorder.record(
                "dependency_analysis_done",
                patch_id=result["patch_id"],
                status=status,
                dependency_count=len(normalised),
                unresolved_count=len(questions),
                rejected_count=len(rejected),
                duration_s=metadata["duration_s"],
                usage=metadata["usage"],
            )
        return result

    # -- payload assembly -------------------------------------------------- #

    @staticmethod
    def _context_map(contexts: Any) -> dict[str, dict]:
        if not contexts:
            return {}
        if isinstance(contexts, dict):
            if "hunk_id" in contexts:
                return {str(contexts["hunk_id"]): contexts}
            return {str(key): value for key, value in contexts.items()}
        return {str(item.get("hunk_id")): item for item in contexts if item.get("hunk_id")}

    def _build_payload(
        self,
        *,
        patch_id: str | None,
        hunks: list[dict],
        pairs: list[dict],
        context_map: dict[str, dict],
        source_revision: str | None,
        target_revision: str | None,
        existing_analysis: list[dict] | None,
        additional_evidence: dict | None = None,
    ) -> dict:
        involved: list[str] = []
        for pair in pairs:
            for hunk_id in pair.get("pair", []):
                if hunk_id not in involved:
                    involved.append(hunk_id)
        involved = sorted(involved, key=hunk_sort_key)

        by_id = {hunk["hunk_id"]: hunk for hunk in hunks}
        hunk_summaries = [
            summarize_hunk(by_id[hunk_id], context_map.get(hunk_id))
            for hunk_id in involved
            if hunk_id in by_id
        ]
        code_context = {}
        for hunk_id in involved:
            record = context_map.get(hunk_id)
            if not record:
                continue
            if (
                not self.config.include_unavailable_context
                and record.get("retrieval_status") == "unavailable"
            ):
                code_context[hunk_id] = {
                    "retrieval_status": "unavailable",
                    "target_symbol_status": {},
                    "note": "no repository access in this run; judge from the patch text",
                }
                continue
            code_context[hunk_id] = {
                "retrieval_status": record.get("retrieval_status"),
                "source_revision": record.get("source_revision"),
                "target_revision": record.get("target_revision"),
                "target_symbol_status": record.get("target_symbol_status", {}),
                "target_definition_text": record.get("target_definition_text", {}),
                "source_context": _trim_block(record.get("source_context"), self.config.code_context_chars),
                "target_context": _trim_block(record.get("target_context"), self.config.code_context_chars),
                "symbol_evidence": (record.get("symbol_evidence") or [])[:24],
                "errors": record.get("errors") or [],
            }

        payload: dict[str, Any] = {
            "task": "identify_cross_hunk_dependencies",
            "patch_id": patch_id,
            "source_revision": source_revision,
            "target_revision": target_revision,
            "instructions": PAYLOAD_INSTRUCTIONS,
            "output_schema": COMPACT_OUTPUT_SCHEMA if self.config.compact_schema else OUTPUT_SCHEMA,
            "hunks": hunk_summaries,
            "candidate_pairs": _trim_pairs(pairs, self.config.code_context_chars),
            "code_context": code_context,
        }
        if existing_analysis:
            payload["existing_analysis"] = existing_analysis
        if additional_evidence:
            payload["additional_evidence"] = additional_evidence
        return payload

    def _select_candidates(self, candidates: list[dict]) -> tuple[list[dict], list[dict]]:
        wanted = [
            candidate
            for candidate in candidates
            if candidate.get("signals") or self.config.include_no_signal_pairs
        ]
        ranked = sorted(
            wanted,
            key=lambda item: (
                -len(item.get("signals") or []),
                hunk_sort_key(item["pair"][0]),
                hunk_sort_key(item["pair"][1]),
            ),
        )
        selected = ranked[: self.config.max_candidate_pairs]
        selected.sort(key=lambda item: (hunk_sort_key(item["pair"][0]), hunk_sort_key(item["pair"][1])))
        skipped = ranked[self.config.max_candidate_pairs :]
        return selected, skipped

    def _batch(self, pairs: list[dict]) -> list[list[dict]]:
        size = max(1, self.config.max_pairs_per_call)
        return [pairs[index : index + size] for index in range(0, len(pairs), size)]

    # -- model interaction ------------------------------------------------- #

    def _run_batch(
        self, payload: dict, hunk_ids: list[str], *, batch_label: str
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        records: list[dict] = []
        questions: list[dict] = []
        rejected: list[dict] = []
        calls: list[dict] = []
        attempts_allowed = max(1, self.config.max_format_retries + 1)
        extra_messages: list[dict] = []
        for attempt in range(1, attempts_allowed + 1):
            call = self.llm.analyze_dependencies(
                payload,
                extra_messages=extra_messages or None,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
            calls.append(_summarise_call(call, batch_label, attempt))
            if self.config.log_raw_responses and self.recorder is not None:
                raw = call.get("raw") if isinstance(call.get("raw"), dict) else {}
                hint = ""
                if call.get("status") != "ok" and raw.get("finish_reason") == "length":
                    hint = (
                        "output budget was consumed before any answer was produced; "
                        "for reasoning models set reasoning_effort=none "
                        "(PATCH_HUNK_AGENT_REASONING_EFFORT) or lower max_pairs_per_call"
                    )
                self.recorder.record(
                    "llm_raw_response",
                    batch=batch_label,
                    attempt=attempt,
                    status=call.get("status"),
                    finish_reason=raw.get("finish_reason"),
                    usage=call.get("usage"),
                    hint=hint,
                    raw_text=(call.get("raw_text") or "")[:4000],
                )
            if call.get("status") == "ok" and isinstance(call.get("parsed"), dict):
                parsed = call["parsed"]
                batch_records, batch_rejected = self._validate_dependencies(
                    parsed.get("dependencies"), hunk_ids, batch_label
                )
                batch_questions = self._validate_questions(
                    parsed.get("unresolved_questions"), hunk_ids
                )
                usable = bool(batch_records) or not batch_rejected
                if usable:
                    for record in batch_records:
                        record["notes"] = [f"batch={batch_label}", f"attempt={attempt}"]
                    return batch_records, batch_questions, rejected + batch_rejected, calls
                rejected.extend(batch_rejected)
                if attempt < attempts_allowed:
                    extra_messages = [
                        {"role": "assistant", "content": (call.get("raw_text") or "")[:4000]},
                        {"role": "user", "content": REPAIR_INSTRUCTION},
                    ]
                    continue
                questions.append(
                    {
                        "question": (
                            f"{batch_label}: the model returned records that could not be "
                            "validated; see rejected_records"
                        ),
                        "hunk_ids": sort_hunk_ids(hunk for pair in payload["candidate_pairs"] for hunk in pair["pair"]),
                        "reason": "invalid model output",
                        "needs_more_evidence": False,
                    }
                )
                return [], questions, rejected, calls

            if attempt < attempts_allowed:
                extra_messages = [
                    {"role": "assistant", "content": (call.get("raw_text") or "")[:4000]},
                    {"role": "user", "content": REPAIR_INSTRUCTION},
                ]
                continue
            questions.append(
                {
                    "question": f"{batch_label}: the model did not return usable JSON",
                    "hunk_ids": sort_hunk_ids(hunk for pair in payload["candidate_pairs"] for hunk in pair["pair"]),
                    "reason": call.get("error") or "unparseable model output",
                    "needs_more_evidence": False,
                }
            )
            return [], questions, rejected, calls
        return records, questions, rejected, calls  # pragma: no cover - loop always returns

    def _validate_dependencies(
        self, raw_records: Any, hunk_ids: list[str], batch_label: str
    ) -> tuple[list[dict], list[dict]]:
        records: list[dict] = []
        rejected: list[dict] = []
        if raw_records is None:
            return records, rejected
        if not isinstance(raw_records, list):
            rejected.append(
                {
                    "batch": batch_label,
                    "reason": "dependencies must be a list",
                    "record": raw_records,
                }
            )
            return records, rejected
        for index, record in enumerate(raw_records):
            if not isinstance(record, dict):
                rejected.append(
                    {
                        "batch": batch_label,
                        "index": index,
                        "reason": "record is not an object",
                        "record": record,
                    }
                )
                continue
            problems = check_dependency(record, known_hunks=hunk_ids)
            if problems:
                rejected.append(
                    {
                        "batch": batch_label,
                        "index": index,
                        "reason": "; ".join(problems),
                        "record": record,
                    }
                )
                continue
            normalised = normalize_dependency(record)
            if normalised["source_hunk"] == normalised["target_hunk"]:
                rejected.append(
                    {
                        "batch": batch_label,
                        "index": index,
                        "reason": "a hunk cannot depend on itself",
                        "record": record,
                    }
                )
                continue
            records.append(normalised)
        return records, rejected

    def _validate_questions(self, raw_questions: Any, hunk_ids: list[str]) -> list[dict]:
        questions: list[dict] = []
        if not isinstance(raw_questions, list):
            return questions
        known = set(hunk_ids)
        for question in raw_questions:
            if isinstance(question, str):
                questions.append(
                    {"question": question, "hunk_ids": [], "reason": "", "needs_more_evidence": True}
                )
                continue
            if not isinstance(question, dict):
                continue
            item = {
                "question": str(question.get("question") or "").strip(),
                "hunk_ids": sort_hunk_ids(
                    hunk_id for hunk_id in question.get("hunk_ids") or [] if hunk_id in known
                ),
                "reason": str(question.get("reason") or ""),
                "needs_more_evidence": bool(question.get("needs_more_evidence", True)),
            }
            if question.get("suggested_query"):
                item["suggested_query"] = question["suggested_query"]
            if not item["question"]:
                continue
            questions.append(item)
        return questions

    def _seed_existing_analysis(
        self, existing_analysis: list[dict] | None, hunk_ids: list[str]
    ) -> tuple[list[dict], list[dict]]:
        records: list[dict] = []
        rejected: list[dict] = []
        for index, record in enumerate(existing_analysis or []):
            if not isinstance(record, dict):
                rejected.append(
                    {"source": "existing_analysis", "index": index, "reason": "not an object"}
                )
                continue
            problems = check_dependency(record, known_hunks=hunk_ids)
            if problems:
                rejected.append(
                    {
                        "source": "existing_analysis",
                        "index": index,
                        "reason": "; ".join(problems),
                        "record": record,
                    }
                )
                continue
            normalised = normalize_dependency(record)
            normalised["origin"] = normalised.get("origin") or "existing_analysis"
            records.append(normalised)
        return records, rejected

    # -- phase D: targeted extra evidence ---------------------------------- #

    def _evidence_round(
        self,
        *,
        records: list[dict],
        hunks: list[dict],
        hunk_ids: list[str],
        patch_id: str | None,
        source_revision: str | None,
        target_revision: str | None,
        context_map: dict[str, dict],
        candidates: list[dict],
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        unresolved = [
            record
            for record in records
            if record.get("needs_more_evidence")
            or record.get("target_version_status") == "unknown"
        ]
        if not unresolved:
            return records, [], [], []

        requests: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for record in unresolved:
            symbol = _first_symbol(record)
            if not symbol:
                continue
            key = (symbol, "target")
            if key in seen:
                continue
            seen.add(key)
            requests.append(
                {
                    "symbol": symbol,
                    "side": "target",
                    "revision": target_revision,
                    "hunk_ids": [record.get("source_hunk"), record.get("target_hunk")],
                    "reason": record.get("reasoning_summary") or "target version state unknown",
                }
            )

        evidence: dict[str, Any] = {}
        for request in requests:
            result = self.context_provider.additional_evidence(request)
            evidence[symbol_key(request["symbol"])] = {
                "request": request,
                "result": result,
            }
            if self.recorder is not None:
                self.recorder.record(
                    "additional_evidence",
                    symbol=request["symbol"],
                    status=(result or {}).get("status"),
                    match_count=len((result or {}).get("matches") or []),
                )

        pairs = [
            candidate
            for candidate in candidates
            if any(
                hunk in {
                    record.get("source_hunk"),
                    record.get("target_hunk"),
                }
                for record in unresolved
                for hunk in candidate.get("pair", [])
            )
        ]
        if not pairs:
            return records, [], [], []

        kept = [
            record
            for record in records
            if not (
                record.get("needs_more_evidence")
                or record.get("target_version_status") == "unknown"
            )
        ]
        payload = self._build_payload(
            patch_id=patch_id,
            hunks=hunks,
            pairs=pairs,
            context_map=context_map,
            source_revision=source_revision,
            target_revision=target_revision,
            existing_analysis=None,
            additional_evidence=evidence,
        )
        payload["instructions"] = PAYLOAD_INSTRUCTIONS + [
            "This is a follow-up round. additional_evidence holds the requested lookups. "
            "Re-judge the listed pairs with that evidence and return the corrected records."
        ]
        batch_records, questions, rejected, calls = self._run_batch(
            payload, hunk_ids, batch_label="evidence-round"
        )
        if not calls:
            return records, [], [], []
        for record in batch_records:
            record["extra_evidence_rounds"] = 1
        merged = kept + batch_records
        return merged, questions, calls, rejected

    # -- finalisation ------------------------------------------------------ #

    def _normalise_records(
        self, records: list[dict], hunk_ids: list[str]
    ) -> tuple[list[dict], list[dict]]:
        notes: list[dict] = []
        merged: dict[tuple[str, str, str], dict] = {}
        order: list[tuple[str, str, str]] = []
        for record in records:
            source = str(record.get("source_hunk"))
            target = str(record.get("target_hunk"))
            category = str(record.get("category"))
            key = (*sorted((source, target), key=hunk_sort_key), category)
            existing = merged.get(key)
            if existing is None:
                merged[key] = record
                order.append(key)
                continue
            if STRENGTH_ORDER.get(str(record.get("strength")), 99) < STRENGTH_ORDER.get(
                str(existing.get("strength")), 99
            ):
                notes.append(
                    {
                        "reason": "duplicate pair kept the weaker record",
                        "record": record,
                        "kept": {k: existing.get(k) for k in ("source_hunk", "target_hunk", "category")},
                    }
                )
                merged[key] = record
            else:
                notes.append(
                    {
                        "reason": "duplicate pair dropped in favour of the stronger record",
                        "record": record,
                    }
                )
        final: list[dict] = []
        for index, key in enumerate(order, start=1):
            record = dict(merged[key])
            record["dependency_id"] = f"D{index}"
            final.append(record)
        return final, notes

    @staticmethod
    def _status(
        batches: int,
        calls: list[dict],
        questions: list[dict],
        rejected: list[dict],
        records: list[dict],
    ) -> str:
        if batches == 0:
            return "partial" if (questions or rejected) else "complete"
        usable = [call for call in calls if call.get("status") == "ok"]
        if not usable:
            return "failed"
        if questions or rejected:
            return "partial"
        if len(usable) < batches:
            return "partial"
        return "complete"


def _summarise_call(call: dict, batch_label: str, attempt: int) -> dict:
    return {
        "batch": batch_label,
        "attempt": attempt,
        "status": call.get("status"),
        "model": call.get("model"),
        "usage": call.get("usage"),
        "latency_s": call.get("latency_s"),
        "error": call.get("error"),
        "repairs": call.get("repairs"),
        "notes": call.get("notes"),
        "dependency_count": len((call.get("parsed") or {}).get("dependencies") or [])
        if isinstance(call.get("parsed"), dict)
        else 0,
        "raw_text_preview": (call.get("raw_text") or "")[:600],
    }


def _trim_block(block: Any, limit: int) -> dict | None:
    if not isinstance(block, dict):
        return None
    trimmed = dict(block)
    code = trimmed.get("code")
    if isinstance(code, str) and len(code) > limit:
        trimmed["code"] = code[:limit] + "\n...<truncated>"
        trimmed["truncated"] = True
    return trimmed


def _trim_pairs(pairs: list[dict], context_chars: int) -> list[dict]:
    trimmed: list[dict] = []
    for pair in pairs:
        item = {
            "pair": pair.get("pair"),
            "signal_names": pair.get("signal_names"),
            "shared_symbols": pair.get("shared_symbols"),
            "machine_hint": pair.get("machine_hint"),
            "same_file": pair.get("same_file"),
            "proximity_notes": pair.get("proximity_notes"),
            "signals": [],
            "machine_evidence": (pair.get("machine_evidence") or [])[:6],
        }
        for signal in pair.get("signals") or []:
            item["signals"].append(
                {
                    key: value
                    for key, value in signal.items()
                    if key
                    in (
                        "signal",
                        "direction",
                        "symbol",
                        "definition",
                        "reference",
                        "target_symbol_status",
                        "description",
                        "before_params",
                        "after_params",
                    )
                }
            )
        trimmed.append(item)
    return trimmed


def _first_symbol(record: dict) -> str | None:
    for item in record.get("evidence") or []:
        symbol = str(item.get("symbol") or "").strip()
        if symbol:
            return symbol
    return None


def symbol_key(symbol: str) -> str:
    return str(symbol).strip()
