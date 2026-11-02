#!/usr/bin/env python3
"""Deterministically classify patch hunks before semantic analysis.

The classification is routing data, not a port decision. Each role declares how
the hunk should enter the later pipeline.
"""
import json
import os
import re
import sys


sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "backport_patch/tools/patch-backporting/src",
))
from tools.utils import split_patch_indexed  # noqa: E402


CODE_EXTS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".java", ".kt",
    ".kts", ".py", ".rs", ".go", ".js", ".jsx", ".ts", ".tsx", ".proto",
}
BUILD_NAMES = {
    "Android.bp", "Android.mk", "BUILD", "BUILD.bazel", "CMakeLists.txt",
    "Makefile", "meson.build", "Cargo.toml", "pom.xml", "build.gradle",
    "build.gradle.kts",
}
META_RE = re.compile(
    r"(?:^|/)(?:METADATA|OWNERS|README|CHANGELOG|LICENSE|NOTICE|VERSION)(?:\.|$)",
    re.I,
)


def hunk_file_path(hunk):
    path = str(hunk.get("file") or "").strip()
    if path and path not in {"(new file)", "(deleted file)", "/dev/null"}:
        return path
    text = str(hunk.get("text") or "")
    match = re.search(r"^\+\+\+ b/(.+)$|^--- a/(.+)$", text, re.M)
    if not match:
        return path
    return (match.group(1) or match.group(2)).strip()


def changed_lines(hunk):
    return [
        line[1:].strip()
        for line in str(hunk.get("text") or "").splitlines()
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]


def is_test_path(path):
    lower = str(path or "").lower()
    return (
        "/tests/" in lower
        or "/test/" in lower
        or bool(re.search(r"(?:Tests?|_test)\.(?:java|kt|cpp|cc|c)$", path))
        or bool(re.match(r"(?:test_.+|.+_test)\.py$", os.path.basename(lower)))
    )


def test_subtype(lines):
    meaningful = [line for line in lines if line]
    infrastructure = bool(meaningful) and all(
        line.startswith(("import ", "@Rule", "@ClassRule"))
        or re.match(
            r"^(?:public|private|protected)?\s*static\s+final\s+.*Rule\b",
            line,
        )
        for line in meaningful
    )
    if infrastructure:
        return "test_infrastructure", "SKIP"
    behavior = re.search(
        r"@Test\b|\bassert(?:That|WithMessage|Equals)?\s*\(|"
        r"\bverify\s*\(|\bwhen\s*\(|\bthenReturn\s*\(",
        "\n".join(lines),
    )
    if behavior:
        return "behavior_test", "TEST_EQUIVALENCE_REQUIRED"
    return "test_fixture_or_expected", "OPTIONAL_TEST_OBLIGATION"


def production_debug_only(lines):
    meaningful = [line for line in lines if line]
    if not meaningful:
        return False
    debug_terms = re.compile(
        r"\b(?:append|Slog\.[dviwef]|print(?:ln|f)?|log\.[dviwef]|"
        r"ProtoLog\.[dviwef])\b"
    )
    control_flow = re.compile(r"\b(?:if|else|for|while|switch|return|throw|catch)\b")
    assignment = re.compile(r"(?<![=!<>])=(?!=)")
    return (
        any(debug_terms.search(line) for line in meaningful)
        and not any(control_flow.search(line) or assignment.search(line)
                    for line in meaningful)
    )


def classify(hunk):
    path = hunk_file_path(hunk)
    lower = path.lower()
    base = os.path.basename(path)
    ext = os.path.splitext(lower)[1]
    lines = changed_lines(hunk)
    evidence = []

    if lower.endswith(".aconfig") or "/flags/" in lower:
        role, subtype, action = "config_capability", "config_declaration", "CONFIG_CAPABILITY_PIPELINE"
        evidence.append("path indicates an aconfig/flags capability")
    elif is_test_path(path):
        subtype, action = test_subtype(lines)
        role = "test_source"
        evidence.append("path/test content indicates test source")
    elif base in BUILD_NAMES:
        role, subtype, action = "build_system", "build_rule", "DEPENDENCY_REVIEW_REQUIRED"
        evidence.append("path is a build file")
    elif META_RE.search(path):
        role, subtype, action = "metadata", "document_or_ownership", "SKIP"
        evidence.append("path is metadata/documentation")
    elif "/res/" in lower or "/resources/" in lower or "/assets/" in lower:
        role, subtype, action = "resource", "resource_or_asset", "DEPENDENCY_REVIEW_REQUIRED"
        evidence.append("path is a resource/asset")
    elif re.search(r"(?:^|/)(?:generated|gen)/", lower):
        role, subtype, action = "generated", "generated_source", "SKIP"
        evidence.append("path is generated output")
    elif ext in CODE_EXTS:
        role = "production_source"
        if production_debug_only(lines):
            subtype, action = "debug_only", "SKIP_PRODUCTION"
            evidence.append("changed lines are debug output only")
        else:
            subtype, action = "behavior_or_implementation", "SEMANTIC_PIPELINE"
            evidence.append("source path requires semantic review")
    else:
        role, subtype, action = "unknown", "unclassified", "NEEDS_EVIDENCE"
        evidence.append("no deterministic classification rule matched")

    confidence = "HIGH" if action != "NEEDS_EVIDENCE" else "LOW"
    return {
        "hunk": hunk["idx"],
        "file": path,
        "role": role,
        "subtype": subtype,
        "action": action,
        "confidence": confidence,
        "evidence": evidence,
    }


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: hunk_classifier.py <patch> <out.json>")
    patch_path, out_path = sys.argv[1:]
    text = open(patch_path, encoding="utf-8", errors="replace").read()
    _, hunks = split_patch_indexed(text, True)
    rows = [classify(hunk) for hunk in hunks]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    counts = {}
    for row in rows:
        counts[row["role"]] = counts.get(row["role"], 0) + 1
    print("hunk classification:", json.dumps(counts, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
