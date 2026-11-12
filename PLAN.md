# agent-harness: extraction plan

## Current state and next step

- 2026-11-12: hooks slice DONE (commit "port generic hooks"). Ported: retry_cap_footer, stop_latch, cold_cache_gate,
  quota_breaker, model_route_guard, shell_edit_guard (+ shell_read_guard library), agent_prompt_guard, ruff_on_edit,
  plus `_hookkit.py` and the new `_shellparse.py`. All registered in `hooks/hooks.json`. Not ported: pre_paste_gate.
  `claude plugin validate` passes. 7 test files green in `tests/` (`.venv\Scripts\python.exe -m pytest tests/<file>`).
- The live firing check is NOT done: auto mode refused a headless `claude -p` run. Owner check, from a scratch repo:
  `claude --plugin-dir <this dir>`, then ask for any subagent and see the "Retry cap:" footer in its prompt.
- Per-repo config `<project>/.claude/agent-harness.json` (all keys optional):
  `{"pytest": {"venv": ".venv", "suite_command": "...", "suite_dir": "tests"}, "process_kill": true}`.
  No `pytest` key = no pytest rules; `suite_command` is what switches the whole-suite rule on. Ruff lint has no key of its
  own: it runs only when the file exists (any JSON object, `{}` is enough) AND the project has a ruff config AND a ruff
  binary is found (`kit.has_config`).
- Next: agents (log-reader, fast-worker, deep-reasoner, implementer, plan-code-reviewer), skills, RULES.md. The
  `agent-harness:<name>` lookup in model_route_guard already reads `agents/<name>.md`, which does not exist yet.
- Owner constraint: no user-level registration. Trial per repo with `claude --plugin-dir <this dir>` (one session
  only, nothing written to `~/.claude/settings.json`). Never trial it in pdf_editor: its own
  `.claude/settings.json` registers the same hooks, so every hook would fire twice.
- Out of scope for this slice: Codex (`codex_hook.py`, `_codexkit.py`), git hooks (device guard, pre-push), and
  any change to pdf_editor.

## Decisions

- Source of truth during the trial: pdf_editor `scripts/hooks/` at commit 9defa9b6. Ports are copies; pdf_editor
  keeps its own until the owner switches it over.
- Hooks are stdlib-only. They are launched as `python "${CLAUDE_PLUGIN_ROOT}/hooks/<x>.py"` and import siblings
  relative to their own file. The project directory comes from `CLAUDE_PROJECT_DIR`, else the payload `cwd`,
  never `Path(__file__).parents[...]`.
- Per-repo settings live in `<project>/.claude/agent-harness.json`. A repo-dependent rule does nothing when the
  file or its key is absent: a wrong deny in a foreign repo is the failure this trial looks for.
- Plugin agents are namespaced: `agent-harness:<name>`. A plugin cannot override the built-in `Explore`/`Plan`, so
  those two are not ported; spawns name a model instead (`model_route_guard` requires one anyway).
- No hard-coded user paths, hostnames or project names in any file here.

## Hooks (registration copied from pdf_editor `.claude/settings.json`)

