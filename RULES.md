## Delegation (agent-harness)

- `agent-harness:log-reader` (Haiku): reads test output, junit XML and logs; counts and verbatim first error lines, no diagnosis.
- `agent-harness:fast-worker` (Haiku): purely mechanical, fully specified steps; no new logic or tests.
- `agent-harness:deep-reasoner` (Opus, read-only): root cause, design trade-offs, hard diagnosis.
- `agent-harness:implementer` (Sonnet): builds a decided design, red-light first; never commits.
- `agent-harness:plan-code-reviewer` (Opus, no edit tools): `/code-review high` plus a verdict against the governing plan.
- Standing authorization, no need to ask: wide reading goes to `agent-harness:log-reader` or a Haiku Explore spawn; a diff over 3 files goes to `agent-harness:plan-code-reviewer` before you call it done; root cause or design goes to `agent-harness:deep-reasoner`; implementing a decided design goes to `agent-harness:implementer`.
- Name a `model` on every spawn. Leave `effort` off unless the owner names one (the Jev hook picks it).
- A failing subagent: first have it verify its own work with a test or script, then raise effort one level, then move up one model at lower effort.
- Spawn prompts give file:line ranges to read and the acceptance criteria. No rules preamble: this file and the hooks already reach every subagent.

## Context loading

- Search and read with Grep/Read, not `grep`/`sed`/`cat` through the shell. Read big files by offset/limit (300 lines at most), never whole.
- Cap shell output (`| head -60`, `| cut -c1-300`). Test runs: output to a file, read its tail.
- Python over ~15 lines goes in a scratchpad `.py` file, not a heredoc or `python -c`.

## Workflow

- Red-light first: write the test, run it, show the failure, then implement. A test that passes before the change exists is invalid.
- Work that spans sessions or is handed to another agent gets `plans/<name>.md`: goal, steps, decisions, dead ends. Keep a current-state and next-step block of at most 40 lines at the top and the running log below. Archive finished plans, never delete.

## agent-harness.json

Optional, at `<project>/.claude/agent-harness.json` (the project dir is `CLAUDE_PROJECT_DIR`, else the hook payload's cwd). All keys optional; a missing file means no repo-dependent rule fires.

```json
{"pytest": {"venv": ".venv", "suite_command": "scripts/run_all_tests.py", "suite_dir": "tests"},
 "process_kill": true}
```

- `pytest` (object): absent = no pytest rules. `venv` is the interpreter dir pytest must run from (default `.venv`). `suite_command` switches the whole-suite rule on and is named in its deny text. `suite_dir` is the directory whose bare use means "whole suite".
- `process_kill` (bool, default true): `false` turns off the rule that refuses process-kill commands.
- Ruff on edit has no key: it runs only when the file exists (`{}` is enough), the project has a ruff config, and a ruff binary is found (project `.venv`, else PATH).
