# Agent Workflow Kit

Project-level workflow kit for AI agents. Install it into any Git repository so
Codex, Claude Code, Cursor, or another coding agent can follow the same plan,
task docs, loop checks, and GitHub standards without the human repeating the
workflow every time.

Repository:

```text
https://github.com/asimfish/super_project
```

## Quick Start

### Option A: ask an agent to install it

Start an agent inside your target project and say:

```text
Install https://github.com/asimfish/super_project.git into this project.
```

After installation, start normal work with:

```text
按 .agent 规范开始工作。
```

The agent should read the installed `AGENTS.md` and `.agent/WORKFLOW_ENTRY.md`,
then run the workflow by itself.

### Option B: install manually

```bash
git clone https://github.com/asimfish/super_project.git
cd super_project
./install.sh /path/to/your/project
```

This is equivalent to:

```bash
python3 tools/agentctl.py init /path/to/your/project
```

Installation is preflighted before any file is written. Existing `AGENTS.md`, PR
template, and Codex/Claude/Cursor hook JSON are merged in managed sections while
project content is preserved. Project-owned `.agent/` plans, tasks, rules, and
runtime history are seeded only when absent. Re-running `init` is idempotent and
upgrades unchanged kit-managed tools using `.agent/install-manifest.json`.

If a managed tool or workflow file was edited locally, installation stops before
making partial changes. Inspect the diff first, then explicitly replace only the
kit-managed files when appropriate:

```bash
python3 tools/agentctl.py init /path/to/your/project --force-managed
```

## What It Installs

The target project gets a local `.agent/` system plus hooks and controller files:

```text
AGENTS.md
tools/agentctl.py
tools/agent_workflow_hook.py
.githooks/{pre-commit,commit-msg,pre-push}
.codex/hooks.json
.claude/settings.json
.cursor/hooks.json
.github/workflows/agent-workflow-check.yml
.agent/
  WORKFLOW_ENTRY.md
  PROJECT_PLAN.md
  TASKS.md
  board.json
  agents.json
  tasks/
  loops/
  rules/
  logs/
  handoffs/
  decisions/
  gates/
  state/          # local only, gitignored; includes generated SESSIONS.md
```

The installed `.agent/` directory belongs to that project. It is not a global
agent memory.

## Normal Human Interaction

Humans do not need to run workflow commands during normal work.

You mainly inspect and edit:

```text
.agent/PROJECT_PLAN.md
.agent/TASKS.md
.agent/tasks/*.md
.agent/rules/*.md
.agent/loops/checkpoints.json
```

If direction, priority, scope, or acceptance criteria are wrong, edit those
files. The next agent run must re-read them and continue from the durable state.

The shortest normal prompt remains:

```text
按 .agent 规范开始工作。
```

## Multiple Conversations In One Project

You may open several Codex, Claude Code, or Cursor conversations in the same
project directory and give each one the same short prompt. No session-management
command is required from the human.

The kit derives a private session key from the client runtime, so a second
conversation does not silently resume the first conversation's task. Each
conversation atomically publishes its task, scope, heartbeat, and claimed files
under the Git common directory. Hooks aggregate that state into the local
gitignored file `.agent/state/SESSIONS.md`, show it at session start, and refresh
it during work.

Forked or cloned conversations are isolated as separate runtime instances. A
persisted workflow ID is bound to the host runtime that created it, so a child
that inherits the parent's environment cannot use that stale ID to resume the
parent's task. Native `parent`/`fork`/`branch`/`clone` hook metadata supplies an
additional instance key when needed; `.agent/state/SESSIONS.md` shows the hashed
fork lineage without storing the raw parent ID. If a client reports the same
parent and child ID and then fails to propagate the instance established by its
`SessionStart` hook, mutating hooks fail closed.

The coordination policy is deliberately small:

- Disjoint task scopes may work concurrently in one checkout.
- The same task, overlapping scopes, an out-of-scope path, or a file claimed by
  another session is blocked.
- Git index, HEAD, branch, merge, and push operations are exclusive per checkout.
  Parallel commit-producing work should use `agentctl worktree create`.
- A missing heartbeat marks a session stale but does not discard its claim.
  After inspection, an agent can explicitly release it and resume the existing
  task; task docs and working files are preserved.
