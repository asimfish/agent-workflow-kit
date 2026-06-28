# Agent Workflow Kit

An installable Git-repo toolkit that drops a complete, enforceable workflow into any
project so that **multiple AI agents work to the same standards, follow one plan,
keep task docs maintained, and do not drift on long tasks**.

You develop a new project, point this kit at it, and the project immediately gets:
coding/doc/commit standards, plan-driven task tracking, a machine-readable task
board, per-task locks, and lifecycle + Git hooks that enforce all of it.

## Why

Four problems this solves when several agents share one repo:

1. **Standards** — commits, docs, and code follow fixed rules (aligned with the
   `super_skill_team` standard).
2. **Plan-driven** — every task is read before work and updated after; the plan is
   the durable source of truth.
3. **Task docs** — each task has a doc that is read on start and updated on progress,
   with explicit maintenance rules so things don't get messy as agents/tasks grow.
4. **No drift on long tasks** — hooks re-inject the current task focus on every
   resume/compaction so a long-running agent never forgets its task or the plan.

## Install

```bash
# 1. get the kit
git clone <this-repo> && cd super_project

# 2. install into your project
./install.sh /path/to/your/project
#   (equivalent to: python3 tools/agentctl.py init /path/to/your/project)
```

`init` will:

- copy the `.agent/` templates, `AGENTS.md`, the Codex/Claude/Cursor hook configs,
  and the GitHub Action into the project (never overwriting existing files);
- distribute `tools/agentctl.py` so the project's hooks can call it;
- install the Git hooks into `.githooks/` and set `git config core.hooksPath .githooks`;
- seed `.agent/board.json` and `.agent/agents.json`.

## Layout (installed into a project)

```text
AGENTS.md                      # agent entry protocol
tools/agentctl.py              # the controller (stdlib-only)
tools/agent_workflow_hook.py   # lifecycle hook bridge (Codex/Claude/Cursor)
.githooks/{pre-commit,commit-msg,pre-push}
.codex/hooks.json .claude/settings.json .cursor/hooks.json
.github/workflows/agent-workflow-check.yml
.agent/
  PROJECT_PLAN.md   TASKS.md            # human-readable plan + index
  board.json        agents.json         # machine-readable board + agent registry
  tasks/  decisions/  handoffs/  bus/ gates/ logs/
  rules/  (code / documentation / github / agent-operating standards)
  state/  (current_session.json, locks/  — gitignored, local only)
```

## Commands

```text
agentctl init [path]                 scaffold + distribute agentctl + git hooks
agentctl task create --id --title [--owner --scope --deps]
agentctl agents add --id --role [--backend --scope --tools --model]
agentctl start --task --agent [--scope --force]   read receipt + lock + in_progress
agentctl focus [--task]              print task goal/scope/TODO (re-read anytime)
agentctl progress --note             append a stage note (task doc + log + board)
agentctl complete --summary [--tests] completion record + release lock -> review
agentctl gate approve|reject --task --by [--note]   review -> done / blocked
agentctl handoff create --from --to --summary [--artifact]   write task packet
agentctl handoff list|show|mark                       inspect or close packets
agentctl refresh                     re-record doc hashes after plan/rules changed
agentctl board [--json]              show the task board
agentctl task show <id>              show one task
agentctl check --mode <m> [--message-file f] [--commit-range r] [--json]
agentctl status [--json]             show the current session
```

`check` modes: `manual` (lifecycle hook), `pre-commit`, `commit-msg`, `pre-push`, `ci`.
Exit codes: `0` ok, `1` violations, `2` usage error, `3` no active session.

## Core loop

1. Supervisor updates `PROJECT_PLAN.md` and the board (`agentctl task create`).
2. Supervisor writes a task doc per bounded unit of work.
3. Worker runs `agentctl start` (acquires the lock, checks write-scope conflicts,
   injects the task focus) before editing.
4. Worker records progress with `agentctl progress`.
5. Worker creates task packets with `agentctl handoff create` when outputs feed downstream tasks.
6. On resume/compaction the session-start hook re-injects the focus; run
   `agentctl focus` to re-anchor a long task at any time.
7. Worker runs `agentctl complete` (task -> `review`, lock released).
8. Reviewer runs `agentctl gate approve` (task -> `done`, plan box auto-checked).
9. Git hooks verify session, doc updates, commit format, task IDs, and review/done
   state before commit/push.

## Multi-agent split (example)

For data collection split across agents, create disjoint write scopes
(Fan-out/Fan-in):

```bash
agentctl task create --id T-101 --title "collect 1-20"  --owner agent1 --scope data/raw/001-020
agentctl task create --id T-102 --title "collect 21-40" --owner agent2 --scope data/raw/021-040
agentctl task create --id T-103 --title "collect 41-60" --owner agent3 --scope data/raw/041-060
agentctl task create --id T-199 --title "merge + validate" --owner supervisor --scope data/manifest
```

`agentctl start` refuses to start a task whose write scope overlaps another
in-progress task owned by a different agent, so agents do not clobber each other.

## Status machine

```text
todo -> ready -> in_progress -> review -> approved -> done
                         └----> blocked        └----> failed
```

## Enforcement (three layers)

- **Lifecycle hooks** (`tools/agent_workflow_hook.py`, wired via the three hook
  configs): inject the protocol + focus on session start, block mutating tools when
  no task session is active, remind to progress/complete on stop.
- **Git hooks** (`.githooks/`): `pre-commit` (active session + staged doc updates +
  secret scan), `commit-msg` (Conventional Commits + task ID), `pre-push`
  (task IDs + tasks must be review/approved/done; done must have a completion record
  and a checked plan box).
- **GitHub Action**: runs `agentctl check --mode ci` to catch bypassed local hooks.

See `docs/enforcement.md` and `docs/workflow.md` for details.
