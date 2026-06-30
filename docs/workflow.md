# Workflow

## Core Loop

1. Human or supervisor agent keeps `.agent/PROJECT_PLAN.md` and task docs directionally correct.
2. Human starts a worker with `按 .agent 规范开始工作。` or an equivalent task request.
3. Worker reads `.agent/WORKFLOW_ENTRY.md` and runs `agentctl work --agent <name>` before editing.
4. If no task exists for the current request, the worker runs `agentctl work --agent <name> --auto-create --title "..." --scope "..."`.
5. Worker records phase progress with `agentctl note`.
6. Worker creates handoff packets for downstream tasks with `agentctl handoff create`.
7. Worker completes with `agentctl finish`.
8. Git hooks verify active task context, doc updates, and commit format.

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

## Low-Friction Agent Loop

Humans do not need to send the loop. They can say:

```text
按 .agent 规范开始工作。
```

Agents then run the short loop themselves:

```bash
agentctl work --agent codex
# If no assigned task exists:
agentctl work --agent codex --auto-create --title "current request" --scope "paths/"
agentctl note "short factual progress update"
agentctl finish --summary "what changed" --tests "commands run"
git commit -m "feat(scope): summary" -m "Refs: T-101"
git push
```

`start`, `progress`, and `complete` remain available as explicit low-level commands,
but everyday agents should not need them.

## Human Steering

Humans are not required to run workflow commands during normal development. They
can periodically open `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, and task docs,
then edit direction, scope, priorities, or acceptance criteria. Agents must treat
those edits as updated instructions: re-read the changed files, run `agentctl refresh`,
and continue under the new plan.

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
