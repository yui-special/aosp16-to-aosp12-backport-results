#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 链完整性检查：改了方法签名后，是否还有调用点用旧参数个数。

原理（与 CVE 无关）：
  1. 从 patch 里找出「同一个方法名，在 - 行和 + 行参数个数不同」的签名变更；
  2. 在基线加补丁生成的候选树中查找调用点，数参数个数；
  3. 如果还有调用点用旧个数 -> 报 MISSING_CALLSITE。

用法: chain_check.py <repo_dir> <patch_file> [pathspec...] [--base <target_commit>]
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

SIG = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
SKIP_NAMES = {"if", "for", "while", "switch", "return", "new", "catch", "synchronized"}
DEFAULT_GLOBS = [
    "*.java", "*.kt", "*.aidl", "*.c", "*.cc", "*.cpp", "*.h", "*.hpp",
    "*.rs", "*.py",
]


def count_args(text, start):
    """从 '(' 之后开始，数顶层逗号（忽略嵌套括号/字符串）。"""
    depth = 0
    args = 0
    seen = False
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            q = ch
            i += 1
            while i < len(text) and text[i] != q:
                i += 1
        elif ch in "([{":
            depth += 1
            seen = True
        elif ch in ")]}":
            if depth == 0:
                return args + 1 if seen else 0
            depth -= 1
        elif ch == "," and depth == 0:
            args += 1
        elif ch.strip():
            seen = True
        i += 1
    return None


def signatures_from_patch(patch_text):
    """返回 {name: set(arg_counts)}，只看参数个数发生变化的方法。"""
    minus, plus = {}, {}
    lines = patch_text.split("\n")
    for idx, line in enumerate(lines):
        if len(line) < 2 or line[0] not in "+-":
            continue
        body = line[1:]
        m = SIG.search(body)
        if not m or m.group(1) in SKIP_NAMES:
            continue
        name = m.group(1)
        n = count_args(body, m.end())
        if n is None:
            # 签名可能跨行：把后面几行拼上来再数
            joined = body
            for follow in lines[idx + 1: idx + 8]:
                joined += "\n" + follow[1:] if follow[:1] in "+- " else "\n" + follow
                n = count_args(joined, m.end())
                if n is not None:
                    break
        if n is None:
            continue
        (plus if line[0] == "+" else minus).setdefault(name, set()).add(n)

    changed = {}
    for name, new in plus.items():
        old = minus.get(name)
        if old and old != new:
            changed[name] = {"old": sorted(old), "new": sorted(new)}
    return changed


def looks_like_declaration(text, match):
    """Filter declarations while remaining language-agnostic."""
    before = text[:match.start()].strip()
    if re.search(
        r"\b(public|private|protected|static|final|abstract|synchronized|"
        r"native|inline|extern)\b", text
    ):
        return True
    if not before:
        return False
    if before.endswith((".", "->", "::", "=", "return", "new")):
        return False
    # Return type/signature prefixes such as `int`, `static void`,
    # `SQLITE_PRIVATE int`, or `Result<T>` indicate declarations.
    return bool(re.match(r"^[A-Za-z_~][A-Za-z0-9_:<>,*&\[\]\s]*$", before))


