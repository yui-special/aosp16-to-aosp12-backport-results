"""Read-only git helpers.

Only read-only subcommands are used (``rev-parse``, ``show``, ``grep``,
``ls-tree``, ``cat-file``).  The agent must never modify a repository.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitResult",
    "git_available",
    "is_git_repo",
    "run_git",
    "git_rev_parse",
    "git_show",
    "git_file_exists",
    "git_grep",
]

READ_ONLY_SUBCOMMANDS = {
    "rev-parse",
    "show",
    "grep",
    "ls-tree",
    "cat-file",
    "log",
    "status",
    "diff",
    "describe",
    "symbolic-ref",
}


@dataclass
class GitResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def git_available() -> bool:
    return shutil.which("git") is not None


def _no_window_kwargs() -> dict:
    # Avoid flashing console windows on Windows when called from a GUI process.
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def run_git(args: list[str], cwd: str | Path | None = None, timeout: float = 30.0) -> GitResult:
    """Run ``git <args>`` and capture output.  Never raises for git failures."""
    if args and args[0] not in READ_ONLY_SUBCOMMANDS:
        raise ValueError(f"refusing to run non read-only git subcommand: {args[0]!r}")
    command = ["git"] + list(args)
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_no_window_kwargs(),
        )
    except FileNotFoundError:
        return GitResult(command, 127, "", "git executable not found")
    except subprocess.TimeoutExpired:
        return GitResult(command, 124, "", f"git timed out after {timeout}s", timed_out=True)
    except OSError as exc:  # pragma: no cover - platform specific
        return GitResult(command, 126, "", str(exc))
    return GitResult(command, completed.returncode, completed.stdout, completed.stderr)


def is_git_repo(path: str | Path) -> bool:
    target = Path(path)
    if not target.exists():
        return False
    return run_git(["rev-parse", "--git-dir"], cwd=target).ok


def git_rev_parse(repo: str | Path, rev: str) -> str | None:
    result = run_git(["rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=repo)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def git_show(repo: str | Path, rev: str, path: str) -> str | None:
    """Return file content at ``rev`` or ``None`` when it does not exist."""
    result = run_git(["show", f"{rev}:{path}"], cwd=repo)
    if not result.ok:
        return None
    return result.stdout


def git_file_exists(repo: str | Path, rev: str, path: str) -> bool:
    result = run_git(["cat-file", "-e", f"{rev}:{path}"], cwd=repo)
    return result.ok


def git_grep(
    repo: str | Path,
    rev: str,
    pattern: str,
    *,
    path: str | None = None,
    max_results: int = 50,
    fixed_string: bool = False,
    timeout: float = 30.0,
) -> tuple[list[dict], str | None]:
    """Search ``rev`` for ``pattern``.  Returns ``(matches, error)``.

    Each match is ``{"file_path", "line", "text"}``.  ``git grep`` exit code 1
    (no match) is not an error.
    """
    args = ["grep", "-n", "-I"]
    args.append("-F" if fixed_string else "-E")
    args += ["-e", pattern, rev]
    if path:
        args += ["--", path]
    result = run_git(args, cwd=repo, timeout=timeout)
    if result.returncode not in (0, 1):
        return [], (result.stderr.strip() or f"git grep failed with rc={result.returncode}")

    matches: list[dict] = []
    prefix = f"{rev}:"
    for raw in result.stdout.splitlines():
        line = raw
        if line.startswith(prefix):
            line = line[len(prefix) :]
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path, line_no, text = parts
        try:
            lineno = int(line_no)
        except ValueError:
            continue
        matches.append({"file_path": file_path, "line": lineno, "text": text})
        if len(matches) >= max_results:
            break
    return matches, None
