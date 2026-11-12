"""Shell-text helpers shared by the shell-command hooks (stdlib only).

Hooks judge a command only where a command can be: quoted strings (grep patterns, sed scripts, awk
programs) and heredoc bodies are data, so they are masked or dropped before any pattern runs.
"""
from __future__ import annotations

import re

HEREDOC_RE = re.compile(
    r"""<<-?\s*(['"]?)(\w+)\1[^\n]*\n.*?(?:\n\s*\2\s*$|\Z)|@(['"])\s*\n.*?\n\s*\3@""",
    re.S | re.M,
)
COMMAND_ARG_FLAGS = ("-c", "-lc", "/c", "/C", "-Command", "-command", "-e")


def _strip_heredocs(text: str) -> str:
    """Drop heredoc / here-string bodies (file contents written through the shell)."""
    return HEREDOC_RE.sub(lambda m: m.group(0).split("\n", 1)[0] + "\n", text)


def _mask_quotes(text: str, keep_command_args: bool) -> str:
    """Replace the inside of quoted strings with `_` (grep patterns, sed scripts, awk
    programs are data, not commands).  With *keep_command_args*, the quoted string
    right after `-c` / `/c` / `-Command` is kept: that one IS a command."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch not in "\"'":
            i += 1
            continue
        before = text[:i].rstrip()
        keep = keep_command_args and any(before.endswith(flag) for flag in COMMAND_ARG_FLAGS)
        j = i + 1
        while j < n and text[j] != ch and text[j] != "\n":
            if ch == '"' and text[j] == "\\" and j + 1 < n:
                j += 1
            j += 1
        if not keep:
            for k in range(i + 1, min(j, n)):
                if text[k] != "\n":
                    out[k] = "_"
        i = j + 1
    return "".join(out)
