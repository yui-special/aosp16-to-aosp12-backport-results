#!/usr/bin/env python
"""Generic driver: any patch in -> grouping + PortGPT artifacts + manifest out.

    python scripts/run_grouping.py --patch <file.diff> [options]

Everything patch-specific comes from arguments (or from a PortGPT config.yml you
point at); nothing is hard-coded to one CVE or one machine.

Common invocations::

    # offline plumbing check, no network
    python scripts/run_grouping.py --patch tests/fixtures/simple_dependency.diff \
        --provider mock-heuristic --grouping-mode components

    # real model (credentials come from .env), PortGPT-compatible outputs
    python scripts/run_grouping.py --patch /path/to/fix.patch \
        --portgpt-root /path/to/portgpt-src-modified \
        --portgpt-config /path/to/portgpt/config.yml \
        --out results/<name>

    # variance measurement (same input N times) and scoring against a reference
    python scripts/run_grouping.py --patch fix.patch --repeat 3 --dataset ref/dataset.json

Outputs land in ``--out`` (default ``results/<patch_id>``):
  grouping.json / analysis.json / tasks.json / result.json, run.jsonl,
  hunk_groups.json + merged_hunks/ (when --portgpt-root is given), manifest.json,
  and evaluation.json (when --dataset is given).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(SRC), str(ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

from patch_hunk_agent import PACKAGE_VERSION  # noqa: E402
from patch_hunk_agent.cli import main as cli_main  # noqa: E402
from patch_hunk_agent.config import load_env_file, load_yaml  # noqa: E402
from patch_hunk_agent.evaluator import Evaluator  # noqa: E402
from patch_hunk_agent.utils.json_utils import load_json, save_json  # noqa: E402
from score_grouping import prediction_from_result  # noqa: E402

PROMPT_FILES = ("dependency_analysis.txt", "evidence_extraction.txt", "global_partition.txt")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patch", required=True, help="any unified diff")
    parser.add_argument("--patch-id", default=None, help="defaults to the patch file stem")
    parser.add_argument("--out", default=None, help="output directory (default results/<patch_id>)")
    parser.add_argument("--config", default=None, help="agent config (default configs/default.yaml)")
    parser.add_argument("--provider", default=None, help="provider name (mock / mock-heuristic / openai / any name with an endpoint)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--grouping-mode", default=None, choices=["global", "components"])
    parser.add_argument("--portgpt-root", default=None, help="PortGPT tree: enables alignment + spec + merged hunks")
    parser.add_argument("--portgpt-config", default=None, help="PortGPT config.yml: supplies endpoint/model/key/paths")
    parser.add_argument("--repo", default=None, help="git checkout usable as source+target context")
    parser.add_argument("--source-revision", default=None)
    parser.add_argument("--target-revision", default=None)
    parser.add_argument("--source-tree", default=None)
    parser.add_argument("--target-tree", default=None)
    parser.add_argument("--dataset", default=None, help="reference dataset (reference_groups) for scoring")
    parser.add_argument("--repeat", type=int, default=1, help="run N times to measure variance")
    parser.add_argument("--no-tasks", action="store_true")
    parser.add_argument("--extra-arg", action="append", default=[], help="extra arg passed to the CLI verbatim")
    return parser


def _adopt_portgpt_config(path: str, patch_dataset_dir: list[str | None]) -> dict:
    """Reuse PortGPT's own settings (endpoint/model/key) unless already configured."""
    raw = load_yaml(path) or {}
    for env_name, key in (
        ("PATCH_HUNK_AGENT_BASE_URL", "llm_base_url"),
        ("PATCH_HUNK_AGENT_MODEL", "llm_model"),
        ("PATCH_HUNK_AGENT_API_KEY", "openai_key"),
    ):
        value = str(raw.get(key) or "").strip()
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value
    if raw.get("patch_dataset_dir"):
        patch_dataset_dir[0] = str(raw["patch_dataset_dir"]).replace("/", os.sep).rstrip(os.sep)
    return raw