- Task/plan/log transitions are serialized with an OS advisory lock, and each
  session writes only its own atomically replaced JSON record.

A full Git clone has a different Git common directory, so it never reads or
writes the source clone's live session records even when both processes expose
the same host session ID. Committed plan/task files are only a snapshot across
independent clones; use normal fetch/branch/PR integration to exchange later
decisions. Live cross-clone coordination is intentionally not inferred.

Committed `.agent` plans and task docs remain the durable memory. The generated
session view is only live coordination, avoiding constant Git conflicts in a
shared Markdown file.

## Agent Work Cycle

When an agent starts work, it should follow this cycle:

1. Read `AGENTS.md`, `.agent/WORKFLOW_ENTRY.md`, the project plan, task index,
   rules, and relevant task doc.
2. Run `agentctl work --agent <agent-name>`.
3. If no matching task exists, create one with
   `agentctl work --agent <agent-name> --auto-create --title "..." --scope "..."`.
4. Work only inside the active task write scope.
5. Record meaningful progress with `agentctl note "..."`.
6. Finish with `agentctl finish --summary "..." --tests "..."`.
7. For an independently gated task, a registered supervisor/reviewer starts a
   separate planning/review task and runs `agentctl gate approve --task <worker-task>
   --by <reviewer>` from that active reviewer session. The worker and a spoofed
   `--by` value cannot approve the worker task. The controller fingerprints
   host-issued Codex/Claude/Cursor runtime identifiers, records every runtime
   that participated in implementation, and rejects any of those runtimes as
   the reviewer.
8. Commit with Conventional Commits and a task ID.
9. Push only after Git hooks pass.

`agentctl work` and `agentctl finish` automatically run checkpoint loops, so the
agent does not need to remember separate triage and document hygiene commands.

## Advanced Supervisor Guidance

Use this when a stronger planning model should guide an implementation worker.
The intended human prompt can be this short:

```text
按 .agent 规范开始工作。你作为 fable/supervisor，负责拆解计划并下发给 codex 的 gpt5.5xhigh（他的会话ID是 xxx）执行。
```

Fable should translate that into durable project state:

1. Create or update task documents for the implementation work.
2. Register the target worker session when needed:

   ```bash
   python3 tools/agentctl.py agents add \
     --id codex-gpt55xhigh \
     --role "implementation worker" \
     --backend codex \
     --model gpt-5.5 \
     --reasoning-effort xhigh \
     --session-id xxx
   ```

3. Send the plan and immediately run one bounded worker turn in that specific
   Codex session:

```bash
python3 tools/agentctl.py guidance create \
  --from-agent fable \
  --to-agent codex-gpt55xhigh \
  --to-model gpt-5.5 \
  --to-reasoning-effort xhigh \
  --to-session xxx \
  --task T-101 \
  --summary "Implement the benchmark runner in three phases" \
  --plan-file .agent/plans/T-101-fable-plan.md \
  --dispatch
```

The plan is stored as a durable packet under `.agent/bus/` and mirrored into
`.agent/handoffs/`. `--dispatch` then runs the supported non-interactive Codex
continuation command for the target session:

```text
codex exec resume <SESSION_ID> -
```

The guidance prompt is UTF-8 text supplied on standard input rather than a
command-line argument.

The call is synchronous and bounded (default timeout: 7200 seconds). It inherits
the target Codex session's configured trust, approval, and sandbox policy; the
kit never adds a dangerous bypass flag. The worker's final message and raw
receipt stay under the gitignored `.agent/state/dispatch/`, while the guidance
packet records transport status, attempt count, timestamps, and exit code.

After the turn returns, the worker commits the bounded turn and leaves its
checkout clean. Fable then verifies the whole state transition:

```bash
python3 tools/agentctl.py guidance verify <packet-id> --by fable \
  --target <worker-worktree>
```

This does not trust a successful process exit by itself. It requires a signed
immutable guidance contract and receipt matching the target session/model/effort,
acknowledgement by the exact worker for the same task, and a new task completion
record with tests in `review`, `approved`, or `done`. Fable runs this from its
active planning/review session; the worker cannot self-approve by changing `--by`.
Signed decisions bind the committed worker HEAD/tree and evidence hashes, and
survive worktree release under the Git common directory at
`agent-workflow/acceptance/`.

