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
  state/          # local only, gitignored
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
7. Commit with Conventional Commits and a task ID.
8. Push only after Git hooks pass.

`agentctl work` and `agentctl finish` automatically run checkpoint loops, so the
agent does not need to remember separate triage and document hygiene commands.

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
python3 tools/agentctl.py doctor
```

## System Modules

| Module | Files | Role |
|---|---|---|
| Entry protocol | `AGENTS.md`, `.agent/WORKFLOW_ENTRY.md` | Tells every agent how to start from the same workflow. |
| Plan and tasks | `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, `.agent/tasks/*.md`, `.agent/board.json` | Durable plan, task contracts, status, and progress. |
| Loop runtime | `.agent/loops/*`, `agentctl loop ...` | Bounded Trigger -> Execute -> Check -> Feedback -> Memory -> Next cycles. |
| Controller | `tools/agentctl.py` | Starts tasks, records notes, finishes tasks, runs checks and loops. |
| Lifecycle hooks | `tools/agent_workflow_hook.py`, `.codex/`, `.claude/`, `.cursor/` | Injects workflow context and blocks mutating actions when no task is active where supported. |
| Git hooks | `.githooks/` | Enforces commit format, task IDs, staged workflow docs, secret checks, and push gates. |
| CI gate | `.github/workflows/agent-workflow-check.yml` | Catches bypassed local checks on GitHub. |
| Handoffs | `.agent/handoffs/`, `.agent/bus/` | File-based packets for multi-agent task handoff. |

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
agentctl loop list|show|run <id> --once             inspect or run one loop
agentctl loop auto --checkpoint <name> --once       run checkpoint policy
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
escalated follow-up packets, task-board status counts, checkpoint memory, and
the same base conditions as `agentctl check --mode manual`. It exits nonzero
when a real workflow problem needs attention.

## Regression Tests

```bash
python3 -m unittest discover -s tests
```

`tests/test_loop_workflow.py` installs the kit into fresh temporary Git
projects and replays the full loop feedback chain: custom loop failure,
follow-up packet creation, escalation, check/finish blocking, fix with
auto-close, and the `--ack-escalations` override. CI runs the same tests on
every push and pull request.

## Current Boundaries

The current design intentionally does not include:

- a background daemon or cron scheduler;
- automatic worktree pools;
- external connector loops;
- automatic expensive experiment launches;
- automatic merge to protected branches.

The system is ready for project-level use. More autonomous scheduling should be
added only after the checkpoint loops are reliable in real repositories.

## More Detail

- `docs/workflow.md`: full workflow reference.
- `docs/loop-engineering.md`: loop contract and checkpoint model.
- `docs/enforcement.md`: hook and GitHub enforcement layers.
- `.agent/rules/github-standards.md`: commit, push, and PR standards.
