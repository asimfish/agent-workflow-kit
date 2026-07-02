# Loop: project-check

Purpose: example custom loop. Its `loop-check` command block is executed by
`agentctl loop run project-check --once`; exit codes decide the run status.

## Trigger

- Mode: manual
- Checkpoints: none by default (add it to `.agent/loops/checkpoints.json` to
  run it from a checkpoint).

## Execute

- Agent: any
- Task: the active task
- Allowed Writes:
  - .agent/loops/runs/
  - .agent/loops/state.json
- Max Iterations: 1
- Max Runtime Minutes: 10

## Check

Declared commands run in order from the repository root. `timeout` (seconds)
and `max-output` (characters per failing command) are optional.

```loop-check
timeout: 120
max-output: 2000
# add project-specific checks below, one '$ <command>' per line
$ python3 -m py_compile tools/agentctl.py
```

## Feedback

- If a command fails: the report lists the exit code and capped output; fix
  the failing commands, then re-run this loop.
- If all commands pass: nothing to do before the next cycle.

## Memory

- Write a run report to `.agent/loops/runs/`.
- Update `.agent/loops/state.json` with the last status and report path.

## Next

- Stop after the report. When run via a checkpoint, a failure creates a
  loop follow-up packet in the bus; a later success auto-closes it.
