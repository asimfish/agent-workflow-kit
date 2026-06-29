# Agent Operating Entry

All agents working in this repository must follow this entry protocol.

## Before Any Edit

Run the autonomous work entry:

```bash
python3 tools/agentctl.py work --agent <agent-name>
```

This resumes the current task or claims the next assigned `ready`/`todo` task,
records the read receipt, acquires the task lock, and prints the required focus.
Do not edit project files before this command succeeds.

Codex and Claude Code project hooks also block mutating tools when no active task session exists. Git hooks and CI remain the final enforcement layer.

## During Work

- Keep the assigned task scope narrow.
- Do not modify files outside the task write scope unless the task document is updated first.
- After each meaningful phase, run:

```bash
python3 tools/agentctl.py note "<short factual update>"
```

- If `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, or `.agent/rules/*.md` changes during the session, re-read the changed files and run:

```bash
python3 tools/agentctl.py refresh
```

## Completing Work

Before claiming completion:

1. Run the task verification commands.
2. Update the task document with artifacts, tests, risks, and follow-ups.
3. Run:

```bash
python3 tools/agentctl.py finish --summary "<what changed>" --tests "<commands run>"
```

Before pushing to GitHub, the task must be `review` or `done`. If the task is done, `.agent/PROJECT_PLAN.md` must show it as checked off and the task document must have a filled Completion Record.

## Commit Rules

- Commit messages must use Conventional Commits.
- Every commit message must include the active task ID, for example `Refs: T-001`.
- A commit with code or data changes must include a staged task doc, plan, or progress log update.
