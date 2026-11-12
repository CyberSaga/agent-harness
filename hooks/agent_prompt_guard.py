#!/usr/bin/env python3
"""PreToolUse hook (Agent|Workflow|Bash|PowerShell): the test-command rules as a refusal.

Three rules, each denying the tool call instead of relying on someone remembering it:
  - never bare `pytest`, and pytest only on the project's venv interpreter (the system
    Python can differ from the project environment and mask runtime-only bugs);
  - the whole suite only through the project's own suite command (a hang inside one pytest
    process cannot be ended from inside it);
  - subagent prompts and subagent shell calls never ask for process kills.

The pytest rules are repo-dependent, so they come from `<project>/.claude/agent-harness.json`:

    {"pytest": {"venv": ".venv", "suite_command": "scripts/run_suite.py", "suite_dir": "tests"},
     "process_kill": true}

`pytest` (object) switches the venv rules on; `venv` is the path fragment that marks a venv
interpreter (default ".venv"); `suite_command` additionally switches the whole-suite rule on and
is named in its deny text; `suite_dir` is the directory whose bare use means "whole suite".
Without a `pytest` key those rules do nothing.  The process-kill rule is on unless the file sets
`"process_kill": false`.  The project directory is CLAUDE_PROJECT_DIR, else the payload cwd.

Precision: only command position counts (line start, after a shell operator or a backtick, or
the quoted command of `-c`); quoted strings (grep patterns, sed scripts, awk programs), heredoc
bodies, `pytest.raises`, placeholders such as `<one file>` and `$PY` bound to the venv in the
same command all pass.

Scope: Bash/PowerShell commands (main thread and subagents), Agent prompts and Workflow
scripts/args.  Process kills are denied in agent prompts and in subagent shell calls; the main
thread may still end a stuck process.

Answers `{"hookSpecificOutput": {"permissionDecision": "deny", ...}}` on a violation, nothing
when clean, and fails OPEN (exit 1, stderr) on a payload it cannot read.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass

import _hookkit as kit
from _shellparse import _mask_quotes, _strip_heredocs

HOOK = "agent_prompt_guard"

# Command position: line start, after a shell operator or a backtick, or the quoted
# command of `-c` / `/c` / `-Command`.  Optionally `<runner> [-flags] -m` before pytest.
# `pytest.` (attribute), `pytest,` and `pytest)` are prose/Python, not commands.
PYTEST_RE = re.compile(
    r"""(?:^|[;&|(`]|\$\s|\bthen\s|\bdo\s|(?:-l?c|/[cC]|-Command)\s+["'])\s*"""
    r"""(?P<runner>(?:"[^"\n]*"|'[^'\n]*'|[^\s"'`;&|()]+)\s+(?:-[\w.=-]+\s+)*-m\s+)?"""
    r"""pytest\b(?![.,)])(?P<args>(?:[^\n`;&|)"']|"[^"\n]*"|'[^'\n]*')*)""",
    re.M,
)
# `$PY`, `"$PY"`, `${PY}`, `%PY%` or a bare placeholder like `PY`: the interpreter is a variable.
VAR_RUNNER_RE = re.compile(r"""^["']?\$\{?(\w+)\}?["']?$|^%(\w+)%$|^([A-Z][A-Z0-9_]*)$""")
PLACEHOLDER_RE = re.compile(r"<[^>\n]+>|\.\.\.|\$\{?\w+\}?|%\w+%|\*")
PROSE_TOKEN_RE = re.compile(r"^[A-Za-z][a-z]+$")  # `pytest lock`, `pytest is slow`
VALUE_FLAGS = {
    "-k", "-m", "-p", "-o", "-W", "-c", "-n", "--tb", "--rootdir", "--confcutdir", "--basetemp",
    "--junitxml", "--junit-xml", "--deselect", "--ignore", "--maxfail", "--durations", "--timeout",
}
KILL_RE = re.compile(
    r"""(?:^|[;&|(`]|[,:]\s|\$\s|\bthen\s|\bdo\s|\brun\s)\s*"""
    r"""(?P<cmd>taskkill\b|pkill\b|killall\b|Stop-Process\b|kill\s+-(?:9|KILL|TERM|\d+)\b)""",
    re.I | re.M,
)
NEGATION_RE = re.compile(
    r"\b(?:never|not|don'?t|dont|do\s+not|no|forbid(?:den)?|without|avoid|must\s+not)\b"
    r"|禁止|不要|不得|不可|不准|別",
    re.I,
)
SHELL_TOOLS = ("Bash", "PowerShell")
PROMPT_TOOLS = ("Agent", "Workflow")
DEFAULT_VENV = ".venv"

RULES = {
    "bare-pytest": (
        "never bare `pytest`: run it on the project venv interpreter (`{venv}`), e.g. "
        "`{venv}/bin/python -m pytest <file>` (the system Python can differ from the project environment)"
    ),
    "system-python": "pytest must run on the project venv (`{venv}`), not on the system Python",
    "whole-suite": (
        "the whole suite runs only through `{suite}` (never pytest on the suite directory or without a "
        "file target: a hang there cannot be ended from inside)"
    ),
    "process-kill": (
        "no taskkill/pkill/Stop-Process in agent work: run one test process at a time in the foreground "
        "with its output sent to a file, and report a hang instead of killing it"
    ),
}


@dataclass(frozen=True)
class Violation:
    kind: str
    snippet: str
    message: str = ""

    def rule(self) -> str:
        return self.message or RULES[self.kind]


@dataclass(frozen=True)
class PytestRules:
    """The `pytest` section of agent-harness.json."""

    venv: str = DEFAULT_VENV
    suite_command: str | None = None
    suite_dir: str | None = None

    @classmethod
    def from_config(cls, section: object) -> PytestRules | None:
        """None (rules off) unless the section is an object (or `true`)."""
        if section is True:
            section = {}
        if not isinstance(section, dict):
            return None
        venv = section.get("venv")
        suite = section.get("suite_command")
        suite_dir = section.get("suite_dir")
        return cls(
            venv=venv if isinstance(venv, str) and venv else DEFAULT_VENV,
            suite_command=suite if isinstance(suite, str) and suite else None,
            suite_dir=suite_dir.strip("/\\") if isinstance(suite_dir, str) and suite_dir.strip("/\\") else None,
        )

    def suite_dir_re(self) -> re.Pattern[str] | None:
        if not self.suite_dir:
            return None
        return re.compile(rf"^\.?[/\\]?{re.escape(self.suite_dir)}[/\\]*$")

    def message(self, kind: str) -> str:
        return RULES[kind].format(venv=self.venv, suite=self.suite_command or "")


# --------------------------------------------------------------------------- text shaping


def _shell_text(command: str) -> tuple[str, str]:
    """(text for the pytest rule, text for the kill rule) of a shell command."""
    base = _strip_heredocs(command)
    return _mask_quotes(base, keep_command_args=True), _mask_quotes(base, keep_command_args=False)


# --------------------------------------------------------------------------- pytest rule


def _interpreter(runner: str) -> str:
    """The interpreter token of `<runner> [-flags] -m` (quoted path kept whole)."""
    runner = runner.strip()
    if runner[:1] in "\"'":
        end = runner.find(runner[0], 1)
        return runner[: end + 1] if end != -1 else runner
    return runner.split()[0] if runner.split() else runner


def _runner_kind(runner: str, text: str, venv: str) -> str:
    """'venv' | 'system' | 'unknown' for the interpreter in front of `-m pytest`."""
    interp = _interpreter(runner)
    if venv in interp:
        return "venv"
    m = VAR_RUNNER_RE.match(interp)
    if not m:
        return "system"
    name = next(g for g in m.groups() if g)
    # The value runs to the closing quote or the next shell operator, not the next space: the
    # interpreter path can contain spaces, or be computed (`Join-Path $root '.venv\Scripts\python.exe'`).
    assign = re.search(
        rf"""(?m)(?:^|[;&|\s])(?:export\s+|set\s+|\$)?{re.escape(name)}\s*=\s*("[^"\n]*"|'[^'\n]*'|[^;\n&|]+)""",
        text,
    )
    if assign is None:
        return "unknown"
    return "venv" if venv in assign.group(1) else "system"


