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
  checkpoints.json
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
python3 tools/agentctl.py loop auto --checkpoint experiment-check --once
python3 tools/agentctl.py loop cycle --checkpoint experiment-check --cycles 3 --interval 300
```

One-shot runs and explicitly bounded cycles are supported. `loop cycle` requires
a finite `--cycles` count (maximum 100), honors the checkpoint policy, and stops
on failure unless `--continue-on-failure` is explicit. There is still no
background daemon; normal continuation comes from workflow checkpoints:

- `work-start`: runs `daily-plan-triage` after `agentctl work` starts or resumes a task.
- `pre-finish`: runs strict `doc-hygiene` before a task can move to review.
- `post-finish`: writes a final non-strict `doc-hygiene` memory report after review.
- `experiment-check`: runs `experiment-monitor` when an experiment task asks for it.

Built-in cron management, worktree pools, connector loops, and automatic
experiment launches are intentionally out of scope until checkpoint loops are
proven in real projects.

Each run writes:

```text
.agent/loops/runs/YYYYMMDD-HHMMSS-<loop-id>.md
.agent/loops/runs/YYYYMMDD-HHMMSS-<loop-id>-2.md  # if a same-second run already exists
.agent/loops/state.json
```

The run report records what was read, what happened, which checks ran, what
feedback was produced, what memory changed, and what should happen next.
Checkpoint state is also recorded in `.agent/loops/state.json`, so the next
cycle can see the latest checkpoint status and report paths.

## Feedback Link

Loop runs are chained: each run reads the previous run's outcome and each
failing checkpoint leaves a durable work item. No scheduling daemon is
involved; the feedback lives in `.agent/` files.

Previous-state injection:

- Every run report contains a `- Previous:` line with the prior run's status
  and report path (`- Previous: none recorded` on the first run).
- Built-in loops compare the current status against the previous one and add a
  feedback line: issues resolved since the last run, issues persisting since
  the last run, or a regression after a previously successful run.

Checkpoint follow-up packets:

- When a checkpoint aggregates to `failed`/`blocked`, or to `partial` under
  strict mode, it writes a `loop-follow-up` packet into the bus inbox of the
  active task (`supervisor` when no session is active).
- Re-running a still-failing checkpoint does not create a second packet; the
  open packet is updated in place and its `occurrences` counter increments.
- When the same checkpoint later aggregates to `success`, its open follow-up
  packets are marked `done` and moved to `.agent/bus/done/` automatically.
- The current open packet id is mirrored in `.agent/loops/state.json` under
  `checkpoints.<name>.open_follow_up`.
- `daily-plan-triage` lists open loop follow-ups in its Feedback section, so
  the next work cycle starts from them. A typical resolution is: fix the
  reported checks, then re-run
  `python3 tools/agentctl.py loop auto --checkpoint <name> --once --force`
  to let the checkpoint auto-close its packet.

Escalation:

- When an open follow-up packet keeps failing and its `occurrences` counter
  reaches the checkpoint's `escalate_after` threshold (default 3, configurable
  per checkpoint in `.agent/loops/checkpoints.json`), the packet is flagged
  `escalated` exactly once. Escalation means: stop retrying silently, a human
  decision is needed.
- Escalated packets are surfaced as check problems: `daily-plan-triage` lists
  them in its Checks section (the run turns `partial`), and
  `agentctl check --mode manual` / `--mode ci` fail while one is open.
- `agentctl finish` (and `complete`) refuse while an escalated packet targets
  the active task. Either fix the underlying failures — a successful checkpoint
  still auto-closes escalated packets — or re-run with `--ack-escalations` to
  record a deliberate human override (`acknowledged_by` plus a note is written
  into the packet).

## Custom Loop Commands

Loops whose id is not built-in can declare executable checks directly in the
contract. Put one fenced `loop-check` block inside the loop file:

````markdown
## Check

```loop-check
timeout: 120
max-output: 2000
# comments are allowed
$ python3 -m py_compile tools/agentctl.py
$ pytest -q tests/smoke
```
````

Rules:

- `$ <command>` lines run in order, from the repository root, via the shell.
- `timeout:` is the per-command limit in seconds (default 120, max 3600).
- `max-output:` caps captured output per failing command (default 2000 chars).
- All commands exit 0 -> the run is `success`; any non-zero exit or timeout ->
  `failed`. Failing commands get their capped output in the Feedback section.
- Declare exactly one block. A malformed block makes the run `failed` and is
  also reported by `agentctl check`.
- Built-in loop ids (`daily-plan-triage`, `doc-hygiene`, `experiment-monitor`)
  keep their built-in behavior; a `loop-check` block on them is a check error.
- Custom loops participate in the feedback link like built-ins: reports carry
  a `Previous:` line, and checkpoint failures create follow-up packets.

`project-check` ships as an installed example; edit its command block to run
project-specific checks, and wire it into `.agent/loops/checkpoints.json` if it
should run at a workflow checkpoint.

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
