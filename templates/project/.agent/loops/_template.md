# Loop: <loop-id>

Every loop must close these six links: Trigger, Execute, Check, Feedback,
Memory, and Next. Keep each loop small, bounded, and auditable.

## Trigger

- Mode: manual
- Future Modes: cron, session-start, task-finished
- Debounce: do not run again if a useful run completed recently

## Execute

- Agent: <agent-id>
- Task: <task-id or any>
- Allowed Writes:
  - .agent/loops/runs/
- Max Iterations: 1
- Max Runtime Minutes: 30

## Check

- Commands:
  - python3 tools/agentctl.py check --mode manual
- Required Artifacts:
  - .agent/loops/runs/<timestamp>-<loop-id>.md
- Fail If:
  - required sections are missing
  - outputs are not written to durable project files

## Feedback

- If successful: record what changed and what should happen next.
- If partial: record exactly what remains.
- If blocked: record the smallest human decision needed.

## Memory

- Write every run report to `.agent/loops/runs/`.
- Update `.agent/loops/state.json`.
- Update task docs with `agentctl note` when the loop changes task state.

## Next

- Stop after one run unless an explicit scheduler or downstream loop triggers a
  new cycle.
- Do not run unbounded loops.
