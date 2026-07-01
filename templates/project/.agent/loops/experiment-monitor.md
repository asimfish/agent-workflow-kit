# Loop: experiment-monitor

Purpose: monitor long-running experiment projects without launching new
experiments blindly.

## Trigger

- Mode: manual
- Future Modes: cron, task-finished
- Debounce: do not rerun more than once every 30 minutes unless result files
  changed.

## Execute

- Agent: experiment-agent
- Task: experiment or benchmark task
- Allowed Writes:
  - .agent/loops/runs/
  - .agent/loops/state.json
  - doc/
- Max Iterations: 1
- Max Runtime Minutes: 30

## Check

- Scan bounded experiment directories for `DONE` and `ERROR` markers.
- Report scan caps instead of walking an unbounded tree forever.
- Do not claim success from marker counts alone.

## Feedback

- If errors exist: classify them before relaunching.
- If missing cells are suspected: create a task-specific relaunch list first.
- If no experiment directories exist: report no-op.

## Memory

- Write a run report to `.agent/loops/runs/`.
- Update `.agent/loops/state.json` with the last status and report path.
- Update task docs separately with `agentctl note` when the loop changes task
  state.

## Next

- Stop after the report.
- Do not auto-launch expensive experiments in this phase.
