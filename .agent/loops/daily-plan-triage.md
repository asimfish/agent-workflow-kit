# Loop: daily-plan-triage

Purpose: keep the project plan, task index, task docs, and machine board aligned
so agents can start from durable state instead of chat memory.

## Trigger

- Mode: manual
- Future Modes: cron, session-start
- Debounce: do not rerun more than once every 30 minutes unless the plan changed.

## Execute

- Agent: supervisor
- Task: any active workflow-maintenance task
- Allowed Writes:
  - .agent/loops/runs/
  - .agent/loops/state.json
- Max Iterations: 1
- Max Runtime Minutes: 10

## Check

- Compare `.agent/board.json`, `.agent/TASKS.md`, `.agent/tasks/*.md`, and
  `.agent/PROJECT_PLAN.md`.
- Report status mismatches, missing task docs, review tasks, in-progress tasks,
  and done tasks not checked in the plan.

## Feedback

- If mismatches exist: next agent should repair the plan/task/board state before
  relying on automation.
- If review tasks exist: next agent should route them to a reviewer or human.
- If no issues exist: next loop can proceed.

## Memory

- Write a run report to `.agent/loops/runs/`.
- Update `.agent/loops/state.json` with the last status and report path.

## Next

- Stop after the report.
- Do not auto-approve or auto-reorder tasks.
