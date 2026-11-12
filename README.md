# agent-harness

A Claude Code plugin bundling a generic agent harness: hook guards (retry-cap footer, stop latch, cold-cache gate,
quota breaker, model-route guard, shell read/edit guards, agent prompt guard, ruff on edit), five subagents
(`agent-harness:log-reader`, `fast-worker`, `deep-reasoner`, `implementer`, `plan-code-reviewer`) and four skills
(`adversarial-verify`, `scope-fence`, `receiving-code-review`, `systematic-debugging`).

## Trial in a repo

1. Start Claude Code with the plugin for one session only (nothing is written to user settings):
   `claude --plugin-dir <path to this directory>`
2. Paste `RULES.md` into the trial repo's `CLAUDE.md`.
3. Optional: add `<repo>/.claude/agent-harness.json` to switch on the repo-dependent rules (pytest runner, whole-suite
   command, ruff on edit). The keys are documented at the end of `RULES.md`. Without the file those rules stay off.
4. To check the hooks fire, ask for any subagent and look for the "Retry cap:" footer in its prompt.

## Warning

Do not load this plugin in a repo whose own `.claude/settings.json` already registers the same hooks: every hook
would fire twice.

## Development

Hooks are stdlib-only Python. Tests: `.venv\Scripts\python.exe -m pytest tests/<file> -q --tb=short`, one file at a
time. Validate the layout with `claude plugin validate <path>`. See `PLAN.md` for status and decisions.