def _is_target(token: str, suite_dir: str | None) -> bool:
    t = token.strip("\"'")
    if not t or t.startswith("-") or t in (">", ">>", "<", "2>", "1>"):
        return False
    if re.fullmatch(r"_+", t):
        return True  # a masked quoted argument: a path, as far as this hook can tell
    return t.endswith(".py") or "::" in t or bool(suite_dir and suite_dir in t) or bool(PLACEHOLDER_RE.search(t))


def _targets(args: str, suite_dir: str | None) -> list[str]:
    out: list[str] = []
    prev = ""
    for token in args.split():
        if prev in VALUE_FLAGS:
            prev = token
            continue
        if _is_target(token, suite_dir):
            out.append(token.strip("\"'"))
        prev = token
    return out


def _pytest_violations(
    text: str, rules: PytestRules, original: str | None = None, prose: bool = False
) -> list[Violation]:
    """*text* may be the quote-masked command; *original* (same length) supplies the
    real runner so a quoted venv path or `"$PY"` is judged on its content.  With
    *prose* (agent prompts, workflow scripts) a match negated earlier on its line
    ("never `pytest tests/`") is the rule being restated, not broken, and a
    variable runner (`${PY}`) is not judged."""
    source = original if original is not None else text
    suite_re = rules.suite_dir_re()
    out: list[Violation] = []
    for m in PYTEST_RE.finditer(text):
        runner = source[m.start("runner"):m.end("runner")].strip() if m.group("runner") else ""
        args = m.group("args") or ""
        tokens = args.split()
        first = tokens[0].strip("\"'") if tokens else ""
        if first and first != rules.suite_dir and PROSE_TOKEN_RE.match(first):
            continue  # `pytest lock`, `pytest is slow`: words, not a command line (the suite dir is not a word)
        if prose:
            line_start = text.rfind("\n", 0, m.start()) + 1
            if NEGATION_RE.search(text[line_start:m.start()]):
                continue
        snippet = (((runner + " ") if runner else "") + "pytest" + args.rstrip()).strip()[:160]
        if not runner:
            out.append(Violation("bare-pytest", snippet, rules.message("bare-pytest")))
        elif _runner_kind(runner, source, rules.venv) == "system" and not (
            prose and VAR_RUNNER_RE.match(_interpreter(runner))
        ):
            out.append(Violation("system-python", snippet, rules.message("system-python")))
        if rules.suite_command is None:
            continue
        targets = _targets(args, rules.suite_dir)
        placeholder = bool(PLACEHOLDER_RE.search(args))
        if (not targets and not placeholder) or (suite_re and any(suite_re.match(t) for t in targets)):
            out.append(Violation("whole-suite", snippet, rules.message("whole-suite")))
    return out


