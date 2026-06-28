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
| Task lifecycle | `start -> progress -> complete (review) -> gate approve (done)`; status machine `todo->ready->in_progress->review->approved->done` (branches `blocked`/`failed`) |
| Long-task anti-drift | lifecycle hook re-injects the current task focus on resume/compaction |
| Three-layer enforcement | lifecycle hooks (process), git hooks (commit/push gate), GitHub Action (remote backstop) |

## Interaction model

The primary interface is the `agentctl` CLI plus automatic hooks, across two roles.

### Human (Supervisor / Reviewer) — board + approval + dispatch

```bash
agentctl task create --id T-101 --title "collect 1-20" --owner agent1 --scope data/raw/001-020
agentctl board                                   # see all task states
agentctl gate approve --task T-101 --by you      # review -> done
```

### Agent (Worker) — claim + update + deliver

```bash
agentctl start --task T-101 --agent agent1       # lock + read plan + in_progress
agentctl focus                                   # re-read the current task anytime
agentctl progress --note "collected 1-10"
agentctl complete --summary "..." --tests "..."  # -> review
```

### Automatic (hooks, no manual step)

- Session start / resume / compaction -> inject required reading + current task focus.
- Before a mutating tool -> block if no task session is active.
- Session stop -> remind to record progress or complete (updates the plan).
- `git commit` -> Conventional Commits + task ID + staged doc updates + secret scan.
- `git push` -> tasks must be review/approved/done; a `done` task must be checked off.

## End-to-end (multi-agent collection)

```text
Supervisor: write PLAN -> task create T-101/T-102/T-103 (disjoint write scopes)
agent1/2/3: each `start` (auto lock; conflicting scope is refused) -> focus
            -> work -> progress -> complete (-> review)
Reviewer:   gate approve each task -> done (plan auto-checked)
Commit/push: git hooks reject non-compliant commits automatically
```

## Command reference

```text
init  start  focus  progress  complete  gate  refresh  board  task  agents  check  status
```

See `workflow.md` for the core loop and `enforcement.md` for the enforcement model.
