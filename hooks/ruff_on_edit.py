#!/usr/bin/env python3
"""Ruff-check Python files right after Claude edits them, through any tool.

Registered on two events:
  PostToolUse Edit|Write: ruff-check the edited file (tool_input.file_path)
      when it lies inside a git tree; scratch files outside any git tree are skipped.
  PreToolUse Bash|PowerShell: stamp the start time of the shell call
      (one file per tool_use_id under CLAUDE_RUFF_STAMP_DIR, default
      <tempdir>/claude_ruff_on_shell; stamps older than a day are pruned).
  PostToolUse Bash|PowerShell: ruff-check every .py file in the git tree of
      the call's cwd that changed since that stamp (tracked or untracked, by
      mtime), so sed -i, redirects and Python bulk-edit scripts are linted
      like Edit/Write.  No stamp, no git tree or no changed .py: silent exit 0.

The project root is CLAUDE_PROJECT_DIR, else the payload's cwd (the Edit/Write leg falls
back to the edited file's git root).  The hook does nothing unless that project (1) opted in
with `<project>/.claude/agent-harness.json` (any JSON object, `{}` is enough), (2) has a ruff
config (`ruff.toml`, `.ruff.toml`, or a `[tool.ruff]` table in pyproject.toml, in the root
or at the root of its git tree) and (3) a ruff executable is found: `<project>/.venv/Scripts/ruff.exe`,
`<project>/.venv/bin/ruff`, else ruff on PATH.  A project without ruff is not held to it.

Exit codes (Claude Code hook contract):
  0 - clean file, a non-Python file, or nothing to do (no config, no ruff)
  2 - ruff findings; stderr is fed back to the agent to fix at edit time
  1 - hook could not run ruff (timeout, crash), or the hook payload itself was
      corrupt/malformed; stderr warns the user so a broken lint gate is visible
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import _hookkit as kit

SHELL_TOOLS = ("Bash", "PowerShell")
STAMP_MAX_AGE = 86400.0
MTIME_SLACK = 0.5  # seconds; filesystem timestamp granularity
CONFIG_FILES = ("ruff.toml", ".ruff.toml")
PYPROJECT_RUFF_RE = re.compile(r"^\s*\[tool\.ruff[\].]", re.M)


def stamp_dir() -> Path:
    override = os.environ.get("CLAUDE_RUFF_STAMP_DIR")
    return Path(override) if override else Path(tempfile.gettempdir()) / "claude_ruff_on_shell"


def stamp_path(payload: dict, directory: Path) -> Path | None:
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    key = f"{payload.get('session_id') or 'nosession'}_{tool_use_id}"
    return directory / (re.sub(r"[^\w.-]", "_", key) + ".stamp")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result if result.returncode == 0 else None


def _git_root(path: Path) -> Path | None:
    top = _git(path, "rev-parse", "--show-toplevel")
    return Path(top.stdout.decode("utf-8", "replace").strip()) if top is not None else None


def _dir_has_ruff_config(directory: Path) -> bool:
    if any((directory / name).is_file() for name in CONFIG_FILES):
        return True
    try:
        return bool(PYPROJECT_RUFF_RE.search((directory / "pyproject.toml").read_text(encoding="utf-8-sig")))
    except (OSError, UnicodeDecodeError):
        return False


def has_ruff_config(project: Path) -> bool:
    """True when *project* (or the root of its git tree) carries a ruff config."""
    if not project.is_dir():
        return False
    if _dir_has_ruff_config(project):
        return True
    root = _git_root(project)
    return root is not None and root != project and _dir_has_ruff_config(root)


def resolve_ruff(project: Path) -> str | None:
    section = kit.load_config(project).get("pytest")
    venv = section.get("venv") if isinstance(section, dict) else None
    venv_dir = venv if isinstance(venv, str) and venv else ".venv"
    for candidate in (
        project / venv_dir / "Scripts" / "ruff.exe",  # Windows venv
        project / venv_dir / "bin" / "ruff",  # POSIX venv
    ):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ruff")


def lint_enabled(project: Path) -> bool:
    """The repo opted in (agent-harness.json present) and carries a ruff config."""
    return kit.has_config(project) and has_ruff_config(project)


def record_start(payload: dict) -> int:
    if not lint_enabled(kit.project_dir(payload)):
        return 0  # nothing will be linted for this call, so no stamp
    directory = stamp_dir()
    path = stamp_path(payload, directory)
    if path is None:
        return 0
    try:
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old in directory.glob("*.stamp"):
            try:
                if now - old.stat().st_mtime > STAMP_MAX_AGE:
                    old.unlink()
            except OSError:
                pass
        path.write_text(repr(now), encoding="ascii")
    except OSError:
        pass  # no stamp only means this call is not linted
    return 0


def changed_py_files(cwd: Path, since: float) -> list[Path]:
    """.py files in cwd's git tree (tracked or untracked) modified at or after since."""
    root = _git_root(cwd)
    if root is None:
        return []
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "*.py")
    if status is None:
        return []
    fields = status.stdout.decode("utf-8", "replace").split("\0")
    found: list[Path] = []
    skip_next = False
    for field in fields:
        if skip_next:
            skip_next = False
            continue
        if len(field) < 4:
            continue
        code, rel = field[:2], field[3:]
        if code[0] in "RC":
            skip_next = True  # -z puts the rename source in the next field
        if not rel.endswith(".py"):
            continue
        path = root / rel
        try:
            if path.is_file() and path.stat().st_mtime >= since - MTIME_SLACK:
                found.append(path)
        except OSError:
            continue
    return found