When the matching Codex/GPT-5.5 worker starts or resumes, it still runs:

```bash
python3 tools/agentctl.py work --agent codex-gpt55xhigh \
  --model gpt-5.5 --reasoning-effort xhigh --session-id xxx
```

the focus output automatically includes any unacknowledged guidance addressed to
that exact worker session, including the packet path and a plan excerpt. If the
guidance is bound to the active task, `agentctl check --mode manual` and
`agentctl finish` refuse to pass until the worker records that it incorporated
the plan:

```bash
python3 tools/agentctl.py guidance ack <packet-id> --by codex-gpt55xhigh
```

This keeps the model hierarchy file-based: the stronger model does planning and
review direction; Codex still owns the task doc, implementation, verification,
and final commit. After the dispatched turn returns, Fable inspects the task
document, diff, and verification evidence, then either sends another bounded
guidance packet or gates the task. It must not acknowledge guidance or approve a
task on the worker's behalf.

Use `guidance dispatch <packet-id> --dry-run` to inspect the exact resume command
without starting Codex. Omit `--dispatch` for the original asynchronous,
file-only mode. File-only guidance can target an agent without a session ID;
active dispatch requires a registered or explicit target session.

The human-facing phrase `gpt5.5xhigh` is intentionally translated into two
runtime settings: model `gpt-5.5` and reasoning effort `xhigh`. Do not pass the
combined phrase as a Codex model ID.

## Loop Design

This kit treats a loop as a bounded feedback cycle, not an infinite background
agent. Every loop answers six questions:

| Link | Question |
|---|---|
| Trigger | Who or what starts this cycle? |
| Execute | What does the agent do, and what may it write? |
| Check | How is the result verified? |
| Feedback | How does this result affect the next run? |
| Memory | Where is the durable record written? |
| Next | Stop, continue later, hand off, or ask a human? |

Checkpoint policy lives in:

```text
.agent/loops/checkpoints.json
```

Default checkpoints:

| Checkpoint | Runs | When | Strict |
|---|---|---|---|
| `work-start` | `daily-plan-triage` | after `agentctl work` starts or resumes a task | no |
| `pre-finish` | `doc-hygiene` | before a task moves to review | yes |
| `post-finish` | `doc-hygiene` | after a task moves to review | no |
| `experiment-check` | `experiment-monitor` | explicit experiment/benchmark monitoring | no |

Each loop run writes:

```text
.agent/loops/runs/YYYYMMDD-HHMMSS-<loop-id>.md
.agent/loops/state.json
```

The state file records the latest loop status and checkpoint status so the next
cycle can use durable memory outside chat.

### Feedback Packets

Checkpoint failures are converted into durable work items:

- a failing or blocked checkpoint creates one `loop-follow-up` packet in
  `.agent/bus/inbox/<task>/`;
- repeated failures update the same packet instead of flooding the inbox;
- the packet tracks `occurrences`, latest report paths, and the checkpoint name;
- a later successful checkpoint auto-closes the packet into `.agent/bus/done/`.

This is the main feedback link between one loop cycle and the next. Agents do
not need chat memory to know what failed last time.

### Escalation

Each checkpoint can define `escalate_after` in `.agent/loops/checkpoints.json`.
Default value is `3`.

When a follow-up keeps failing until that threshold:

- the packet is marked `escalated`;
- `daily-plan-triage` and `agentctl check --mode manual` surface it as a problem;
- `agentctl finish` / `complete` refuse to finish the target task;
- fixing the underlying check and rerunning the checkpoint auto-closes it;
- `--ack-escalations` records an explicit override when a human deliberately
  accepts the risk.

### Custom Loop Checks

Projects can add their own loop contracts under `.agent/loops/`. A custom loop
can declare executable checks in a fenced `loop-check` block:

````markdown
```loop-check
timeout: 120
max-output: 2000
$ python3 -m py_compile tools/agentctl.py
$ pytest -q
```
````

`agentctl loop run <id> --once` executes those commands from the repository root.
All commands passing means `success`; any non-zero exit or timeout means `failed`.
Custom loops participate in checkpoint follow-ups and escalation like built-in
loops.

## Built-In Loops

