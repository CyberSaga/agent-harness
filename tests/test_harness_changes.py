"""Behaviour that differs from the project the hooks were extracted from.

  - project dir: CLAUDE_PROJECT_DIR, else the payload cwd (never the hook's own location);
  - `<project>/.claude/agent-harness.json` absent => the repo-dependent rules do nothing;
  - the process-kill rule is on by default;
  - `agent-harness:<name>` resolves against this plugin's agents/<name>.md;
  - the quote/heredoc helpers live in `_shellparse.py`;
  - deny text carries no project names, paths or anecdotes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(HOOKS))

import _hookkit as kit  # noqa: E402
import _shellparse as shellparse  # noqa: E402
import agent_prompt_guard as guard  # noqa: E402
import model_route_guard as route  # noqa: E402
import ruff_on_edit as roe  # noqa: E402

CFG = {"venv": ".venv", "suite_command": "scripts/run_suite.py", "suite_dir": "tests"}
HAS_RUFF = shutil.which("ruff") is not None


def _project(tmp_path: Path, config: object | None, name: str = "proj") -> Path:
    root = tmp_path / name
    (root / ".claude").mkdir(parents=True)
    if config is not None:
        (root / ".claude" / "agent-harness.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def _run(script: str, payload: dict, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=60,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
    )


def _reason(result: subprocess.CompletedProcess) -> str:
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


# --------------------------------------------------------------------------- project dir and config


def test_project_dir_prefers_env_then_payload_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert kit.project_dir({"cwd": str(tmp_path)}) == tmp_path
    assert kit.project_dir({}) == Path(".")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "envdir"))
    assert kit.project_dir({"cwd": str(tmp_path)}) == tmp_path / "envdir"


def test_load_config_absent_invalid_and_valid(tmp_path: Path) -> None:
    assert kit.load_config(_project(tmp_path, None, "a")) == {}
    bad = _project(tmp_path, None, "b")
    (bad / ".claude" / "agent-harness.json").write_text("{not json", encoding="utf-8")
    assert kit.load_config(bad) == {}
    listy = _project(tmp_path, ["x"], "c")
    assert kit.load_config(listy) == {}
    ok = _project(tmp_path, {"pytest": {"venv": ".venv"}}, "d")
    assert kit.load_config(ok) == {"pytest": {"venv": ".venv"}}
    assert kit.load_config(tmp_path / "missing") == {}


# --------------------------------------------------------------------------- agent_prompt_guard


def test_guard_pytest_rules_are_off_without_config() -> None:
    for command in ("pytest -q", "python -m pytest tests/test_a.py", ".venv/bin/python -m pytest"):
        assert guard.violations("Bash", {"command": command}) == [], command
    assert guard.violations("Agent", {"prompt": "run `pytest tests/`", "description": "d"}) == []


def test_guard_empty_pytest_config_enables_venv_rules_only() -> None:
    kinds = {v.kind for v in guard.violations("Bash", {"command": "pytest -q"}, pytest_cfg={})}
    assert kinds == {"bare-pytest"}
    kinds = {v.kind for v in guard.violations("Bash", {"command": "python -m pytest tests/a.py"}, pytest_cfg={})}
    assert kinds == {"system-python"}


def test_guard_whole_suite_needs_a_suite_command() -> None:
    cfg = {"suite_command": "make test", "suite_dir": "spec"}
    found = guard.violations("Bash", {"command": ".venv/bin/python -m pytest spec/"}, pytest_cfg=cfg)
    assert [v.kind for v in found] == ["whole-suite"]
    assert "make test" in found[0].rule()
    assert guard.violations("Bash", {"command": ".venv/bin/python -m pytest spec/test_a.py"}, pytest_cfg=cfg) == []


def test_guard_custom_venv_marker() -> None:
    cfg = {"venv": "ci-env"}
    assert guard.violations("Bash", {"command": "ci-env/bin/python -m pytest tests/a.py"}, pytest_cfg=cfg) == []
    kinds = {v.kind for v in guard.violations("Bash", {"command": ".venv/bin/python -m pytest tests/a.py"}, pytest_cfg=cfg)}
    assert kinds == {"system-python"}


def test_guard_kill_rule_is_on_by_default_and_can_be_switched_off() -> None:
    command = {"command": "taskkill /F /PID 5"}
    assert [v.kind for v in guard.violations("Bash", command, in_subagent=True)] == ["process-kill"]
    assert [v.kind for v in guard.violations("Agent", {"prompt": "then pkill -f x"})] == ["process-kill"]
    assert guard.violations("Bash", command, in_subagent=True, kill_rule=False) == []


def test_guard_main_without_config_allows_bare_pytest_and_denies_kills_in_subagents(tmp_path: Path) -> None:
    project = _project(tmp_path, None)
    allowed = _run("agent_prompt_guard.py", {"tool_name": "Bash", "cwd": str(project), "tool_input": {"command": "pytest -q"}})
    assert allowed.returncode == 0 and allowed.stdout.strip() == b""
    kill = {"tool_name": "Bash", "cwd": str(project), "agent_id": "a1", "tool_input": {"command": "taskkill /F /PID 5"}}
    assert "no taskkill/pkill/Stop-Process" in _reason(_run("agent_prompt_guard.py", kill))


def test_guard_main_reads_config_from_claude_project_dir_over_cwd(tmp_path: Path) -> None:
    project = _project(tmp_path, {"pytest": CFG})
    elsewhere = _project(tmp_path, None, "other")
    payload = {"tool_name": "Bash", "cwd": str(elsewhere), "tool_input": {"command": "pytest tests/test_a.py"}}
    result = _run("agent_prompt_guard.py", payload, env={"CLAUDE_PROJECT_DIR": str(project)})
    assert "never bare `pytest`" in _reason(result)
    assert _run("agent_prompt_guard.py", payload).stdout.strip() == b""  # no config at the payload cwd


def test_guard_main_config_can_disable_the_kill_rule(tmp_path: Path) -> None:
    project = _project(tmp_path, {"process_kill": False})
    payload = {"tool_name": "Bash", "cwd": str(project), "agent_id": "a1", "tool_input": {"command": "taskkill /F /PID 5"}}
    assert _run("agent_prompt_guard.py", payload).stdout.strip() == b""


def test_guard_deny_text_is_generic_and_names_the_configured_suite_command(tmp_path: Path) -> None:
    project = _project(tmp_path, {"pytest": {"venv": ".venv", "suite_command": "ci/run_all.py", "suite_dir": "tests"}})
    payload = {"tool_name": "Bash", "cwd": str(project), "tool_input": {"command": "pytest -q"}}
    text = _reason(_run("agent_prompt_guard.py", payload))
    assert "ci/run_all.py" in text
    for rule in guard.RULES.values():
        assert not FORBIDDEN.search(rule)
    assert not FORBIDDEN.search(text)


def test_quota_breaker_allows_when_no_statusline_state_exists(tmp_path: Path) -> None:
    home = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    result = _run("quota_breaker.py", {"tool_name": "Agent", "tool_input": {}}, env=home)
    assert result.returncode == 0 and result.stdout.strip() == b""


# --------------------------------------------------------------------------- _shellparse


def test_shellparse_owns_the_quote_and_heredoc_helpers() -> None:
    assert shellparse._mask_quotes("grep 'a b' f", keep_command_args=False) == "grep '___' f"
    assert shellparse._mask_quotes('bash -c "ls x"', keep_command_args=True) == 'bash -c "ls x"'
    assert shellparse._strip_heredocs("cat <<'E'\nbody\nE\nls").startswith("cat <<'E'\n")
    assert "body" not in shellparse._strip_heredocs("cat <<'E'\nbody\nE\nls")
    assert guard._mask_quotes is shellparse._mask_quotes
    assert guard._strip_heredocs is shellparse._strip_heredocs


def test_shell_guards_do_not_import_agent_prompt_guard() -> None:
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(HOOKS)!r})\n"
        "import shell_edit_guard, shell_read_guard\n"
        "print('agent_prompt_guard' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "False", out.stderr


# --------------------------------------------------------------------------- model_route_guard


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    agents = tmp_path / "plugin" / "agents"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text("---\nname: worker\nmodel: sonnet\n---\nbody\n", encoding="utf-8")
    (agents / "floating.md").write_text("---\nname: floating\nmodel: inherit\n---\n", encoding="utf-8")
    return tmp_path / "plugin"


def test_namespaced_agent_resolves_against_the_plugin_agents_dir(tmp_path: Path, plugin: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    assert route.decide("Agent", {"subagent_type": "agent-harness:worker"}, project, plugin_root=plugin) is None
    for name in ("agent-harness:floating", "agent-harness:missing", "agent-harness:../worker", "other:worker"):
        assert route.decide("Agent", {"subagent_type": name}, project, plugin_root=plugin) is not None, name
    assert route.pinned_model(project, "agent-harness:worker", plugin_root=plugin) == "sonnet"


def test_namespaced_agent_does_not_read_the_project_agents_dir(tmp_path: Path, plugin: Path) -> None:
    project = tmp_path / "proj"
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "agents" / "worker.md").write_text("---\nmodel: opus\n---\n", encoding="utf-8")
    assert route.decide("Agent", {"subagent_type": "agent-harness:nope"}, project, plugin_root=plugin) is not None


def test_default_plugin_root_is_the_parent_of_hooks() -> None:
    assert route.PLUGIN_ROOT == ROOT


def test_route_main_resolves_namespaced_agent_from_claude_plugin_root(tmp_path: Path, plugin: Path) -> None:
    payload = {"tool_name": "Agent", "cwd": str(tmp_path), "tool_input": {"subagent_type": "agent-harness:worker"}}
    ok = _run("model_route_guard.py", payload, env={"CLAUDE_PLUGIN_ROOT": str(plugin)})
    assert ok.returncode == 0 and ok.stdout.strip() == b""
    bad = {**payload, "tool_input": {"subagent_type": "agent-harness:floating"}}
    assert _run("model_route_guard.py", bad, env={"CLAUDE_PLUGIN_ROOT": str(plugin)}).stdout.strip() != b""


# --------------------------------------------------------------------------- ruff_on_edit


def _ruff_payload(project: Path, **extra: object) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Write", "tool_input": {"file_path": str(project / "m.py")}, **extra}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _repo(tmp_path: Path, config_file: str | None, body: str = "x = 1\n") -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "m.py").write_text("import os\n" + body, encoding="utf-8")
    if config_file == "pyproject.toml":
        (root / config_file).write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    elif config_file == "pyproject-noruff":
        (root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    elif config_file:
        (root / config_file).write_text("line-length = 100\n", encoding="utf-8")
    return root


def test_ruff_config_detection(tmp_path: Path) -> None:
    for name, expected in (("ruff.toml", True), (".ruff.toml", True), ("pyproject.toml", True),
                           ("pyproject-noruff", False), (None, False)):
        sub = tmp_path / f"case_{name}"
        sub.mkdir()
        repo = _repo(sub, name)
        assert roe.has_ruff_config(repo) is expected, name


@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
def test_ruff_edit_leg_does_nothing_without_a_ruff_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path, None)
    result = _run("ruff_on_edit.py", _ruff_payload(repo, cwd=str(repo)))
    assert result.returncode == 0 and result.stderr == b""


@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
@pytest.mark.parametrize("config_file", ["ruff.toml", ".ruff.toml", "pyproject.toml"])
def test_ruff_edit_leg_lints_when_the_project_has_a_config(tmp_path: Path, config_file: str) -> None:
    repo = _repo(tmp_path, config_file)
    result = _run("ruff_on_edit.py", _ruff_payload(repo, cwd=str(repo)))
    assert result.returncode == 2 and b"F401" in result.stderr


@pytest.mark.skipif(not HAS_RUFF, reason="ruff not installed")
def test_ruff_project_root_comes_from_claude_project_dir_over_cwd(tmp_path: Path) -> None:
    configured = _repo(tmp_path, "ruff.toml")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = _ruff_payload(configured, cwd=str(elsewhere))
    assert _run("ruff_on_edit.py", payload, env={"CLAUDE_PROJECT_DIR": str(configured)}).returncode == 2
    assert _run("ruff_on_edit.py", payload, env={"CLAUDE_PROJECT_DIR": str(elsewhere)}).returncode == 0


def test_ruff_missing_binary_is_silent(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "ruff.toml")
    result = _run("ruff_on_edit.py", _ruff_payload(repo, cwd=str(repo)), env={"PATH": str(tmp_path / "empty")})
    assert result.returncode == 0 and result.stderr == b""


def test_ruff_prefers_the_project_venv(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "ruff.toml")
    scripts = repo / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    fake = scripts / "ruff.exe"
    fake.write_text("", encoding="utf-8")
    assert roe.resolve_ruff(repo) == str(fake)


def test_ruff_shell_leg_does_nothing_without_a_config_and_writes_no_stamp(tmp_path: Path) -> None:
    repo = _repo(tmp_path, None)
    stamps = tmp_path / "stamps"
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t1", "session_id": "s",
               "cwd": str(repo), "tool_input": {"command": "true"}}
    result = _run("ruff_on_edit.py", payload, env={"CLAUDE_RUFF_STAMP_DIR": str(stamps)})
    assert result.returncode == 0 and not stamps.exists()


# --------------------------------------------------------------------------- shell guards


def _tracked_repo(tmp_path: Path) -> Path:
    root = tmp_path / "tr"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "notes.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return root


def test_shell_guard_falls_back_to_claude_project_dir_when_the_payload_has_no_cwd(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path)
    payload = {"tool_name": "Bash", "tool_input": {"command": "sed -i 's/a/b/' notes.md"}}
    result = _run("shell_edit_guard.py", payload, cwd=tmp_path, env={"CLAUDE_PROJECT_DIR": str(repo)})
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shell_guard_judges_the_payload_cwd_not_the_hooks_own_location(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path)
    payload = {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "sed -i 's/a/b/' notes.md"}}
    assert json.loads(_run("shell_edit_guard.py", payload, cwd=tmp_path).stdout)
    silent = {"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": "sed -i 's/a/b/' notes.md"}}
    assert _run("shell_edit_guard.py", silent, cwd=repo).stdout.strip() == b""


def test_shell_guard_deny_text_has_no_project_anecdotes(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path)
    edit = {"tool_name": "Bash", "cwd": str(repo), "tool_input": {"command": "cat notes.md; sed -i 's/a/b/' notes.md"}}
    text = _reason(_run("shell_edit_guard.py", edit))
    assert "Edit tool" in text and "Read tool" in text
    assert not FORBIDDEN.search(text), text
    assert not re.search(r"20\d\d-\d\d-\d\d", text), text
    assert "TODOS" not in text and "PITFALLS" not in text and "CRLF checkout" not in text


# --------------------------------------------------------------------------- hygiene

FORBIDDEN = re.compile("|".join(["pdf_" + "editor", "Py" + "MuPDF", "test_" + "scripts", "ji" + "ang", "python " + "programs"]))


def test_hooks_carry_no_project_names_or_paths() -> None:
    for path in [*HOOKS.glob("*.py"), *HOOKS.glob("*.json"), *(ROOT / "tests").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert not FORBIDDEN.search(text), path.name
