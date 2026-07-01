# Agent Workflow Entry

This file is the single project-level entry point for all agents.

## Human Prompt

The human can start work with only this sentence:

```text
按 .agent 规范开始工作。
```

English equivalent:

```text
Follow .agent and start work.
```

The human does not need to describe the workflow commands. The agent is
responsible for reading this file and operating the workflow.

## Agent Startup

When working in this repository, do this before any edit or mutating command:

1. Read `AGENTS.md`, `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, and the relevant task doc under `.agent/tasks/`.
   If a loop is relevant, read `.agent/loops/` as well.
2. Enter the autonomous work loop:

   ```bash
   python3 tools/agentctl.py work --agent <agent-name>
   ```

   This also runs the `work-start` checkpoint loop from
   `.agent/loops/checkpoints.json`.

3. If no existing task matches the user's request, create and start one yourself:

   ```bash
   python3 tools/agentctl.py work --agent <agent-name> --auto-create --title "<current request>" --scope "<paths>"
   ```

4. Follow the focus printed by `agentctl work`.

Use the backend name as `<agent-name>` when no project-specific agent ID is
assigned, for example `codex`, `claude`, `cursor`, or `agent`.

## During Work

- Keep edits inside the active task write scope.
- If the plan, task index, task doc, or rules changed, re-read them and run:

  ```bash
  python3 tools/agentctl.py refresh
  ```

- After each meaningful phase, record a short factual update:

  ```bash
  python3 tools/agentctl.py note "<short factual update>"
  ```

- When the next step is a bounded feedback cycle, run one loop cycle only:

  ```bash
  python3 tools/agentctl.py loop run <loop-id> --once
  ```

  Every loop must close Trigger, Execute, Check, Feedback, Memory, and Next, and
  must write a report under `.agent/loops/runs/`.

- For experiment or benchmark monitoring, use the project checkpoint:

  ```bash
  python3 tools/agentctl.py loop auto --checkpoint experiment-check --once
  ```

## Finish And Commit

Before claiming a phase complete:

1. Run the task verification commands.
2. Update task artifacts, tests, risks, and follow-ups.
3. Move the task to review:

   ```bash
   python3 tools/agentctl.py finish --summary "<what changed>" --tests "<commands run>"
   ```

   `finish` runs `pre-finish` and `post-finish` checkpoint loops automatically.

GitHub commits and pushes must obey `.agent/rules/github-standards.md`.
Git hooks enforce Conventional Commits, task IDs, staged workflow docs, and
review/done state before push.

## Human Role

Humans steer by editing:

- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/tasks/*.md`
- `.agent/rules/*.md`

Agents must treat those edits as updated instructions, refresh their read
receipt, and continue. Humans should not need to run workflow commands during
normal development.
