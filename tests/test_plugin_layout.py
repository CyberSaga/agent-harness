"""Plugin layout: agents/*.md frontmatter, skills present, model_route_guard accepts the namespaced agents."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
AGENTS = ROOT / "agents"
SKILLS = ROOT / "skills"
EXPECTED_AGENTS = {"log-reader", "fast-worker", "deep-reasoner", "implementer", "plan-code-reviewer"}
EXPECTED_SKILLS = {"adversarial-verify", "scope-fence", "receiving-code-review", "systematic-debugging"}

sys.path.insert(0, str(ROOT / "hooks"))
import model_route_guard  # noqa: E402


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].strip() == "---", f"{path.name}: no frontmatter"
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            out[key.strip()] = value.strip()
    raise AssertionError(f"{path.name}: frontmatter not closed")


def test_agent_set_is_exactly_the_expected_five() -> None:
    assert {p.stem for p in AGENTS.glob("*.md")} == EXPECTED_AGENTS


@pytest.mark.parametrize("name", sorted(EXPECTED_AGENTS))
def test_agent_frontmatter_has_name_description_model(name: str) -> None:
    fm = _frontmatter(AGENTS / f"{name}.md")
    assert fm.get("name") == name
    assert fm.get("description")
    assert fm.get("model")
    assert fm["model"] != "inherit"


def test_plan_code_reviewer_has_no_agent_tool() -> None:
    tools = {t.strip() for t in _frontmatter(AGENTS / "plan-code-reviewer.md")["tools"].split(",")}
    assert "Agent" not in tools
    assert not {"Edit", "Write"} & tools


@pytest.mark.parametrize("name", sorted(EXPECTED_AGENTS))
def test_model_route_guard_allows_namespaced_agent_without_model(name: str, tmp_path: Path) -> None:
    tool_input = {"subagent_type": f"agent-harness:{name}", "prompt": "x"}
    assert model_route_guard.decide("Agent", tool_input, tmp_path, ROOT) is None


def test_model_route_guard_still_denies_unknown_namespaced_agent(tmp_path: Path) -> None:
    tool_input = {"subagent_type": "agent-harness:no-such-agent", "prompt": "x"}
    assert model_route_guard.decide("Agent", tool_input, tmp_path, ROOT) is not None


@pytest.mark.parametrize("name", sorted(EXPECTED_SKILLS))
def test_skill_has_frontmatter(name: str) -> None:
    fm = _frontmatter(SKILLS / name / "SKILL.md")
    assert fm.get("name") == name
    assert fm.get("description")


def test_rules_fragment_is_short_and_names_every_agent_namespaced() -> None:
    rules = ROOT / "RULES.md"
    assert rules.is_file()
    text = rules.read_text(encoding="utf-8")
    assert len(text.splitlines()) < 60 + 25  # fragment under 60 lines plus the agent-harness.json section
    for name in EXPECTED_AGENTS - {"fast-worker"}:
        assert f"agent-harness:{name}" in text
    for key in ("pytest", "venv", "suite_command", "suite_dir", "process_kill"):
        assert key in text


def test_readme_documents_trial_command() -> None:
    assert "--plugin-dir" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_no_project_specific_text_in_agents_skills_rules() -> None:
    # Built from fragments so this file does not itself match the repo-wide hygiene grep.
    banned = ("ji" + "ang", "python " + "programs", "pdf" + "_editor", "Py" + "MuPDF", "test" + "_scripts",
              "run" + "_suite", ".code" + "graph")
    files = [*AGENTS.glob("*.md"), *SKILLS.rglob("*"), *(ROOT / "hooks").glob("*.py"),
             *(ROOT / "hooks").glob("*.json"), ROOT / "RULES.md", ROOT / "README.md"]
    for f in files:
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{f.name} mentions {word}"