# --------------------------------------------------------------------------- kill rule


def _kill_violations(text: str, negation_lifts: bool) -> list[Violation]:
    out: list[Violation] = []
    for m in KILL_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start("cmd")) + 1
        before = text[line_start:m.start("cmd")]
        if negation_lifts and NEGATION_RE.search(before):
            continue
        line_end = text.find("\n", m.end())
        snippet = text[line_start:line_end if line_end != -1 else None].strip()[:160]
        out.append(Violation("process-kill", snippet, RULES["process-kill"]))
    return out


# --------------------------------------------------------------------------- entry points


def _string_leaves(value: object) -> list[str]:
    """Every string inside a JSON-like value, one per line, so a command passed as
    a workflow arg is seen in command position."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _string_leaves(v)]
    if isinstance(value, list):
        return [s for v in value for s in _string_leaves(v)]
    return []


def _prompt_text(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Agent":
        return "\n".join(str(tool_input.get(k) or "") for k in ("prompt", "description"))
    parts = [str(tool_input.get("script") or "")]
    parts.extend(_string_leaves(tool_input.get("args")))
    return "\n".join(parts)


def violations(
    tool_name: str,
    tool_input: dict,
    in_subagent: bool = False,
    pytest_cfg: dict | None = None,
    kill_rule: bool = True,
) -> list[Violation]:
    """Every rule the tool call breaks (empty when clean).  *pytest_cfg* is the `pytest`
    section of agent-harness.json; None switches the pytest rules off."""
    rules = PytestRules.from_config(pytest_cfg) if pytest_cfg is not None else None
    found: list[Violation] = []
    if tool_name in SHELL_TOOLS:
        command = str(tool_input.get("command") or "")
        if command:
            pytest_text, kill_text = _shell_text(command)
            if rules is not None:
                found = _pytest_violations(pytest_text, rules, original=_strip_heredocs(command))
            if in_subagent and kill_rule:
                found += _kill_violations(kill_text, negation_lifts=False)
    elif tool_name in PROMPT_TOOLS:
        text = _prompt_text(tool_name, tool_input)
        if text.strip():
            if rules is not None:
                found = _pytest_violations(text, rules, prose=True)
            if kill_rule:
                found += _kill_violations(text, negation_lifts=True)
    seen: dict[str, Violation] = {}
    for v in found:
        seen.setdefault(v.kind, v)
    return list(seen.values())


def main() -> int:
    payload = kit.read_payload()
    if payload is None:
        return kit.fail_open(HOOK, "hook input is not JSON")
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return kit.fail_open(HOOK, "hook input has no tool_input object")
    try:
        config = kit.load_config(kit.project_dir(payload))
        section = config.get("pytest")
        found = violations(
            tool_name,
            tool_input,
            in_subagent=kit.is_subagent(payload),
            pytest_cfg=section if isinstance(section, dict) or section is True else None,
            kill_rule=config.get("process_kill", True) is not False,
        )
    except Exception as exc:
        return kit.fail_open(HOOK, f"{exc.__class__.__name__}: {exc}")
    if not found:
        return 0
    lines = [f"{HOOK}: this {tool_name} call breaks the agent test-command rules:"]
    for v in found:
        lines.append(f"- {v.rule()}\n    offending: {v.snippet}")
    lines.append("Rewrite the command/prompt; these rules are not negotiable inside agents.")
    return kit.deny("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