| Loop | Purpose |
|---|---|
| `daily-plan-triage` | Checks `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, `.agent/board.json`, and task docs for stale or inconsistent task state. |
| `doc-hygiene` | Checks task document structure, duplicate stage logs, leftover placeholders, and empty review records. |
| `experiment-monitor` | Bounded scan of standard experiment directories for `DONE` and `ERROR` markers. It does not launch experiments. |
| `project-check` | Installed custom-loop example. Edit its `loop-check` commands for project-specific verification. |

Run loops manually when needed:

```bash
python3 tools/agentctl.py loop list
python3 tools/agentctl.py loop show daily-plan-triage
python3 tools/agentctl.py loop run daily-plan-triage --once
python3 tools/agentctl.py loop auto --checkpoint experiment-check --once
python3 tools/agentctl.py loop cycle --checkpoint experiment-check --cycles 3 --interval 300 --continue-on-failure --max-failures 2
python3 tools/agentctl.py loop status
python3 tools/agentctl.py loop resume
python3 tools/agentctl.py loop stop --reason "owner changed the experiment plan"
python3 tools/agentctl.py doctor
```

### Bounded Loop Cycles

Use `loop cycle` when a checkpoint should keep checking a bounded process, such
as a training job, benchmark, or document hygiene gate:

```bash
python3 tools/agentctl.py loop cycle --checkpoint experiment-check --cycles 6 --interval 600
```

This is the safe continuous-loop mode:

| Link | How `loop cycle` handles it |
|---|---|
| Trigger | A human, agent, cron, or previous task starts a command with an explicit `--cycles` count. |
| Execute | The existing checkpoint loops run once per cycle. |
| Check | Each cycle reuses the checkpoint's loop checks and strictness policy. |
| Feedback | Failures update one follow-up packet; repeated failures can escalate. |
| Memory | Every cycle writes a loop report; runtime progress and recent events live in `.agent/loops/state.json`. |
| Next | It sleeps cooperatively for `--interval`, starts the next cycle, then stops at the count, failure budget, escalation, or stop request. |

Failures stop the cycle by default. Use `--continue-on-failure` only when you
want repeated failures to accumulate into escalation evidence. Use `--force` when
you deliberately want every cycle to bypass checkpoint debounce. An escalated
follow-up always blocks the runtime even with `--continue-on-failure`.
Setting `--max-failures` above 1 also requires `--continue-on-failure`.

Cycle execution is resumable but not a daemon:

- `loop status` reports the latest runtime ID, owner, progress, failures, and stop reason;
- a second `loop cycle` is rejected while a live or interrupted runtime is unfinished;
- `loop run`, `loop auto`, and `loop cycle` share one durable execution lease, so a one-shot command cannot bypass an active cycle; a killed one-shot retains an `interrupted` lease until its result is explicitly reconciled;
- if the runner disappears between cycles, `loop status` marks it safely `interrupted` and `loop resume` continues at the next unfinished cycle;
- each child waits behind a launch gate until its PID is persisted; if the runner disappears during a check, the runtime retains the in-flight cycle and command identity, blocks `resume`, and requires side-effect inspection followed by `loop stop --ack-inflight --reason "<reconciliation>"` after the command exits;
- runtime claims and control transitions use OS-released advisory locks and compare-and-set, while JSON snapshots use atomic file replacement;
- on Linux, macOS, and Windows, persisted owners and child commands pair their PID with a native process-birth marker, so a reused PID cannot keep a crashed runtime or lease alive;
- a POSIX process group whose recorded leader has exited is treated as unverifiable rather than trusted by numeric ID alone: automatic replay stays blocked, while inspected state can be closed explicitly with `loop stop --ack-inflight --reason "<reason>"`;
- a `loop stop` request is cooperative and does not kill the command currently being checked;
- `--max-failures` adds a hard failure budget, while `--cycles` remains mandatory and capped at 100.

Commands used in repeatable loops should be idempotent. The runtime prevents an
unknown in-flight command from being started twice automatically, but it cannot
roll back side effects written before a process was interrupted.

An external scheduler may invoke these bounded commands, but the kit does not
install cron jobs or keep a background process alive.

### Managed Worktree Leases

Parallel agents should not share one Git index or working directory. After the
supervisor commits the task plan and reaches a clean baseline, it can allocate a
dedicated worktree:

```bash
python3 tools/agentctl.py worktree create --task T-101 --agent codex-worker
python3 tools/agentctl.py worktree list
```

The command creates a task-scoped branch and prints the worker path. Lease state
lives under the repository's shared Git common directory, so every linked
worktree sees the same allocation. Creation applies the same owner, dependency,
write-scope, and `todo`/`ready` claim rules as worker startup. It also refuses
uncommitted task documents, dirty baselines, duplicate task/agent leases,
existing branches, paths overlapping another checkout, and any scope overlap
with a nonreleased lease, including leases using the same agent name. A managed
worker cannot override its leased task, agent, or scope during startup.

After the worker commits or removes all changes, run release from another
worktree:

```bash
python3 tools/agentctl.py worktree release <lease-id>
```

Release refuses the current or a dirty worktree. It removes only the linked
working directory and preserves the branch and commits. An externally moved
checkout is tracked by its stable Git admin directory and stops for inspection.
Prunable or already-missing metadata is refused by default because Git cannot
prove the checkout was deleted; after inspecting the path, acknowledge cleanup
explicitly:

```bash
python3 tools/agentctl.py worktree release <lease-id> --ack-missing
```

The kit never force removes a worktree or deletes its branch.

### Harness Evaluation

Workflow and harness changes use deterministic baseline/candidate evaluation.
The supervisor keeps the suite policy in its checkout and runs the same argv-only
verifiers against two clean target worktrees:

```bash
python3 tools/agentctl.py eval run workflow-integrity --target <baseline-path> --json
python3 tools/agentctl.py eval run workflow-integrity --target <candidate-path> --json
python3 tools/agentctl.py eval compare --baseline <baseline-id> --candidate <candidate-id>
python3 tools/agentctl.py eval gate --baseline <baseline-id> --candidate <candidate-id> \
  --by <reviewer>
