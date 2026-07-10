# Workflow

## Core Loop

1. Human or supervisor agent keeps `.agent/PROJECT_PLAN.md` and task docs directionally correct.
2. Human starts a worker with `按 .agent 规范开始工作。` or an equivalent task request.
3. Worker reads `.agent/WORKFLOW_ENTRY.md` and runs `agentctl work --agent <name>` before editing. The command also runs the `work-start` checkpoint loop.
4. If no task exists for the current request, the worker runs `agentctl work --agent <name> --auto-create --title "..." --scope "..."`.
5. Worker records phase progress with `agentctl note`.
6. `agentctl finish` runs `pre-finish` and `post-finish` checkpoint loops for
   document hygiene. Experiment tasks can run `agentctl loop auto --checkpoint
   experiment-check --once` before deciding whether to relaunch jobs.
7. Worker creates handoff packets for downstream tasks with `agentctl handoff create`.
8. Worker completes with `agentctl finish`.
9. Git hooks verify active task context, doc updates, and commit format.

## Loop Contract

Every loop in `.agent/loops/` must close six links:

- Trigger: who or what starts this cycle.
- Execute: which agent acts and what scope it may write.
- Check: how the result is verified.
- Feedback: how the result changes the next run.
- Memory: where durable records are written.
- Next: stop, hand off, continue later, or ask a human.

Loops run one cycle at a time. Continuous behavior is checkpoint-triggered:

```bash
agentctl loop list
agentctl loop run daily-plan-triage --once
agentctl loop auto --checkpoint work-start --once
agentctl loop auto --checkpoint pre-finish --once
agentctl loop auto --checkpoint experiment-check --once
```

This intentionally avoids unbounded token spend while still producing durable
run reports in `.agent/loops/runs/`.

Checkpoint policy is project-local in `.agent/loops/checkpoints.json`. Humans can
edit that file when a project needs different loop mapping, strictness, or
debounce windows.

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

## Supervisor To Codex Dispatch

When the supervisor has a Codex session ID, one bounded producer-reviewer turn
can be started directly:

```bash
agentctl guidance create \
  --from-agent fable \
  --to-agent codex-gpt55xhigh \
  --to-model gpt-5.5 \
  --to-reasoning-effort xhigh \
  --to-session <session-id> \
  --task T-101 \
  --summary "implement the next verified phase" \
  --plan-file .agent/plans/T-101.md \
  --dispatch
```

This closes the loop links as follows:

- Trigger: the supervisor creates a task-scoped guidance packet with
  `--dispatch`.
- Execute: `codex exec resume <SESSION_ID>` sends the plan to the named worker
  session without invoking a shell.
- Check: the worker runs task verification and `agentctl finish`; the dispatch
  process also records its exit code.
- Feedback: the worker acknowledges incorporated guidance, or a failed transport
  leaves the packet ready for `guidance dispatch <packet-id>` retry.
- Memory: task docs and packet metadata are tracked; raw dispatch output stays in
  `.agent/state/dispatch/`.
- Next: the supervisor reviews evidence and either sends one more bounded packet,
  gates the task, or stops for a human decision.

The transport does not weaken target-session permissions and does not create a
background daemon. A worker session should use an isolated worktree when another
agent is actively changing the same repository checkout.

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

## Diagnostics

Use `agentctl doctor` when an installed project does not behave as expected:

```bash
agentctl doctor
agentctl doctor --json
```

It is read-only. It checks required workflow files, Git hook wiring, loop
contracts, open or escalated loop follow-up packets, task-board status counts,
checkpoint memory, and the base manual workflow check.

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
