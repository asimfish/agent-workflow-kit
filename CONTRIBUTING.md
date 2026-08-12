# Contributing

This project dogfoods its own workflow: every change — human or agent — goes
through the task system it ships. That keeps the audit trail honest and is
also the fastest way to understand the kit.

## Ground Rules

- One task, one branch, one PR. Conventional Commits with a task reference
  (`Refs: T-...`) on every commit.
- Every functional change lands with tests and passes the full suite; docs
  changes pass `agentctl check --mode manual` and `agentctl reconcile check`.
- Merges to `main` require an independent review: a separate session whose
  host runtime differs from every runtime recorded by the worker task runs
  `agentctl gate approve --task <task> --by <reviewer>`.
- Never commit secrets. The pre-commit hook scans staged content; CI reruns
  everything on Ubuntu and Windows.

## Workflow For A Change

```bash
# 1. Start (or auto-create) a task — code tasks get an isolated worktree
python3 tools/agentctl.py work --agent <you> --auto-create \
  --title "..." --scope "tools/,tests/" --type code

# 2. Implement inside the printed scope; record progress
python3 tools/agentctl.py note "..."

# 3. Verify
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tools/agentctl.py check --mode manual

# 4. Finish, commit, push
python3 tools/agentctl.py finish --summary "..." --tests "..."
git commit -m "type(scope): summary" -m "Refs: <task-id>"
git push -u origin <branch>

# 5. Independent review from another session, then PR to main
python3 tools/agentctl.py gate approve --task <task-id> --by <reviewer>
```

The [PR template](.github/PULL_REQUEST_TEMPLATE.md) asks for a summary, task
references, verification evidence, and risk notes — fill every section
(`none` is acceptable, blank is not).

## Reporting Issues

Use the issue templates. For bugs, include the controller output, the
relevant `.agent/tasks/<id>.md` stage log when one exists, and your platform
(the kit supports POSIX and Windows; CI covers both).

## Code Style

- Python 3.9+ standard library only — the controller is a single
  dependency-free file (`tools/agentctl.py`) by design.
- Fail closed on authority and identity; fail open only for hygiene that
  must never block work (see `docs/multi-session-execution.md` for the
  invariants).
- Comments explain intent and trade-offs, not mechanics.

## Standards References

- `.agent/rules/github-standards.md` — commit, branch, push, and PR rules.
- `docs/workflow.md` — the full task lifecycle.
- `docs/multi-session-execution.md` — coordination invariants a change must
  not weaken.