```

Suites live in `.agent/evals/suites.json` or in a supervisor-only external file.
Every suite contains `held_in` cases for known weaknesses and `held_out` cases
for non-regression. Reports include suite and commit identity, dirty state,
timeouts, bounded output, required artifacts, split scores, and a
supervisor-local integrity signature. Acceptance
requires both splits not to regress and every required case to pass. The
candidate does not control the suite or write the decision. See
`docs/harness-evaluation.md` for the schema and trust boundary.

## System Modules

| Module | Files | Role |
|---|---|---|
| Entry protocol | `AGENTS.md`, `.agent/WORKFLOW_ENTRY.md` | Tells every agent how to start from the same workflow. |
| Plan and tasks | `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, `.agent/tasks/*.md`, `.agent/board.json` | Durable plan, task contracts, status, and progress. |
| Loop runtime | `.agent/loops/*`, `agentctl loop ...` | Bounded Trigger -> Execute -> Check -> Feedback -> Memory -> Next cycles. |
| Worktree leases | Git common dir, `agentctl worktree ...` | Isolates parallel agents with shared allocation state and non-destructive release. |
| Harness evaluation | `.agent/evals/suites.json`, `.agent/state/evals/*`, `agentctl eval ...` | Keeps versioned policy separate from local signed evidence and compares held-in/held-out results before acceptance. |
| Controller | `tools/agentctl.py` | Starts tasks, records notes, finishes tasks, runs checks and loops. |
| Lifecycle hooks | `tools/agent_workflow_hook.py`, `.codex/`, `.claude/`, `.cursor/` | Injects workflow context and blocks mutating actions when no task is active where supported. |
| Git hooks | `.githooks/` | Enforces commit format, task IDs, staged workflow docs, secret checks, and push gates. |
| CI gate | `.github/workflows/agent-workflow-check.yml` | Catches bypassed local checks on GitHub. |
| Handoffs | `.agent/handoffs/`, `.agent/bus/` | File-based packets for multi-agent task handoff. |
| Supervisor guidance | `.agent/bus/`, `.agent/handoffs/`, `agentctl guidance ...` | Higher-capability planning models can send durable task guidance to worker agents such as Codex. |

## GitHub Standards

Commits must follow Conventional Commits and include a task ID:

```text
feat(scope): short imperative summary

Refs: T-001
```

