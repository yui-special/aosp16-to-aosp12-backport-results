#!/usr/bin/env python
"""Baseline vs treatment harness: run PortGPT with and without the grouped hunks.

    python scripts/portgpt_compare.py --portgpt-root DIR --portgpt-config config.yml \
        [--spec <hunk_groups.json> --merged-hunks <merged_hunks/>] [--dry-run]

Baseline  = PortGPT's legacy behaviour: no group file, hunks ported one by one.
Treatment = the same run with this agent's `hunk_groups.json` + `merged_hunks/`
            placed where PortGPT auto-detects them.

The harness never modifies PortGPT's code: it moves the grouping inputs in and out
of `patch_dataset_dir`, runs `backporting.py` as a subprocess, and collects the
per-group results plus the outcome markers from the log.  `--dry-run` only
pre-flights the environment, which is also the fastest way to see what is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from patch_hunk_agent.config import load_yaml  # noqa: E402
from patch_hunk_agent.utils import git_utils  # noqa: E402
from patch_hunk_agent.utils.json_utils import save_json  # noqa: E402

REQUIRED_MODULES = ("yaml", "git", "httpx", "langchain", "langchain_openai", "langchain_core")
SUCCESS_MARKERS = (
    "Successfully backport the patch to the target release",
    "Aplly all hunks in the patch      PASS",
)
FAILURE_MARKERS = ("Failed backport the patch", "Failed to backport", "Reach max_iterations", "Traceback")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--portgpt-root", required=True)
    parser.add_argument("--portgpt-config", required=True, help="PortGPT CVE config yml")
    parser.add_argument("--spec", default=None, help="hunk_groups.json produced by this agent")
    parser.add_argument("--merged-hunks", default=None, help="merged_hunks/ directory produced by this agent")
    parser.add_argument("--out", default=None, help="results directory (default results/compare-<tag>)")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--skip-treatment", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="pre-flight only, run nothing")
    return parser


def preflight(args) -> tuple[dict, list[str]]:
    """Collect everything the two runs need; return (info, blocking_problems)."""
    info: dict = {"portgpt_root": args.portgpt_root, "config": args.portgpt_config}
    problems: list[str] = []
    root = Path(args.portgpt_root)
    entry = root / "backporting.py"
    if not entry.is_file():
        problems.append(f"PortGPT entry point missing: {entry}")
    config_path = Path(args.portgpt_config)
    if not config_path.is_file():
        problems.append(f"PortGPT config missing: {config_path}")
        return info, problems

    raw = load_yaml(config_path) or {}
    info["project"] = raw.get("project")
    info["tag"] = raw.get("tag")
    dataset_dir = Path(str(raw.get("patch_dataset_dir") or "").replace("/", os.sep))
    project_dir = Path(str(raw.get("project_dir") or "").replace("/", os.sep))
    info["patch_dataset_dir"] = str(dataset_dir)
    info["project_dir"] = str(project_dir)
    if not dataset_dir.is_dir():
        problems.append(f"patch_dataset_dir does not exist: {dataset_dir}")
    if not project_dir.is_dir():
        problems.append(f"project_dir does not exist: {project_dir}")
    elif not git_utils.is_git_repo(project_dir):
        problems.append(f"project_dir is not a git repository: {project_dir}")
    else:
        for field in ("new_patch", "new_patch_parent", "target_release"):
            revision = str(raw.get(field) or "")
            if revision and git_utils.git_rev_parse(project_dir, revision) is None:
                problems.append(f"commit {field}={revision[:12]}… is not in {project_dir}")

    # NOTE: `importlib.util` must be imported explicitly, and an empty stdout means
    # the probe itself failed - reporting "nothing missing" in that case would be a
    # silent false pass, so treat it as a problem.
    probe = (
        "import importlib.util, json;"
        f"mods={list(REQUIRED_MODULES)!r};"
        "print(json.dumps([m for m in mods if importlib.util.find_spec(m) is None]))"
    )
    try:
        completed = subprocess.run(
            [args.python, "-c", probe], capture_output=True, text=True, encoding="utf-8", timeout=120
        )
        out = (completed.stdout or "").strip()
        if out:
            missing = json.loads(out.splitlines()[-1])
        else:
            missing = list(REQUIRED_MODULES)
            info["probe_error"] = (completed.stderr or "probe produced no output").strip()[:400]
    except Exception as exc:  # noqa: BLE001
        missing, completed = list(REQUIRED_MODULES), None
        info["probe_error"] = str(exc)
    info["missing_modules"] = missing
    if missing:
        problems.append(
            f"PortGPT dependencies missing for {args.python}: {', '.join(missing)} "
            f"(install them into that interpreter)"
        )
    for label, path in (("spec", args.spec), ("merged_hunks", args.merged_hunks)):
        if path and not Path(path).exists():
            problems.append(f"--{label.replace('_', '-')} not found: {path}")
    return info, problems


def _log_markers(text: str) -> dict:
    return {
        "success": [marker for marker in SUCCESS_MARKERS if marker in text],
        "failure": [marker for marker in FAILURE_MARKERS if marker in text],
    }


def run_variant(
    args, label: str, *, dataset_dir: Path, out_dir: Path, with_grouping: bool
) -> dict:
    """Run PortGPT once, with the grouping inputs placed (or hidden) as requested."""
    staging = out_dir / f"{label}-staging"
    staging.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []

    def stash(path: Path) -> None:
        if path.exists():
            target = staging / path.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(path), str(target))
            moved.append((path, target))

    def restore() -> None:
        for original, target in moved:
            if target.exists() and not original.exists():
                shutil.move(str(target), str(original))

    try:
        # Hide any grouping inputs so the baseline really is the legacy path.
        for candidate in sorted(dataset_dir.glob("*hunk*groups*.json")):
            if not with_grouping:
                stash(candidate)
        merged_dir = dataset_dir / "merged_hunks"
        if not with_grouping and merged_dir.is_dir():
            stash(merged_dir)
        if with_grouping:
            if args.spec:
                shutil.copy2(args.spec, dataset_dir / Path(args.spec).name)
            if args.merged_hunks:
                target = merged_dir
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(args.merged_hunks, target)

        command = [args.python, str(Path(args.portgpt_root) / "backporting.py"), "-c", str(args.portgpt_config)]
        log_path = out_dir / f"{label}.log"
        started = time.perf_counter()
        with log_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("$ " + " ".join(command) + "\n\n")
            handle.flush()
            try:
                completed = subprocess.run(
                    command, cwd=str(args.portgpt_root), stdout=handle, stderr=subprocess.STDOUT,
                    timeout=args.timeout, check=False,
                )
                returncode = completed.returncode
            except subprocess.TimeoutExpired:
                returncode = None
                handle.write(f"\n[timeout after {args.timeout}s]\n")
        duration = round(time.perf_counter() - started, 3)
    finally:
        restore()

    text = log_path.read_text(encoding="utf-8", errors="replace")
    ported = sorted(
        path.name
        for path in (dataset_dir / "merged_hunks" / "ported").glob("*.patch")
        if path.is_file()
    )
    return {
        "label": label,
        "grouping_input": with_grouping,
        "command": command,
        "returncode": returncode,
        "duration_s": duration,
        "log": str(log_path),
        "markers": _log_markers(text),
        "ported_files": ported,
        "ported_count": len(ported),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    info, problems = preflight(args)
    print("== pre-flight ==")
    for key, value in info.items():
        print(f"  {key}: {value}")
    if problems:
        print("\n== blocking problems ==")
        for problem in problems:
            print(f"  - {problem}")
    if problems or args.dry_run:
        print("\n(dry run: nothing executed)" if args.dry_run else "\n(fix the problems above, then rerun)")
        return 2 if problems else 0

    dataset_dir = Path(info["patch_dataset_dir"])
    out_dir = Path(args.out) if args.out else ROOT / "results" / f"compare-{info.get('tag') or 'run'}"
    out_dir.mkdir(parents=True, exist_ok=True)
    save_json(out_dir / "preflight.json", {"info": info, "problems": problems})

    runs = []
    if not args.skip_baseline:
        print("[baseline] PortGPT without grouping …", flush=True)
        runs.append(run_variant(args, "baseline", dataset_dir=dataset_dir, out_dir=out_dir, with_grouping=False))
    if not args.skip_treatment:
        print("[treatment] PortGPT with the grouped hunks …", flush=True)
        runs.append(run_variant(args, "treatment", dataset_dir=dataset_dir, out_dir=out_dir, with_grouping=True))

    comparison = {
        "project": info.get("project"),
        "tag": info.get("tag"),
        "patch_dataset_dir": info["patch_dataset_dir"],
        "runs": runs,
        "verdict": _verdict(runs),
    }
    save_json(out_dir / "comparison.json", comparison)
    print("\n== comparison ==")
    for run in runs:
        print(
            f"  {run['label']:<10} rc={run['returncode']!s:<4} "
            f"groups_ported={run['ported_count']:<3} duration={run['duration_s']!s:<8} "
            f"success_markers={len(run['markers']['success'])} "
            f"failure_markers={len(run['markers']['failure'])}"
        )
    print(f"  verdict: {comparison['verdict']}")
    print(f"  artifacts: {out_dir}")
    return 0


def _verdict(runs: list[dict]) -> dict:
    """Only claims what the logs support: PortGPT prints the outcome itself."""
    def status(run: dict) -> str:
        markers = run["markers"]
        if markers["success"] and not markers["failure"]:
            return "success"
        if markers["success"] and markers["failure"]:
            return "mixed"
        if markers["failure"]:
            return "failed"
        return "unknown"

    by_label = {run["label"]: run for run in runs}
    verdict = {label: status(run) for label, run in by_label.items()}
    baseline, treatment = by_label.get("baseline"), by_label.get("treatment")
    if baseline and treatment:
        verdict["ported_delta"] = treatment["ported_count"] - baseline["ported_count"]
        improvement = {
            "improved": verdict.get("treatment") == "success" and verdict.get("baseline") != "success",
            "regressed": verdict.get("baseline") == "success" and verdict.get("treatment") != "success",
        }
        verdict["outcome_change"] = improvement
    verdict["note"] = (
        "Outcome is read from PortGPT's own log markers and the ported/ directory; "
        "compile/test rates come from the run's own validate/PoC step."
    )
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
