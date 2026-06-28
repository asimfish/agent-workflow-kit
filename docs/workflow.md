# Workflow

## Core Loop

1. Supervisor writes or updates `.agent/PROJECT_PLAN.md`.
2. Supervisor creates a task document for each bounded unit of work.
3. Worker runs `agentctl start` before editing.
4. Worker records phase progress with `agentctl progress`.
5. Worker creates handoff packets for downstream tasks with `agentctl handoff create`.
6. Worker completes with `agentctl complete`.
7. Git hooks verify active task context, doc updates, and commit format.

## Multi-Agent Split

Use one primary team pattern per project:

- `Pipeline`: strict order, each output feeds the next task.
- `Fan-out/Fan-in`: independent tasks, deterministic merge rule.
- `Supervisor`: default for 2-8 workers.
- `Hierarchical`: large task trees, maximum depth 3.

For data collection like 1-20, 21-40, 41-60:

```text
T-101 agent1 scope=data/raw/001-020
T-102 agent2 scope=data/raw/021-040
T-103 agent3 scope=data/raw/041-060
T-199 supervisor scope=data/manifest + validation report
```

Each worker writes only its scope. The supervisor owns manifest merge and final validation.

## Handoffs

Use handoff packets when one task output becomes another task input:

```bash
agentctl handoff create \
  --from T-101 \
  --to T-199 \
  --summary "raw slice ready for merge" \
  --artifact data/raw/001-020/manifest.json
```

This writes:

- `.agent/bus/outbox/<agent-or-task>/<packet-id>.json`
- `.agent/bus/inbox/<target-task>/<packet-id>.json`
- `.agent/handoffs/<from>-to-<to>.md`

The packet references artifacts by path. It should not copy large outputs.

## Completion Gate

A task is not done until:

- Its task doc has a completion record.
- Verification commands have been run or explicitly marked unavailable.
- Artifacts are listed.
- Follow-ups are recorded.
- The project plan task board is updated.
