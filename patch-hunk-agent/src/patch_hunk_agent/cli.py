"""Command line interface (section 14)."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from . import PACKAGE_VERSION
from .config import HunkAgentConfig, load_config
from .dependency_validator import DependencyValidator
from .evaluator import Evaluator
from .evidence_extractor import enrich_hunks, extract
from .grouping_engine import GroupingEngine
from .partition_validator import PartitionValidator
from .patch_parser import PatchParser
from .pipeline import HunkGroupingPipeline, PipelineInputs
from .portgpt_spec import (
    DEFAULT_SPEC_FILENAME,
    build_merged_hunks,
    build_group_spec,
    check_alignment,
    check_spec_coverage,
    dump_merged_hunks,
    grouping_document_from_spec,
    load_group_spec,
    split_patch_with_portgpt,
    write_group_spec,
)
from .utils.json_utils import load_json, save_json
from .utils.logging_utils import setup_logging

__all__ = ["main", "build_parser"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch-hunk-agent",
        description=(
            "Analyse cross-hunk dependencies in a patch and group the hunks that must be "
            "backported together."
        ),
    )
    parser.add_argument("--version", action="version", version=f"patch-hunk-agent {PACKAGE_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    group = subparsers.add_parser("group", help="analyse a patch and emit the grouping JSON")
    group.add_argument("--patch", required=True, help="path to a unified diff")
    group.add_argument("--patch-id", default=None, help="override the patch identifier")
    group.add_argument("--source-revision", default=None, help="source revision/commit")
    group.add_argument("--target-revision", default=None, help="target revision/commit")
    group.add_argument("--source-tree", default=None, help="directory holding the source version")
    group.add_argument("--target-tree", default=None, help="directory holding the target version")
    group.add_argument("--repo", default=None, help="git repository usable for both revisions")
    group.add_argument("--provider", default=None, help="LLM provider name from configs/models.yaml")
    group.add_argument("--model", default=None, help="override the configured model name")
    group.add_argument("--config", default=None, help="configuration file")
    group.add_argument("--output", default=None, help="write the grouping document here")
    group.add_argument("--analysis-output", default=None, help="write the analysis document here")
    group.add_argument("--tasks-output", default=None, help="write the downstream backport tasks here")
    group.add_argument("--record", default=None, help="write JSON lines run records here")
    group.add_argument("--existing-analysis", default=None, help="reuse an earlier analysis JSON")
    group.add_argument(
        "--resolution",
        action="append",
        default=[],
        metavar="D<n>=promote|keep|drop",
        help="explicitly settle a conditional dependency (repeatable)",
    )
    group.add_argument("--no-context", action="store_true", help="skip repository context retrieval")
    group.add_argument("--no-tasks", action="store_true", help="do not build portgpt tasks")
    group.add_argument(
        "--portgpt-root",
        default=None,
        help="path to the PortGPT tree; enables the hunk-group spec bridge",
    )
    group.add_argument(
        "--portgpt-spec-output",
        default=None,
        help="write the PortGPT group spec here (default results/<patch_id>.hunk_groups.json)",
    )
    group.add_argument(
        "--portgpt-spec-language",
        default="zh",
        choices=["zh", "en"],
        help="language of the generated group name/concern/why_together text",
    )
    group.add_argument(
        "--portgpt-strict",
        action="store_true",
        help="fail when the hunk numbering does not align with PortGPT's splitter",
    )
    group.add_argument(
        "--merged-hunks-dir",
        default=None,
        help="write PortGPT-compatible merged hunk patch files here",
    )
    group.add_argument(
        "--grouping-mode",
        default=None,
        choices=["global", "components"],
        help="global = the model proposes work packages (default); components = the document's "
        "connected-component algorithm",
    )
    group.add_argument("--print-json", action="store_true", help="print the full result as JSON")
    group.add_argument("--log-level", default=None)

    inspect = subparsers.add_parser("inspect", help="show the hunk inventory and machine evidence")
    inspect.add_argument("--patch", required=True)
    inspect.add_argument("--source-tree", default=None)
    inspect.add_argument("--target-tree", default=None)
    inspect.add_argument("--source-revision", default=None)
    inspect.add_argument("--target-revision", default=None)
    inspect.add_argument("--config", default=None)

    validate = subparsers.add_parser(
        "validate", help="re-validate an existing grouping document"
    )
    validate.add_argument("--grouping", required=True, help="grouping JSON to re-validate")
    validate.add_argument("--analysis", default=None, help="analysis JSON holding the dependencies")
    validate.add_argument("--config", default=None)
    validate.add_argument("--output", default=None)

    evaluate = subparsers.add_parser("evaluate", help="score groupings against a labelled dataset")
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--output", default=None)

    spec = subparsers.add_parser(
        "portgpt-spec",
        help="build (or check) the hunk-group spec PortGPT consumes",
    )
    spec.add_argument("--patch", required=True, help="the upstream patch PortGPT will backport")
    spec.add_argument("--portgpt-root", required=True, help="path to the PortGPT tree")
    spec.add_argument("--grouping", default=None, help="this agent's grouping JSON")
    spec.add_argument("--analysis", default=None, help="this agent's analysis JSON (used to regroup)")
    spec.add_argument("--check-spec", default=None, help="validate an existing spec instead")
    spec.add_argument(
        "--from-spec",
        default=None,
        help="import an existing PortGPT spec into this agent's grouping format",
    )
    spec.add_argument("--grouping-output", default=None, help="where to write the imported grouping")
    spec.add_argument("--output", default=None, help="where to write the spec")
    spec.add_argument(
        "--merged-hunks-dir",
        default=None,
        help="write PortGPT-compatible merged hunk patch files here",
    )
    spec.add_argument("--language", default="zh", choices=["zh", "en"])
    spec.add_argument("--strict", action="store_true", help="exit 1 when not aligned/covered")
    spec.add_argument("--config", default=None)
    spec.add_argument("--print-json", action="store_true")

    demo = subparsers.add_parser(
        "demo", help="run the bundled fixtures with the offline mock provider"
    )
    demo.add_argument("--output", default="examples/output")
    demo.add_argument("--fixtures", default="tests/fixtures")
    demo.add_argument("--config", default=None)

    subparsers.add_parser("providers", help="list the configured providers")
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "group": _cmd_group,
        "inspect": _cmd_inspect,
        "validate": _cmd_validate,
        "evaluate": _cmd_evaluate,
        "portgpt-spec": _cmd_portgpt_spec,
        "demo": _cmd_demo,
        "providers": _cmd_providers,
    }
    try:
        return int(handlers[args.command](args) or 0)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def _cmd_group(args: argparse.Namespace) -> int:
    config = _load_config(args)
    if args.no_context:
        config.context.enabled = False
    if getattr(args, "grouping_mode", None):
        config.grouping.mode = args.grouping_mode
    provider = args.provider or config.provider
    provider_config = dict(config.provider_config)
    if args.model:
        provider_config["model"] = args.model
    if provider != config.provider:
        provider_config = dict(config.models.get("providers", {}).get(provider, {}))
        if args.model:
            provider_config["model"] = args.model

    from .llm import build_llm_client

    llm = build_llm_client(provider, provider_config=provider_config)
    pipeline = HunkGroupingPipeline(config, llm=llm)
    inputs = PipelineInputs(
        patch_path=args.patch,
        patch_id=args.patch_id,
        source_revision=args.source_revision,
        target_revision=args.target_revision,
        source_dir=args.source_tree,
        target_dir=args.target_tree,
        repo_path=args.repo,
        existing_analysis=load_json(args.existing_analysis) if args.existing_analysis else None,
        resolutions=_parse_resolutions(args.resolution),
        build_tasks=not args.no_tasks,
    )
    if args.record:
        config.logging.record_run = True
        config.logging.record_file = args.record
    result = pipeline.run(inputs)

    output = args.output or str(Path(config.output.directory) / f"{result['patch_id']}.grouping.json")
    save_json(output, result["grouping_document"])
    if args.analysis_output:
        save_json(args.analysis_output, result["analysis"])
    if args.tasks_output and result.get("tasks"):
        save_json(args.tasks_output, result["tasks"])

    bridge_report = None
    if args.portgpt_root:
        bridge_report = _write_portgpt_spec(
            patch_path=args.patch,
            portgpt_root=args.portgpt_root,
            hunks=result["hunks"],
            grouping_document=result["grouping_document"],
            dependencies=result["dependency_validation"]["dependencies"],
            patch_id=result["patch_id"],
            model=config.provider_config.get("model") or config.provider,
            language=args.portgpt_spec_language,
            output=args.portgpt_spec_output
            or str(Path(config.output.directory) / f"{result['patch_id']}.{DEFAULT_SPEC_FILENAME}"),
            merged_hunks_dir=args.merged_hunks_dir,
        )

    if args.print_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_summary(result, output)
        if bridge_report:
            _print_bridge(bridge_report)
    if args.portgpt_strict and bridge_report and not bridge_report["ok"]:
        return 1
    return 1 if result["status"] == "failed" else 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    config = _load_config(args)
    parser = PatchParser()
    document = parser.parse_file(args.patch, patch_id=Path(args.patch).stem)
    hunks = enrich_hunks(document.hunk_dicts())
    payload = {
        "patch": document.summary(),
        "hunks": [
            {
                "hunk_id": hunk["hunk_id"],
                "file_path": hunk["file_path"],
                "header": hunk["header"],
                "symbols": hunk["symbols"],
                "parse_warnings": hunk["parse_warnings"],
            }
            for hunk in hunks
        ],
    }
    if args.source_tree or args.target_tree:
        from .context_provider import CodeContextProvider, LocalTreeAccess

        provider = CodeContextProvider(
            source_access=LocalTreeAccess(
                {args.source_revision: args.source_tree} if args.source_revision else {},
                default_dir=args.source_tree,
            )
            if args.source_tree
            else None,
            target_access=LocalTreeAccess(
                {args.target_revision: args.target_tree} if args.target_revision else {},
                default_dir=args.target_tree,
            )
            if args.target_tree
            else None,
            config=config.context,
        )
        contexts = provider.get_contexts(hunks, args.source_revision, args.target_revision)
        payload["retrieval"] = [
            {
                "hunk_id": record["hunk_id"],
                "retrieval_status": record["retrieval_status"],
                "target_symbol_status": record.get("target_symbol_status"),
                "errors": record.get("errors"),
            }
            for record in contexts
        ]
        payload["candidate_pairs"] = extract(hunks, contexts)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _load_config(args)
    grouping = load_json(args.grouping)
    analysis = load_json(args.analysis) if args.analysis else None
    if analysis is None:
        analysis = grouping.get("analysis") if isinstance(grouping, dict) else None
    dependencies = (analysis or {}).get("dependencies") or grouping.get("dependencies") or []
    hunk_ids = (analysis or {}).get("hunk_ids") or sorted(
        {hunk for group in grouping.get("groups") or [] for hunk in group.get("hunk_ids") or []}
    )
    validator = DependencyValidator(config.validator)
    validated = validator.validate(dependencies, hunk_ids)
    engine = GroupingEngine(config.grouping)
    rebuilt = engine.group(hunk_ids, validated["dependencies"])
    partition = PartitionValidator(config.partition_validator)
    document = partition.validate(
        hunk_ids,
        grouping,
        validated["dependencies"],
        patch_id=str(grouping.get("patch_id") or (analysis or {}).get("patch_id") or "patch_001"),
        unresolved_questions=(analysis or {}).get("unresolved_questions"),
        analysis_status=(analysis or {}).get("status"),
    )
    report = {
        "patch_id": document["patch_id"],
        "status": document["status"],
        "validation": document["validation"],
        "errors": document["errors"],
        "warnings": document["warnings"] + validated["warnings"],
        "checks": document["checks"],
        "dependency_stats": validated["stats"],
        "regrouped_reference": rebuilt["groups"],
        "rejected_dependencies": validated["rejected"],
    }
    if args.output:
        save_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if document["status"] != "failed" else 1


def _cmd_evaluate(args: argparse.Namespace) -> int:
    evaluator = Evaluator()
    report = evaluator.run_from_files(args.dataset, args.predictions, args.output)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    return 0


def _cmd_portgpt_spec(args: argparse.Namespace) -> int:
    """Build the group spec PortGPT reads, or validate an existing one."""
    config = _load_config(args)
    patch_text = Path(args.patch).read_text(encoding="utf-8", errors="replace")
    document = PatchParser().parse_document(patch_text, patch_id=args.patch and Path(args.patch).stem)
    hunks = enrich_hunks(document.hunk_dicts())

    try:
        splitter_info, _message, portgpt_hunks = split_patch_with_portgpt(
            args.portgpt_root, patch_text
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: cannot load PortGPT splitter: {exc}", file=sys.stderr)
        return 2
    alignment = check_alignment(hunks, portgpt_hunks)

    if args.check_spec:
        spec = load_group_spec(args.check_spec)
        coverage = check_spec_coverage(spec, len(portgpt_hunks))
        report = {
            "spec": str(args.check_spec),
            "hunk_count": len(portgpt_hunks),
            "alignment": alignment,
            "splitter": splitter_info.to_dict(),
            "coverage": coverage,
        }
        if args.print_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"spec      : {args.check_spec}")
            print(f"hunks     : {len(portgpt_hunks)} (PortGPT numbering)")
            print(f"aligned   : {alignment['aligned']}")
            covered = coverage["hunk_count"] - len(coverage["uncovered_hunks"])
            print(f"covered   : {covered}/{coverage['hunk_count']}")
            for problem in coverage["problems"]:
                print(f"  problem: {problem}")
            if coverage["uncovered_hunks"]:
                print(f"  uncovered hunk indices: {coverage['uncovered_hunks']}")
        ok = alignment["aligned"] and coverage["valid"]
        return 0 if ok or not args.strict else 1

    if args.from_spec:
        spec = load_group_spec(args.from_spec)
        coverage = check_spec_coverage(spec, len(portgpt_hunks))
        imported = grouping_document_from_spec(spec, hunks, patch_id=document.patch_id)
        output = args.grouping_output or str(
            Path(config.output.directory)
            / f"{document.patch_id or 'patch'}.imported.grouping.json"
        )
        save_json(output, imported)
        report = {
            "source_spec": str(args.from_spec),
            "grouping": output,
            "hunk_count": len(portgpt_hunks),
            "group_count": len(imported["groups"]),
            "alignment": alignment,
            "splitter": splitter_info.to_dict(),
            "coverage": coverage,
        }
        if args.print_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"source spec   : {args.from_spec}")
            print(f"imported      : {output} ({len(imported['groups'])} groups)")
            covered = coverage["hunk_count"] - len(coverage["uncovered_hunks"])
            print(f"spec coverage : {covered}/{coverage['hunk_count']} hunks")
            print(f"aligned       : {alignment['aligned']}")
        ok = alignment["aligned"] and coverage["valid"]
        return 0 if ok or not args.strict else 1

    grouping_document = _load_or_rebuild_grouping(args, hunks, config)
    if grouping_document is None:
        print(
            "error: pass --grouping (or --analysis, or --check-spec) so a grouping is available",
            file=sys.stderr,
        )
        return 2

    spec = build_group_spec(
        grouping_document,
        hunks,
        index_of=alignment["index_of"],
        dependencies=_dependencies_for_spec(args, grouping_document),
        patch_id=str(grouping_document.get("patch_id") or "patch_001"),
        model=(grouping_document.get("metadata") or {}).get("source_model")
        or config.provider_config.get("model")
        or config.provider,
        language=args.language,
    )
    coverage = check_spec_coverage(spec, len(portgpt_hunks))
    output = args.output or str(
        Path(config.output.directory)
        / f"{grouping_document.get('patch_id') or 'patch'}.{DEFAULT_SPEC_FILENAME}"
    )
    write_group_spec(spec, output)
    merged_info = _dump_merged(
        spec,
        hunks,
        portgpt_hunks,
        alignment,
        args.merged_hunks_dir,
        grouping_document=grouping_document,
    )
    report = {
        "spec": output,
        "hunk_count": len(portgpt_hunks),
        "group_count": len(spec["groups"]),
        "alignment": alignment,
        "splitter": splitter_info.to_dict(),
        "coverage": coverage,
        **merged_info,
    }
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_bridge({**report, "ok": alignment["aligned"] and coverage["valid"]})
    ok = alignment["aligned"] and coverage["valid"]
    return 0 if ok or not args.strict else 1


def _load_or_rebuild_grouping(args: argparse.Namespace, hunks: list[dict], config):
    """Use the grouping JSON when given, else rebuild groups from the analysis JSON."""
    if args.grouping:
        return load_json(args.grouping)
    if not args.analysis:
        return None
    analysis = load_json(args.analysis)
    hunk_ids = [str(hunk_id) for hunk_id in analysis.get("hunk_ids") or [h["hunk_id"] for h in hunks]]
    validated = DependencyValidator(config.validator).validate(
        analysis.get("dependencies") or [], hunk_ids
    )
    grouped = GroupingEngine(config.grouping).group(hunk_ids, validated["dependencies"])
    return PartitionValidator(config.partition_validator).validate(
        hunk_ids,
        grouped,
        validated["dependencies"],
        patch_id=str(analysis.get("patch_id") or "patch_001"),
        unresolved_questions=analysis.get("unresolved_questions"),
        analysis_status=analysis.get("status"),
    )


def _dependencies_for_spec(args: argparse.Namespace, grouping_document: dict) -> list[dict]:
    """Dependency records to embed; ``None`` lets the bridge use the imported ones."""
    if args.analysis:
        from_analysis = (load_json(args.analysis) or {}).get("dependencies")
        if from_analysis:
            return list(from_analysis)
    inline = grouping_document.get("dependencies")
    return list(inline) if inline else None  # type: ignore[return-value]


def _write_portgpt_spec(
    *,
    patch_path: str,
    portgpt_root: str,
    hunks: list[dict],
    grouping_document: dict,
    dependencies: list[dict],
    patch_id: str,
    model: str | None,
    language: str,
    output: str,
    merged_hunks_dir: str | None = None,
) -> dict:
    """Alignment check + spec build + write, used by ``group``."""
    patch_text = Path(patch_path).read_text(encoding="utf-8", errors="replace")
    try:
        splitter_info, _message, portgpt_hunks = split_patch_with_portgpt(portgpt_root, patch_text)
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {
            "ok": False,
            "error": str(exc),
            "spec": None,
            "alignment": {"aligned": False, "problems": [{"field": "splitter", "detail": str(exc)}]},
            "coverage": {"valid": False, "problems": [], "uncovered_hunks": []},
            "hunk_count": 0,
            "group_count": 0,
            "splitter": {},
        }
    alignment = check_alignment(hunks, portgpt_hunks)
    spec = build_group_spec(
        grouping_document,
        hunks,
        index_of=alignment["index_of"],
        dependencies=dependencies,
        patch_id=patch_id,
        model=model,
        language=language,
    )
    coverage = check_spec_coverage(spec, len(portgpt_hunks))
    write_group_spec(spec, output)
    merged_info = _dump_merged(spec, hunks, portgpt_hunks, alignment, merged_hunks_dir)
    return {
        "ok": alignment["aligned"] and coverage["valid"],
        "error": None,
        "spec": output,
        "alignment": alignment,
        "coverage": coverage,
        "hunk_count": len(portgpt_hunks),
        "group_count": len(spec["groups"]),
        "splitter": splitter_info.to_dict(),
        **merged_info,
    }


def _dump_merged(
    spec: dict,
    hunks: list[dict],
    portgpt_hunks: list[dict],
    alignment: dict,
    merged_hunks_dir: str | None,
    grouping_document: dict | None = None,
) -> dict:
    """Write merged hunk patch files when a target directory was requested."""
    if not merged_hunks_dir:
        return {"merged_hunks_dir": None, "merged_count": 0}
    plan = ((grouping_document or {}).get("metadata") or {}).get("grouping") or {}
    plan = plan.get("execution_plan") or {}
    merged = build_merged_hunks(
        spec,
        hunks,
        portgpt_hunks=portgpt_hunks,
        index_of=alignment["index_of"],
        order=plan.get("order"),
        coordination=plan.get("directives"),
    )
    original_dir, _ported = dump_merged_hunks(merged, merged_hunks_dir)
    return {
        "merged_hunks_dir": str(original_dir),
        "merged_count": len(merged),
        "execution_order": [group.get("group_id") for group in merged],
        "joint_verification": plan.get("joint_verification") or [],
        "shared_target_regions": plan.get("shared_target_regions") or [],
    }


def _print_bridge(report: dict) -> None:
    print(f"portgpt root  : {report.get('splitter', {}).get('origin', '-')}")
    print(
        f"hunk alignment: {report['hunk_count']} hunks, "
        f"{'aligned' if report['alignment']['aligned'] else 'MISALIGNED'} "
        f"(splitter={report.get('splitter', {}).get('source', '-')})"
    )
    for problem in report["alignment"]["problems"][:5]:
        print(f"  problem: {problem}")
    coverage = report["coverage"]
    covered = coverage.get("hunk_count", 0) - len(coverage.get("uncovered_hunks") or [])
    print(f"spec groups   : {report.get('group_count', '-')}")
    print(f"spec coverage : {covered}/{coverage.get('hunk_count', '-')} hunks assigned once")
    for problem in (coverage.get("problems") or [])[:5]:
        print(f"  problem: {problem}")
    if report.get("spec"):
        print(f"portgpt spec  : {report['spec']}")
    if report.get("merged_hunks_dir"):
        print(f"merged hunks  : {report['merged_hunks_dir']} ({report['merged_count']} files)")


def _cmd_demo(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config.provider = "mock-heuristic"
    # The offline demo uses the bundled heuristic provider, which answers dependency
    # judgements rather than whole-patch partitions; keep the documented algorithm.
    config.grouping.mode = "components"
    pipeline = HunkGroupingPipeline(config)
    fixtures = Path(args.fixtures)
    source_tree = fixtures / "trees" / "source"
    target_tree = fixtures / "trees" / "target"
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        ("simple_dependency", "SRC", "TGT"),
        ("signature_dependency", "SRC", "TGT"),
        ("independent_hunks", "SRC", "TGT"),
        ("target_already_satisfies", "SRC", "TGT"),
        ("insufficient_evidence", None, None),
    ]
    summary = []
    for name, source_revision, target_revision in cases:
        patch_path = fixtures / f"{name}.diff"
        if not patch_path.is_file():
            continue
        result = pipeline.run(
            PipelineInputs(
                patch_path=str(patch_path),
                patch_id=name,
                source_revision=source_revision,
                target_revision=target_revision,
                source_dir=str(source_tree) if source_revision else None,
                target_dir=str(target_tree) if target_revision else None,
            )
        )
        save_json(output_dir / f"{name}.grouping.json", result["grouping_document"])
        save_json(output_dir / f"{name}.analysis.json", result["analysis"])
        if result.get("tasks"):
            save_json(output_dir / f"{name}.tasks.json", result["tasks"])
        summary.append(
            {
                "patch_id": name,
                "status": result["status"],
                "groups": [
                    {"group_id": group["group_id"], "hunk_ids": group["hunk_ids"]}
                    for group in result["grouping_document"]["groups"]
                ],
                "dependencies": [
                    {
                        "dependency_id": record["dependency_id"],
                        "pair": [record["source_hunk"], record["target_hunk"]],
                        "category": record["category"],
                        "strength": record["strength"],
                        "target_version_status": record["target_version_status"],
                    }
                    for record in result["dependency_validation"]["dependencies"]
                ],
            }
        )
    save_json(output_dir / "demo_summary.json", {"cases": summary})
    print(json.dumps({"cases": summary}, ensure_ascii=False, indent=2))
    return 0


def _cmd_providers(args: argparse.Namespace) -> int:
    config = load_config(None)
    providers = config.models.get("providers", {})
    print(
        json.dumps(
            {
                "default": config.provider,
                "providers": {
                    name: {
                        "kind": value.get("kind"),
                        "model": value.get("model"),
                        "base_url": value.get("base_url"),
                        "api_key_env": value.get("api_key_env"),
                    }
                    for name, value in providers.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _load_config(args: argparse.Namespace) -> HunkAgentConfig:
    config = load_config(getattr(args, "config", None))
    if getattr(args, "log_level", None):
        config.logging.level = args.log_level
    setup_logging(config.logging.level, config.logging.log_file)
    return config


def _parse_resolutions(values: list[str]) -> dict[str, str]:
    resolutions: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"invalid --resolution {value!r}; expected D<n>=promote|keep|drop")
        key, _, resolution = value.partition("=")
        resolutions[key.strip()] = resolution.strip().lower()
    return resolutions


def _print_summary(result: dict, output_path: str) -> None:
    document = result["grouping_document"]
    print(f"patch_id      : {result['patch_id']}")
    print(f"status        : {result['status']}")
    print(f"hunks         : {len(result['hunks'])} in {result['patch']['file_count']} file(s)")
    print(f"dependencies  : {len(result['dependency_validation']['dependencies'])} validated")
    print(
        "validation    : "
        + ", ".join(f"{key}={value}" for key, value in document["validation"].items())
    )
    for group in document["groups"]:
        print(f"  {group['group_id']}: {', '.join(group['hunk_ids'])}")
    if document["unresolved_questions"]:
        print(f"unresolved    : {len(document['unresolved_questions'])} question(s)")
    print(f"grouping json : {output_path}")


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):  # pragma: no cover
                reconfigure(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
