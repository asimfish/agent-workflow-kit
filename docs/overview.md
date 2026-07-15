# System Overview

Agent Workflow Kit is an installable Git-repo toolkit that gives any project an
enforceable, plan-driven workflow for multiple AI agents. It has two layers:

- **Global kit** — this repo: templates, the `agentctl` controller, hook configs,
  and the installer.
- **Project runtime** — the `.agent/` directory created inside each target project:
  plan, tasks, board, agent registry, locks, gates, logs.

## Features

| Area | What it does |
|------|--------------|
| Install & distribute | `install.sh` / `agentctl init`: copy templates, distribute `agentctl.py`, install git hooks into `.githooks/` and set `core.hooksPath`, seed `board.json`/`agents.json` |
| Standards | `.agent/rules/`: code (stdlib-only, exit codes, confidence tags), documentation (source-of-truth, status machine), GitHub (Conventional Commits, branches, safety), agent-operating (6 team patterns + decision tree). Aligned with `super_skill_team` |
| Plan / tasks / board | `PROJECT_PLAN.md` (human plan), `TASKS.md` (index), `board.json` (machine board), `tasks/*.md` (task contracts) |
| Multi-agent runtime | `agents.json` (profiles/write-scope), `locks/` (per-task locks), write-scope conflict detection so agents don't clobber each other |
| Handoffs / bus | `.agent/bus/` stores machine-readable task packets; `.agent/handoffs/` stores human-readable cross-agent handoff notes |
| Task lifecycle | `work -> note -> finish (review) -> independent gate approve (done)`; status machine `todo->ready->in_progress->review->approved->done` (branches `blocked`/`failed`) |
| Long-task anti-drift | lifecycle hook re-injects the current task focus on resume/compaction |
| Three-layer enforcement | lifecycle hooks (process), git hooks (commit/push gate), GitHub Action (remote backstop) |

## Interaction model

The primary human interface is the project documents plus one short agent prompt:

```text
按 .agent 规范开始工作。
```

`.agent/WORKFLOW_ENTRY.md` is the single startup contract. Hooks and adapter
files inject it into Codex, Claude Code, Cursor, and similar tools.

### Human (Supervisor / Reviewer) — optional steering

Humans normally inspect and edit `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, and
`.agent/tasks/*.md` when direction changes. They do not need to run commands for
ordinary agent work.

### Agent (Worker) — claim + update + deliver

Agents translate the short prompt into the controller loop:

```bash
agentctl work --agent agent1                     # resume or auto-claim assigned work
agentctl work --agent agent1 --auto-create --title "current request" --scope data/raw/001-020
agentctl focus                                   # optional: re-read the current task anytime
agentctl note "collected 1-10"
agentctl handoff create --from T-101 --to T-199 --summary "slice ready" --artifact data/raw/001-020/manifest.json
agentctl finish --summary "..." --tests "..."    # -> review
```

### Automatic (hooks, no manual step)

- Session start / resume / compaction -> inject required reading + current task focus.
- Before a mutating tool -> block if no task session is active.
- Session stop -> remind to record progress or complete (updates the plan).
- `git commit` -> Conventional Commits + task ID + staged doc updates + secret scan.
- `git push` -> tasks must be review/approved/done; a `done` task must be checked off.

## End-to-end (multi-agent collection)

```text
Supervisor: write PLAN or ask supervisor agent to split T-101/T-102/T-103
agent1/2/3: each `work` (auto lock; conflicting scope is refused) -> focus
            -> work -> note -> finish (-> review)
Reviewer:   separate active review task + gate approve -> done (plan auto-checked)
Commit/push: git hooks reject non-compliant commits automatically
```

## Command reference

```text
init  work  note  finish  start  focus  progress  complete  gate  handoff  refresh  board  task  agents  check  status
```

See `workflow.md` for the core loop and `enforcement.md` for the enforcement model.