def shell_changed_files(payload: dict) -> list[Path]:
    """Consume the PreToolUse stamp of this shell call and return the .py files
    changed since it ([] when there is no stamp)."""
    path = stamp_path(payload, stamp_dir())
    if path is None:
        return []
    try:
        since = float(path.read_text(encoding="ascii"))
        path.unlink()
    except (OSError, ValueError):
        return []
    cwd = Path(str(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or "."))
    return changed_py_files(cwd, since)


def shell_post(payload: dict) -> int:
    project = kit.project_dir(payload)
    if not lint_enabled(project):
        return 0
    files = shell_changed_files(payload)
    if not files:
        return 0
    ruff = resolve_ruff(project)
    if ruff is None:
        return 0  # no ruff here: the project is not held to it
    try:
        result = subprocess.run(
            [ruff, "check", "--quiet", "--force-exclude", *map(str, files)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(files[0].parent),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"ruff_on_edit hook: failed to run {ruff} ({exc.__class__.__name__}) - "
            f"the .py files this shell call changed were NOT lint-checked.",
            file=sys.stderr,
        )
        return 1
    if result.returncode == 0:
        return 0
    if result.returncode == 1 and result.stdout.strip():
        tool = payload.get("tool_name") or "shell"
        print(f"ruff findings in .py files changed by this {tool} call:\n{result.stdout.strip()}", file=sys.stderr)
        return 2
    detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
    print(
        f"ruff_on_edit hook: ruff exited {result.returncode} without findings - "
        f"the .py files this shell call changed were NOT lint-checked.\n{detail}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    try:
        # utf-8-sig: tolerate a BOM (PowerShell pipes prepend one)
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        print(
            f"ruff_on_edit hook: hook input format corrupted ({exc.__class__.__name__}), "
            f"lint defenses may fail - this edit was NOT lint-checked.",
            file=sys.stderr,
        )
        return 1

    if isinstance(payload, dict) and payload.get("tool_name") in SHELL_TOOLS:
        if payload.get("hook_event_name") == "PreToolUse":
            return record_start(payload)
        return shell_post(payload)

    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool_input, dict) or "file_path" not in tool_input:
        print(
            "ruff_on_edit hook: hook input format corrupted, lint defenses may fail - "
            "this edit was NOT lint-checked.",
            file=sys.stderr,
        )
        return 1

    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".py"):
        return 0
    path = Path(file_path)
    if not path.is_file():
        return 0
    file_root = _git_root(path.parent)
    if file_root is None:
        return 0  # a scratch file outside any git tree is not held to a lint baseline (as in the shell leg)

    project = file_root  # opt-in is judged where the file lives, not in the session's project dir
    if not lint_enabled(project):
        return 0
    ruff = resolve_ruff(project)
    if ruff is None:
        return 0  # no ruff here: the project is not held to it

    try:
        result = subprocess.run(
            [ruff, "check", "--quiet", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"ruff_on_edit hook: failed to run {ruff} ({exc.__class__.__name__}) - "
            f"this edit was NOT lint-checked.",
            file=sys.stderr,
        )
        return 1

    if result.returncode == 0:
        return 0
    # ruff check contract: 1 = violations found (findings on stdout),
    # 2+ = tool/config error. Anything but a clean lint-findings case is a
    # broken lint check and must be surfaced, not swallowed.
    if result.returncode == 1 and result.stdout.strip():
        print(f"ruff findings in {path.name}:\n{result.stdout.strip()}", file=sys.stderr)
        return 2
    detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
    print(
        f"ruff_on_edit hook: ruff exited {result.returncode} without findings - "
        f"this edit was NOT lint-checked.\n{detail}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