Local hooks enforce:

- staged code/data changes must include matching `.agent` updates;
- commit messages must be Conventional Commits;
- every pushed commit must reference a task ID;
- pushed tasks must be `review`, `approved`, or `done`;
- `done` tasks must have a completion record and checked plan box;
- staged content is scanned for obvious secrets.

## Multi-Agent Work

Split work by non-overlapping write scope. Example:

```bash
python3 tools/agentctl.py task create --id T-101 --title "collect 1-20" \
  --owner agent1 --scope data/raw/001-020
python3 tools/agentctl.py task create --id T-102 --title "collect 21-40" \
  --owner agent2 --scope data/raw/021-040
python3 tools/agentctl.py task create --id T-103 --title "collect 41-60" \
  --owner agent3 --scope data/raw/041-060
python3 tools/agentctl.py task create --id T-199 --title "merge and validate" \
  --owner supervisor --scope data/manifest
```

`agentctl work` refuses to start a task if its write scope overlaps another
in-progress task owned by a different agent.

## Common Commands

```text
agentctl init [path]                                install into a project
agentctl work --agent <name>                        resume or claim work
agentctl work --agent <name> --auto-create --title --scope
agentctl focus                                      reprint current task focus
agentctl note "..."                                 record progress
agentctl finish --summary "..." --tests "..."       move task to review
agentctl gate approve|reject --task --by            review gate
agentctl gate reconcile-github --task --by --pr     sync a human-merged PR to done
agentctl guidance create --from-agent --to-agent    send supervisor plan to an agent/session
agentctl guidance create ... --dispatch             send plan and resume the target Codex session
agentctl guidance list|show|ack|dispatch|verify      inspect, execute, and accept/reject guidance
agentctl eval list|run|show|compare|gate             evaluate baseline and candidate worktrees
agentctl loop list|show|run <id> --once             inspect or run one loop
agentctl loop auto --checkpoint <name> --once       run checkpoint policy
agentctl loop cycle --checkpoint <name> --cycles N  run a durable bounded cycle
agentctl loop status|resume|stop                    inspect or control the latest cycle
agentctl board [--json]                             show board
agentctl check --mode manual|pre-commit|commit-msg|pre-push|ci
agentctl doctor [--json]                            diagnose installed workflow health
```

## Diagnostics

Run a read-only health check any time an installed project seems off:

```bash
python3 tools/agentctl.py doctor
```

`doctor` checks core files, Git hook wiring, loop contract validity, open or
escalated follow-up packets, task-board status counts, checkpoint memory, cycle
runtime state, managed worktree leases, and the same base conditions as
`agentctl check --mode manual`.
It exits nonzero when a real workflow problem needs attention.

## Regression Tests

```bash
python3 -m unittest discover -s tests
```

The regression tests install the kit into fresh temporary Git projects. They
replay feedback escalation, failure budgets, cooperative stop, safe and
unknown-result orphan recovery, launch handshakes, one-shot/cycle mutual
exclusion, descendant cleanup, non-destructive Windows PID checks, and supervisor
guidance, managed worktree allocation, and harness evaluation: Fable-style plan creation,
Codex work-start surfacing, finish blocking until acknowledgement, and successful
completion after `guidance ack`, supervisor rejection of transport-only success,
signed acceptance of evidence-complete turns, tampered-receipt rejection, plus
baseline/candidate non-regression gates and tampered-evidence rejection. CI runs
the same tests on every push and pull request.

## Current Boundaries

The current design intentionally does not include:

- a background daemon or cron scheduler;
- automatic worktree pools or branch deletion;
- external connector loops;
- automatic expensive experiment launches;
- automatic merge to protected branches.
- automatic harness mutation or acceptance without held-in/held-out evidence.

The system is ready for project-level use. More autonomous scheduling should be
added only after the checkpoint loops are reliable in real repositories.

## More Detail

- `docs/workflow.md`: full workflow reference.
- `docs/loop-engineering.md`: loop contract and checkpoint model.
- `docs/harness-evaluation.md`: deterministic suite schema and supervisor trust boundary.
- `docs/enforcement.md`: hook and GitHub enforcement layers.
- `.agent/rules/github-standards.md`: commit, push, and PR standards.