| Hook | Event / matcher | Change needed |
|---|---|---|
| retry_cap_footer | PreToolUse Agent | none (done) |
| stop_latch | PreToolUse Agent\|Workflow\|ScheduleWakeup\|CronCreate\|Monitor `--event tool`; UserPromptSubmit `--event prompt` | none expected; confirm no repo path |
| cold_cache_gate | UserPromptSubmit | none expected |
| quota_breaker | PreToolUse Agent\|Workflow | reads `~/.claude/statusline_state` (written by the owner's status line); missing state must allow |
| model_route_guard | PreToolUse Agent\|Workflow | pinned-model lookup also resolves `agent-harness:<name>` to this plugin's `agents/<name>.md` |
| shell_edit_guard + shell_read_guard | PreToolUse Bash\|PowerShell | move `_mask_quotes` / `_strip_heredocs` out of agent_prompt_guard into `_shellparse.py`; deny text without pdf_editor anecdotes (TODOS/PITFALLS); git-tracked test runs against the payload's project dir |
| agent_prompt_guard | PreToolUse Agent\|Workflow\|Bash\|PowerShell | pytest rules (venv runner, whole-suite command, suite dir) come from `agent-harness.json` key `pytest`; absent = those rules off. The process-kill rule stays on by default |
| ruff_on_edit | PostToolUse Edit\|Write\|Bash\|PowerShell; PreToolUse Bash\|PowerShell | project root from the payload; ruff from `<project>/.venv` or PATH; skip when no ruff, or when the project has no ruff config (`ruff.toml`, `.ruff.toml`, `[tool.ruff]` in pyproject.toml) |
| pre_paste_gate | UserPromptSubmit `--event prompt`; Stop `--event stop` | read it first; owner keywords at :40 and a git diff at :58 may be pdf_editor-specific. Port only if it is generic |

Tests: port the matching cases from `test_claude_hooks.py`, `test_shell_edit_guard.py`, `test_shell_read_guard.py`,
`test_ruff_on_edit_shell.py`, `test_retry_cap_footer.py` into `tests/`. New behaviour (config absent = no deny,
namespaced agent lookup, payload project dir) gets red tests first. Run them one file at a time with this repo's
`.venv` (`.venv\Scripts\python.exe -m pytest tests/<file> -q --tb=short`).

## Agents, skills, rules

- Agents: log-reader, fast-worker, deep-reasoner, implementer, plan-code-reviewer. Keep each body and replace the
  pdf_editor "Hard constraints" block with "follow the project's CLAUDE.md / AGENTS.md commands and rules".
- Skills: adversarial-verify, scope-fence, receiving-code-review, systematic-debugging (check each body for
  pdf_editor references). Not ported: verify, pdf-text-fidelity.
- `RULES.md`: the generic part of pdf_editor's CLAUDE.md (Delegation with namespaced agent names; shell-read and
  scratchpad-script rules; red-light first; effort left to the Jev hook unless the owner names one). The owner
  pastes it into a trial repo's CLAUDE.md. Nothing goes into `~/.claude/CLAUDE.md` yet.

## Log

- 2026-11-12: skeleton, validate passed, stdin check passed; headless live check refused by auto mode.
- 2026-11-12: hooks slice. Copied hooks and tests from pdf_editor 9defa9b6, ran them green unmodified (retry 6, claude_hooks
  145 without the pre_paste cases, shell_edit 65, shell_read 71, ruff_shell 15), then red tests first for the changes.
  Changes: `kit.project_dir` / `kit.load_config` (CLAUDE_PROJECT_DIR, else payload cwd); `_shellparse.py` holds
  `_mask_quotes` / `_strip_heredocs` (the shell guards no longer import agent_prompt_guard); agent_prompt_guard pytest rules
  come from the config `pytest` key (absent = off), kill rule on unless `process_kill` is false; model_route_guard resolves
  `agent-harness:<name>` in `<plugin root>/agents/` (CLAUDE_PLUGIN_ROOT, else the parent of hooks/); ruff_on_edit needs a ruff
  config and a ruff binary in the project (else silent exit 0, no stamp) and no longer uses the hook's location; shell_edit_guard
  resolves relative targets from payload cwd, else CLAUDE_PROJECT_DIR; deny texts and docstrings lost project names and dated
  anecdotes. Tests: a conftest clears CLAUDE_PROJECT_DIR / CLAUDE_PLUGIN_ROOT so a session's own values cannot leak in.
- 2026-11-12: pre_paste_gate SKIPPED. It exists for one owner workflow: a Grok batch validator that refuses a paste while
  tracked files differ from HEAD. Its cue regexes (Chinese paste phrases, "v16 pilot", "brief") and deny text name that
  workflow, and it runs `git diff HEAD` on every prompt that mentions pasting. Nothing generic is left once those are
  removed, so it stays in pdf_editor only and is not registered here.
- 2026-11-12: a test pins that every hooks/*.py entry script is registered and every registered script exists
  (`tests/test_hooks_json.py`; `shell_read_guard.py` is listed there as a library, called by shell_edit_guard).
- 2026-11-12: follow-up after review: ruff lint now also needs `agent-harness.json` (brief: absent file = pytest rules and
  ruff do nothing; the table's ruff-config / binary skips apply on top). Weak tests fixed (missing-binary keeps git on PATH,
  namespaced lookup uses a name that exists only in the project dir).
- Known leftovers: the stop_latch / cold_cache_gate prompts contain Chinese deny text and phrases (owner's language, no
  project names); `plugin.json` lacks README / LICENSE / icon (only matters for `--profile directory`).