def _prompt_hashes() -> dict[str, str]:
    hashes = {}
    for name in PROMPT_FILES:
        path = ROOT / "prompts" / name
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return hashes


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Load .env before adopting PortGPT's settings, so an explicitly configured
    # credential always wins over whatever is written in PortGPT's config.yml.
    load_env_file()
    patch_path = Path(args.patch)
    if not patch_path.is_file():
        print(f"error: patch not found: {patch_path}", file=sys.stderr)
        return 2
    patch_id = args.patch_id or patch_path.stem
    out_dir = Path(args.out) if args.out else ROOT / "results" / patch_id
    out_dir.mkdir(parents=True, exist_ok=True)

    dataset_dir: list[str | None] = [None]
    if args.portgpt_config:
        _adopt_portgpt_config(args.portgpt_config, dataset_dir)

    spec_output = args.portgpt_root and str(
        Path(dataset_dir[0]) / "hunk_groups.json" if dataset_dir[0] else out_dir / "hunk_groups.json"
    )
    merged_dir = args.portgpt_root and str(
        Path(dataset_dir[0]) / "merged_hunks" if dataset_dir[0] else out_dir / "merged_hunks"
    )

    runs: list[dict] = []
    started = time.perf_counter()
    for attempt in range(1, max(1, args.repeat) + 1):
        grouping_out = out_dir / ("grouping.json" if args.repeat == 1 else f"grouping.rep{attempt}.json")
        result_out = out_dir / ("result.json" if args.repeat == 1 else f"result.rep{attempt}.json")
        cli_args = [
            "group",
            "--patch", str(patch_path),
            "--patch-id", patch_id,
            "--output", str(grouping_out),
            "--analysis-output", str(out_dir / ("analysis.json" if args.repeat == 1 else f"analysis.rep{attempt}.json")),
            "--record", str(out_dir / "run.jsonl"),
            "--print-json",
        ]
        if args.config:
            cli_args += ["--config", args.config]
        if args.provider:
            cli_args += ["--provider", args.provider]
        if args.model:
            cli_args += ["--model", args.model]
        if args.grouping_mode:
            cli_args += ["--grouping-mode", args.grouping_mode]
        if args.portgpt_root:
            cli_args += ["--portgpt-root", args.portgpt_root, "--portgpt-spec-output", str(spec_output), "--merged-hunks-dir", str(merged_dir)]
        for key, value in (("--repo", args.repo), ("--source-revision", args.source_revision),
                           ("--target-revision", args.target_revision), ("--source-tree", args.source_tree),
                           ("--target-tree", args.target_tree)):
            if value:
                cli_args += [key, value]
        if args.no_tasks:
            cli_args.append("--no-tasks")
        cli_args += args.extra_arg
        print(f"[run {attempt}/{args.repeat}] " + " ".join(cli_args), flush=True)
        buffer = sys.stdout
        captured = _Tee(buffer)
        sys.stdout = captured
        try:
            code = cli_main(cli_args)
        finally:
            sys.stdout = buffer
        if code != 0:
            print(f"run {attempt} exited with {code}", file=sys.stderr)
            return code
        result_out.write_text(captured.text, encoding="utf-8")
        runs.append(load_json(result_out))

    manifest = {
        "agent_version": PACKAGE_VERSION,
        "patch": {"path": str(patch_path), "sha256": hashlib.sha256(patch_path.read_bytes()).hexdigest(), "patch_id": patch_id},
        "prompt_hashes": _prompt_hashes(),
        "portgpt": {"root": args.portgpt_root, "config": args.portgpt_config,
                    "patch_dataset_dir": dataset_dir[0], "spec": str(spec_output) if spec_output else None,
                    "merged_hunks_dir": str(merged_dir) if merged_dir else None},
        "context": {"repo": args.repo, "source_revision": args.source_revision,
                    "target_revision": args.target_revision, "source_tree": args.source_tree,
                    "target_tree": args.target_tree},
        "runs": [
            {
                "grouping_mode": (run.get("grouping") or {}).get("metadata", {}).get("grouping_mode"),
                "status": (run.get("grouping_document") or {}).get("status"),
                "group_count": len((run.get("grouping_document") or {}).get("groups") or []),
                "group_sizes": sorted((len(g["hunk_ids"]) for g in (run.get("grouping_document") or {}).get("groups") or []), reverse=True),
                "tokens": ((run.get("analysis") or {}).get("metadata") or {}).get("usage"),
                "dependency_count": len((run.get("dependency_validation") or {}).get("dependencies") or []),
                "constraint_satisfaction": ((run.get("grouping") or {}).get("metadata") or {}).get("constraint_satisfaction"),
                "execution_order": ((run.get("grouping") or {}).get("metadata") or {}).get("execution_plan", {}).get("order"),
                "duration_s": (run.get("run") or {}).get("duration_s"),
            }
            for run in runs
        ],
        "model": (runs[0].get("run") or {}).get("config", {}).get("model") if runs else None,
        "wall_clock_s": round(time.perf_counter() - started, 3),
    }
    save_json(out_dir / "manifest.json", manifest)

    if args.dataset:
        dataset = load_json(args.dataset)
        report = Evaluator().evaluate_dataset(
            dataset, {"predictions": [prediction_from_result(run, patch_id)["predictions"][0] for run in runs]}
        )
        spreads = {}
        for metric in ("co_membership_recall", "over_grouping_rate", "backport_success_rate"):
            values = [
                row["grouping"][metric]
                for row in report["per_patch"]
                if isinstance(row.get("grouping"), dict) and isinstance(row["grouping"].get(metric), (int, float))
            ]
            if values:
                spreads[metric] = {"mean": round(statistics.fmean(values), 6), "min": min(values),
                                   "max": max(values), "n": len(values)}
        satisfaction = [run.get("grouping", {}).get("metadata", {}).get("constraint_satisfaction") for run in runs]
        save_json(out_dir / "evaluation.json", {"aggregate": report["aggregate"], "spread": spreads,
                                                "constraint_satisfaction": satisfaction})
        print(json.dumps({"aggregate": report["aggregate"], "spread": spreads}, ensure_ascii=False, indent=2))
    print(f"artifacts: {out_dir}")
    return 0


class _Tee:
    """Capture the CLI's JSON output while still writing it through."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._chunks: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._chunks)

    def write(self, data: str) -> int:
        self._chunks.append(data)
        return len(data)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, item):  # pragma: no cover - passthrough
        return getattr(self._stream, item)


if __name__ == "__main__":
    raise SystemExit(main())
