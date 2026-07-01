# Loop Engineering

Agent Workflow Kit treats a loop as a small, bounded feedback cycle. A loop is
not an infinite agent process. It is a versioned project contract that answers
six questions:

1. Trigger: who or what starts the cycle?
2. Execute: which agent acts, and what may it write?
3. Check: how does the project decide whether the work is valid?
4. Feedback: how does this run change the next run?
5. Memory: where is the durable record written?
6. Next: stop, continue later, hand off, or ask a human?

## Files

Installed projects include:

```text
.agent/loops/
  _template.md
  daily-plan-triage.md
  doc-hygiene.md
  experiment-monitor.md
  state.json
  runs/
```

Every loop file must contain these headings:

```text
## Trigger
## Execute
## Check
## Feedback
## Memory
## Next
```

`agentctl check` validates that installed loop contracts keep those sections.

## Commands

```bash
python3 tools/agentctl.py loop list
python3 tools/agentctl.py loop show daily-plan-triage
python3 tools/agentctl.py loop run daily-plan-triage --once
```

Only one-shot runs are supported in this phase. Cron, worktree pools, connector
loops, and automatic experiment launches are intentionally out of scope until the
base loop contract is proven.

Each run writes:

```text
.agent/loops/runs/YYYYMMDD-HHMMSS-<loop-id>.md
.agent/loops/state.json
```

The run report records what was read, what happened, which checks ran, what
feedback was produced, what memory changed, and what should happen next.

## Built-In Loops

`daily-plan-triage` compares `.agent/board.json`, `.agent/TASKS.md`, task docs,
and `.agent/PROJECT_PLAN.md`. It reports stale or inconsistent task state.

`doc-hygiene` checks task document structure, duplicate stage log lines, leftover
placeholders, and incomplete review records.

`experiment-monitor` performs a bounded scan of standard experiment directories
for `DONE` and `ERROR` markers. It does not launch experiments; it tells the next
agent whether a task-specific relaunch list is needed.

## Design Rules

- Keep loops short and bounded.
- Always write a run report outside chat.
- Let feedback change the next run through `.agent` files, not through memory of
  the conversation.
- Stop on missing evidence, missing budget, unclear ownership, or a required
  human decision.
- Add scheduling only after the one-shot loop is reliable.