def patch_candidate_tree(repo, base_ref, patch_text):
    """Apply the candidate patch to a private index and return its tree object."""
    with tempfile.TemporaryDirectory(prefix='chain-check-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(directory, 'index'))
        command = [
            'git',
            '-c',
            'safe.directory=' + os.path.abspath(repo),
            '-c',
            'core.autocrlf=false',
            '-C',
            repo,
        ]
        subprocess.run(
            command + ['read-tree', base_ref],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        applied = subprocess.run(
            command + [
                'apply', '--cached', '--recount', '-'
            ],
            input=patch_text,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if applied.returncode != 0:
            raise RuntimeError(
                (applied.stderr or applied.stdout or 'git apply failed').strip()
            )
        return subprocess.check_output(
            command + ['write-tree'],
            env=env,
            text=True,
        ).strip()


def call_sites(repo, name, pathspecs, ref=''):
    if not ref:
        raise RuntimeError('CHAIN_CHECK_TREE_REQUIRED: specify a candidate tree or target base')
    cmd = ["git", "-C", repo, "grep", "-n", "-F", name + "("]
    if ref:
        cmd.append(ref)
    cmd.append('--')
    cmd += pathspecs or DEFAULT_GLOBS
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in (0, 1):
        raise RuntimeError('Cannot inspect candidate source: ' + p.stderr.strip())
    for line in p.stdout.split("\n"):
        if ref and line.startswith(ref + ':'):
            line = line[len(ref) + 1:]
        if not line.strip():
            continue
        try:
            f, ln, text = line.split(":", 2)
        except ValueError:
            continue
        # 跳过测试代码：它们有自己的 mock/桩，签名口径不同
        if "/tests/" in f or f.endswith("Tests.java") or f.endswith("Test.java"):
            continue
        pattern = re.compile(r"(?<![A-Za-z0-9_])%s\s*\(" % re.escape(name))
        for m in pattern.finditer(text):
            if looks_like_declaration(text, m):
                continue
            n = count_args(text, m.end())
            yield f, int(ln), n, text.strip()
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo')
    parser.add_argument('patch_file')
    parser.add_argument('pathspecs', nargs='*')
    parser.add_argument('--base', help='Apply the patch to this fixed target base using a private index')
    args = parser.parse_args()
    repo, patch_file, pathspecs = args.repo, args.patch_file, args.pathspecs
    if not pathspecs:
        pathspecs = [
            item.strip() for item in os.environ.get(
                "CHAIN_CHECK_GLOBS", ",".join(DEFAULT_GLOBS)
            ).split(",") if item.strip()
        ]
    with open(patch_file, encoding='utf-8', errors='surrogateescape') as handle:
        patch_text = handle.read()
    ref = os.environ.get('CHAIN_CHECK_REF', '').strip()
    if args.base:
        # An explicit base must override any inherited candidate from an older run.
        ref = patch_candidate_tree(repo, args.base,
                                   patch_text if patch_text.endswith('\n') else patch_text + '\n')
    if not ref:
        base_ref = os.environ.get('CHAIN_CHECK_BASE_REF', '').strip()
        if not base_ref:
            print(
                "CHAIN_CHECK_TREE_REQUIRED: set CHAIN_CHECK_REF to an immutable "
                "candidate tree or CHAIN_CHECK_BASE_REF with the target base",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            ref = patch_candidate_tree(repo, base_ref,
                                       patch_text if patch_text.endswith('\n') else patch_text + '\n')
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(
                "CHAIN_CHECK_CANDIDATE_APPLY_FAILED: %s" % exc,
                file=sys.stderr,
            )
            sys.exit(2)

    # Resolve symbolic refs once, including when there are no changed signatures.
    ref = subprocess.check_output(['git', '-C', repo, 'rev-parse', '--verify', ref + '^{tree}'],
                                  text=True, stderr=subprocess.PIPE).strip()
    print('candidate_tree=' + ref)
    changed = signatures_from_patch(patch_text)
    bad = []
    for name, info in sorted(changed.items()):
        old = set(info["old"])
        new = set(info["new"])
        for f, ln, n, text in call_sites(repo, name, pathspecs, ref=ref):
            if n in old and n not in new:
                bad.append((name, f, ln, n, sorted(new), text))
    print("=== 签名变更（%d 个）===" % len(changed))
    for name, info in sorted(changed.items()):
        print("  %-40s old=%s new=%s" % (name, info["old"], info["new"]))
    print("\n=== 仍用旧参数个数的调用点（%d）===" % len(bad))
    for name, f, ln, n, new, text in bad:
        print("  %s:%d  %s(…%d 个参数)，应为 %s" % (f, ln, name, n, new))
        print("      %s" % text[:110])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('CHAIN_CHECK_ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(2)
