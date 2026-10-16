"""End-to-end orchestration: patch -> hunks -> evidence -> analysis -> groups.

This module is the only place that wires the stages together, so every other
module stays independently testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import AnalysisError, PACKAGE_VERSION
from .context_provider import (
    CodeContextProvider,
    GitRepoAccess,
    LocalTreeAccess,
    NullAccess,
    SourceAccess,
)
from .dependency_analyzer import DependencyAnalyzer
from .dependency_validator import DependencyValidator
from .evidence_extractor import enrich_hunks, extract
from .evaluator import Evaluator
from .grouping_engine import GroupingEngine, merge_groups_across_strong_edges
from .global_grouper import GlobalGrouper
from .partition_validator import PartitionValidator
from .patch_parser import PatchParser
from .portgpt_adapter import PortGPTAdapter
from .schemas import sort_hunk_ids
from .utils.logging_utils import RunRecorder, get_logger, setup_logging

__all__ = ["HunkGroupingPipeline", "PipelineInputs", "build_pipeline"]

LOGGER = get_logger("pipeline")


@dataclass
class PipelineInputs:
    """Everything one analysis needs; every field has a documented default."""

    patch_text: str | None = None
    patch_path: str | None = None
    patch_id: str | None = None
    source_revision: str | None = None
    target_revision: str | None = None
    source_trees: dict[str, str] = field(default_factory=dict)
    target_trees: dict[str, str] = field(default_factory=dict)
    source_dir: str | None = None
    target_dir: str | None = None
    repo_path: str | None = None
    existing_analysis: list[dict] | None = None
    resolutions: dict[str, str] | None = None
    build_tasks: bool = True


class HunkGroupingPipeline:
    """Runs the documented pipeline and returns every intermediate artefact."""

    def __init__(
        self,
        config: Any | None = None,
        *,
        llm: Any | None = None,
        source_access: SourceAccess | None = None,
        target_access: SourceAccess | None = None,
        context_provider: CodeContextProvider | None = None,
        recorder: RunRecorder | None = None,
    ) -> None:
        from .config import HunkAgentConfig

        self.config = config if isinstance(config, HunkAgentConfig) else HunkAgentConfig.load()
        setup_logging(self.config.logging.level, self.config.logging.log_file)
        self.recorder = recorder
        self._llm = llm
        self._source_access = source_access
        self._target_access = target_access
        self._context_provider = context_provider
        self.parser = PatchParser()

    # -- construction helpers --------------------------------------------- #

    @property
    def llm(self) -> Any:
        if self._llm is None:
            from .llm import build_llm_client

            self._llm = build_llm_client(
                self.config.provider, provider_config=self.config.provider_config
            )
        return self._llm

    def context_provider_for(self, inputs: PipelineInputs) -> CodeContextProvider:
        if self._context_provider is not None:
            return self._context_provider
        source_access = self._source_access or _access_for_inputs(inputs, "source")
        target_access = self._target_access or _access_for_inputs(inputs, "target")
        return CodeContextProvider(
            source_access=source_access,
            target_access=target_access,
            config=self.config.context,
            recorder=self.recorder,
        )

    # -- main entry point -------------------------------------------------- #

    def run(self, inputs: PipelineInputs | None = None, **kwargs: Any) -> dict:
        if inputs is None:
            inputs = PipelineInputs(**kwargs)
        elif kwargs:
            raise ValueError("pass either a PipelineInputs object or keyword arguments, not both")

        started = time.perf_counter()
        recorder = self.recorder or RunRecorder(
            path=self.config.logging.record_file if self.config.logging.record_run else None,
            enabled=self.config.logging.record_run,
        )
        self.recorder = recorder
        recorder.record(
            "run_started",
            agent_version=PACKAGE_VERSION,
            provider=self.config.provider,
            patch_path=inputs.patch_path,
            source_revision=inputs.source_revision,
            target_revision=inputs.target_revision,
        )

        document = self._parse(inputs)
        hunks = enrich_hunks(document.hunk_dicts())
        hunk_ids = sort_hunk_ids(hunk["hunk_id"] for hunk in hunks)
        recorder.record(
            "patch_parsed",
            patch_id=document.patch_id,
            hunk_count=len(hunks),
            file_count=len(document.files),
            warnings=list(document.warnings),
        )

        provider = self.context_provider_for(inputs)
        contexts: list[dict] = []
        if self.config.context.enabled:
            contexts = provider.get_contexts(
                hunks, inputs.source_revision, inputs.target_revision
            )
        candidates = extract(hunks, contexts)
        recorder.record(
            "evidence_extracted",
            candidate_pair_count=len(candidates),
            signal_pair_count=sum(1 for item in candidates if item.get("signals")),
        )

        analyzer = DependencyAnalyzer(
            self.llm,
            config=self.config.analyzer,
            context_provider=provider if self.config.context.enabled else None,
            recorder=recorder,
        )
        analysis = analyzer.analyze(
            hunks,
            candidates,
            patch_id=document.patch_id,
            source_revision=inputs.source_revision,
            target_revision=inputs.target_revision,
            contexts=contexts,
            existing_analysis=inputs.existing_analysis,
        )
        if not analysis["dependencies"] and not candidates and analysis["status"] == "complete":
            LOGGER.info("no candidate pairs were produced for %s", document.patch_id)

        validator = DependencyValidator(self.config.validator)
        validated = validator.validate(analysis["dependencies"], hunk_ids, contexts=contexts)
        recorder.record(
            "dependencies_validated",
            accepted=len(validated["dependencies"]),
            rejected=len(validated["rejected"]),
            downgrades=validated["stats"]["downgrade_count"],
        )

        grouping_engine = GroupingEngine(self.config.grouping)
        if self.config.grouping.mode == "global":
            # Primary decision: the model proposes work packages, the graph guards them.
            grouper = GlobalGrouper(
                self.llm,
                config=self.config.grouping,
                engine=grouping_engine.group,
                recorder=recorder,
            )
            grouping = grouper.group(
                hunk_ids,
                validated["dependencies"],
                resolutions=inputs.resolutions,
                hunks=hunks,
                contexts=contexts,
                patch_id=document.patch_id,
                grouping_config=self.config.grouping,
            )
        else:
            grouping = grouping_engine.group(
                hunk_ids, validated["dependencies"], resolutions=inputs.resolutions
            )
        if self.config.grouping.mode == "global":
            # The global grouper already applied its bounded constraint repair; the
            # transitive merge must not run again or it would rebuild the blob.
            groups, repairs = grouping["groups"], []
        else:
            groups, repairs = merge_groups_across_strong_edges(
                grouping["groups"],
                validated["dependencies"],
                max_rounds=self.config.grouping.repartition_rounds,
            )
        if repairs:
            grouping = dict(grouping)
            grouping["groups"] = groups
            grouping["metadata"] = {**grouping.get("metadata", {}), "partition_repairs": repairs}
            recorder.record("partition_repaired", repairs=repairs)

        questions = list(analysis.get("unresolved_questions") or [])
        questions.extend(validated.get("unresolved_questions") or [])
        questions.extend(grouping.get("unresolved_questions") or [])
        questions = _dedupe_questions(questions)

        partition_validator = PartitionValidator(self.config.partition_validator)
        grouping_document = partition_validator.validate(
            hunk_ids,
            grouping,
            validated["dependencies"],
            patch_id=document.patch_id,
            unresolved_questions=questions,
            analysis_status=analysis["status"],
            deferred_dependency_ids=[
                str(item.get("dependency_id"))
                for item in (grouping.get("metadata") or {}).get("deferred_constraints") or []
            ],
            metadata={
                "provider": self.config.provider,
                "model": getattr(self.llm, "model", None),
            },
        )
        status = grouping_document["status"]
        if status != "failed" and analysis["status"] != "complete":
            status = "partial"
        grouping_document["status"] = status
        grouping_document["metadata"].update(
            {
                "dependency_validation": validated["stats"],
                "grouping": grouping.get("metadata", {}),
                "analysis_status": analysis["status"],
                "usage": analysis["metadata"].get("usage"),
            }
        )
        if validated["warnings"]:
            grouping_document["warnings"].extend(validated["warnings"])
        if validated["rejected"]:
            grouping_document["warnings"].extend(
                [
                    {"reason": "dependency_rejected", **item}
                    for item in validated["rejected"]
                ]
            )

        result: dict[str, Any] = {
            "agent_version": PACKAGE_VERSION,
            "patch_id": document.patch_id,
            "status": status,
            "patch": document.summary(),
            "hunks": hunks,
            "contexts": contexts,
            "candidates": candidates,
            "analysis": analysis,
            "dependency_validation": validated,
            "grouping": grouping,
            "grouping_document": grouping_document,
        }

        if inputs.build_tasks:
            adapter = PortGPTAdapter()
            result["tasks"] = adapter.build_tasks(
                grouping_document,
                hunks,
                contexts=contexts,
                analysis={"dependencies": validated["dependencies"]},
                source_revision=inputs.source_revision,
                target_revision=inputs.target_revision,
                patch_id=document.patch_id,
            )

        duration = round(time.perf_counter() - started, 6)
        recorder.record(
            "run_finished",
            patch_id=document.patch_id,
            status=status,
            group_count=len(grouping_document["groups"]),
            dependency_count=len(validated["dependencies"]),
            unresolved_count=len(grouping_document["unresolved_questions"]),
            duration_s=duration,
        )
        result["run"] = {
            "duration_s": duration,
            "tokens": analysis["metadata"].get("usage"),
            "recorder": recorder.summary(),
            "events": recorder.events,
            "config": {
                "provider": self.config.provider,
                "model": getattr(self.llm, "model", None),
                "evidence_mode": self.config.evidence.mode,
                "config_path": self.config.config_path,
            },
            "context_availability": {
                "source": _access_name(self._source_access)
                or _access_name(_access_for_inputs(inputs, "source")),
                "target": _access_name(self._target_access)
                or _access_name(_access_for_inputs(inputs, "target")),
            },
        }
        return result

    # -- helpers ----------------------------------------------------------- #

    def _parse(self, inputs: PipelineInputs):
        patch_id = inputs.patch_id
        if inputs.patch_text is not None:
            return self.parser.parse_document(inputs.patch_text, patch_id=patch_id or "patch_001")
        if inputs.patch_path is not None:
            path = Path(inputs.patch_path)
            if not path.is_file():
                raise AnalysisError(f"patch file not found: {path}")
            return self.parser.parse_file(path, patch_id=patch_id or path.stem)
        raise AnalysisError("no patch supplied: pass patch_text or patch_path")

    def evaluate(
        self,
        dataset_path: str | Path,
        predictions_path: str | Path,
        output_path: str | Path | None = None,
    ) -> dict:
        return Evaluator().run_from_files(dataset_path, predictions_path, output_path)


def _access_for_inputs(inputs: PipelineInputs, side: str) -> SourceAccess:
    trees = inputs.source_trees if side == "source" else inputs.target_trees
    directory = inputs.source_dir if side == "source" else inputs.target_dir
    revision = inputs.source_revision if side == "source" else inputs.target_revision
    if inputs.repo_path:
        return GitRepoAccess(inputs.repo_path, name=f"{side}-git")
    if trees:
        return LocalTreeAccess(dict(trees), name=f"{side}-trees")
    if directory:
        mapping = {revision: directory} if revision else {}
        return LocalTreeAccess(mapping, default_dir=directory, name=f"{side}-tree")
    return NullAccess()


def _access_name(access: Any) -> str | None:
    if access is None:
        return None
    name = getattr(access, "name", None)
    available = access.available() if hasattr(access, "available") else False
    return f"{name}{'' if available else ' (unavailable)'}"


def _dedupe_questions(questions: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for question in questions:
        if isinstance(question, str):
            question = {"question": question, "hunk_ids": [], "reason": ""}
        key = (str(question.get("question")), tuple(question.get("hunk_ids") or []))
        if key in seen:
            continue
        seen.add(key)
        out.append(question)
    return out


def build_pipeline(config_path: str | None = None, **kwargs: Any) -> HunkGroupingPipeline:
    from .config import HunkAgentConfig

    return HunkGroupingPipeline(HunkAgentConfig.load(config_path), **kwargs)
