"""hooks/hooks.json and hooks/*.py must agree: every entry script is registered, every registered script exists."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
HOOKS = ROOT / "hooks"
SCRIPT_RE = re.compile(r'^python "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/(?P<script>[\w.]+\.py)"(?P<args>(?: --[\w-]+ [\w-]+)*)$')
# Library modules (underscore prefix) and rule libraries imported by a registered hook are not entry scripts.
LIBRARIES = {"shell_read_guard.py"}


def _registrations() -> list[tuple[str, str, str, str, int]]:
    """(event, matcher, script, args, timeout) for every command in hooks.json."""
    data = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))
    out: list[tuple[str, str, str, str, int]] = []
    for event, groups in data["hooks"].items():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["type"] == "command"
                m = SCRIPT_RE.match(hook["command"])
                assert m, f"command is not python \"${{CLAUDE_PLUGIN_ROOT}}/hooks/<x>.py\": {hook['command']}"
                out.append((event, group.get("matcher", ""), m["script"], m["args"].strip(), hook["timeout"]))
    return out


def test_every_entry_script_is_registered() -> None:
    entries = {p.name for p in HOOKS.glob("*.py") if not p.name.startswith("_")} - LIBRARIES
    registered = {script for _e, _m, script, _a, _t in _registrations()}
    assert entries - registered == set(), "entry scripts missing from hooks.json"


def test_every_registered_script_exists() -> None:
    for _e, _m, script, _a, _t in _registrations():
        assert (HOOKS / script).is_file(), script


def test_registrations_keep_the_events_matchers_and_args_of_the_source_project() -> None:
    regs = {(e, m, s, a) for e, m, s, a, _t in _registrations()}
    expected = {
        ("PostToolUse", "Edit|Write|Bash|PowerShell", "ruff_on_edit.py", ""),
        ("PreToolUse", "Agent|Workflow|Bash|PowerShell", "agent_prompt_guard.py", ""),
        ("PreToolUse", "Agent|Workflow|ScheduleWakeup|CronCreate|Monitor", "stop_latch.py", "--event tool"),
        ("PreToolUse", "Agent|Workflow", "quota_breaker.py", ""),
        ("PreToolUse", "Agent|Workflow", "model_route_guard.py", ""),
        ("PreToolUse", "Agent", "retry_cap_footer.py", ""),
        ("PreToolUse", "Bash|PowerShell", "shell_edit_guard.py", ""),
        ("PreToolUse", "Bash|PowerShell", "ruff_on_edit.py", ""),
        ("UserPromptSubmit", "", "cold_cache_gate.py", ""),
        ("UserPromptSubmit", "", "stop_latch.py", "--event prompt"),
    }
    assert regs == expected
