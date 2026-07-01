# Loop: doc-hygiene

Purpose: prevent `.agent` task documents from becoming noisy, stale, or
unusable as agents and tasks multiply.

## Trigger

- Mode: manual
- Checkpoints: pre-finish, post-finish
- Debounce: run after meaningful task-doc changes, not after every small edit.

## Execute

- Agent: supervisor
- Task: any active workflow-maintenance task
- Allowed Writes:
  - .agent/loops/runs/
  - .agent/loops/state.json
- Max Iterations: 1
- Max Runtime Minutes: 10

## Check

- Verify task docs preserve required headings.
- Detect duplicate Stage Log lines.
- Detect placeholder stage logs mixed with real updates.
- Detect review tasks with empty completion summaries.

## Feedback

- If issues exist: next agent should clean the listed task docs before the task
  is approved or used as handoff context.
- If no issues exist: docs are clean enough for the next loop.

## Memory

- Write a run report to `.agent/loops/runs/`.
- Update `.agent/loops/state.json` with the last status and report path.

## Next

- Stop after the report.
- Do not rewrite task docs automatically in this phase.
