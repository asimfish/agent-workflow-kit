# Enforcement Model

This kit uses three enforcement layers.

## Layer 1: Agent Entry Protocol

`.agent/WORKFLOW_ENTRY.md` is the single source of truth for startup behavior.
`AGENTS.md`, `.cursor/rules/agent-workflow.mdc`, `.cursor/hooks.json`,
`.codex/hooks.json`, and `.claude/settings.json` point the agent to that entry
and tell it what must happen before work starts.

The human prompt can be only:

```text
按 .agent 规范开始工作。
```

The agent expands that short prompt into:

```bash
python3 tools/agentctl.py work --agent codex
```

`work` resumes the current task or auto-claims the next assigned `ready`/`todo`
task, then creates a read receipt with hashes for the workflow entry, plan, task
index, agent registry, operating and GitHub rules, checkpoint policy, and task
document. Later checks detect when those files change and require an explicit
`agentctl refresh`. `agentctl note` and `agentctl finish` enforce the same receipt
directly, so a note cannot silently erase evidence that instructions changed.

If there is no assigned task for the current request, the agent creates and starts
one itself:

```bash
python3 tools/agentctl.py work --agent codex --auto-create --title "<current request>" --scope "<paths>"
```

Codex, Claude Code, and Cursor project hooks call `tools/agent_workflow_hook.py`. The session-start hook injects the protocol into context, the pre-tool hook blocks mutating tools when no active task session exists, and the stop hook reminds the agent to record progress or complete the task (which updates the plan and task doc).

### Long-task anti-drift (focus re-injection)

The session-start hook matches `startup|resume|compact`. Whenever a long task is
resumed or its context is compacted, the hook calls `agentctl focus` and re-injects
the active task's goal, write scope, and stage TODO plus the required reading list.
This keeps a long-running agent anchored to its task and plan instead of drifting.
Run `agentctl focus` manually any time to re-anchor.

### Runtime commands

`agentctl` is the single controller:

- `start` — read receipt + acquire task lock + write-scope conflict check + board `in_progress`.
- `work` — normal entry point; resume, claim, or auto-create a task and then start it.
- `focus` — print the current task focus (re-read before continuing).
- `note` — append a stage note to the task doc, log, and board.
- `finish` — write the completion record, free the lock, move the task to `review`.
- `gate approve|reject` — optional review gate: `review -> done` (auto-checks the plan box) or `-> blocked`.
- `board` / `task` / `agents` — machine-readable task board, task scaffolding, agent registry.
- `refresh` — re-record doc hashes after the plan/rules/task docs changed.

`start`, `progress`, and `complete` remain available as low-level equivalents for
debugging and scripted migrations.

## Layer 2: Local Git Hooks

Installed hooks live in `.githooks/`, and `init` sets `git config core.hooksPath .githooks`:

- `pre-commit`: requires an active agent session and staged task/plan/log updates when code or data changes.
- `commit-msg`: requires Conventional Commits and a task ID.
- `pre-push`: requires pushed commits to have task IDs and requires pushed tasks to be `review` or `done`.

A `done` task must have:

- a filled Completion Record,
- tests recorded,
- a checked task item in `.agent/PROJECT_PLAN.md`.

## Layer 3: GitHub CI Gate

The installed workflow `.github/workflows/agent-workflow-check.yml` runs `agentctl check --mode ci` on push and pull request. This catches bypassed local hooks in GitHub.

## Important Limitation

Git can enforce commit and push rules. Agent task start is enforced by the project protocol plus lifecycle hooks where the current tool supports them. Keep Git hooks and CI enabled because agent-native hooks can vary by product, trust settings, and client version.
