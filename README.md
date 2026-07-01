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
git clone https://github.com/asimfish/super_project.git && cd super_project

# 2. install into your project
./install.sh /path/to/your/project
#   (equivalent to: python3 tools/agentctl.py init /path/to/your/project)
```

### Agent Bootstrap

If you give this repository link to an agent, the agent should do the install
itself. A minimal install request is:

```text
Install https://github.com/asimfish/super_project.git into this project.
```

After `install.sh` runs, the target project owns its local `.agent/` directory.
That directory lives in the target project root, not globally.

After installation, humans can start agents with one short prompt:

```text
按 .agent 规范开始工作。
```

English equivalent:

```text
Follow .agent and start work.
```

The installed `AGENTS.md`, `.agent/WORKFLOW_ENTRY.md`, and agent hook configs
tell Codex, Claude Code, Cursor, and similar tools how to translate that short
prompt into the full workflow: read the plan and task docs, run `agentctl work`,
record `note`, `finish` phases, and obey GitHub commit/push rules.

`init` will:

- copy the `.agent/` templates, `AGENTS.md`, the Codex/Claude/Cursor hook configs,
  and the GitHub Action into the project (never overwriting existing files);
- distribute `tools/agentctl.py` so the project's hooks can call it;
- install the Git hooks into `.githooks/` and set `git config core.hooksPath .githooks`;
- seed `.agent/board.json` and `.agent/agents.json`.
- record `.agent/adoption.json` in existing Git repositories so pre-push checks
  apply to new commits after installation, not to old project history.

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
  loops/  (bounded Trigger -> Execute -> Check -> Feedback -> Memory -> Next contracts)
  rules/  (code / documentation / github / agent-operating standards)
  WORKFLOW_ENTRY.md  # one-file entry protocol for all agents
  state/  (current_session.json, locks/  — gitignored, local only)
```

## Template Coverage

Installed projects include templates and rules for the three documents that most
often become messy:

- Plan format: `.agent/PROJECT_PLAN.md` defines fixed sections, task-board row
  format, dependency format, and change-log format.
- Task records: `.agent/tasks/_template.md` defines task contract, write scope,
  stage checklist, stage log format, verification, and completion record.
- GitHub workflow: `.agent/rules/github-standards.md`, `.github/PULL_REQUEST_TEMPLATE.md`,
  and `.githooks/` define Conventional Commits, task IDs, PR fields, staged doc
  updates, secret checks, and push gates.

## Commands

```text
agentctl init [path]                 scaffold + distribute agentctl + git hooks
agentctl task create --id --title [--owner --scope --deps]
agentctl agents add --id --role [--backend --scope --tools --model]
agentctl work --agent [--task]       resume current work or claim next assigned task
agentctl work --agent --auto-create --title --scope   create/claim/start a task
agentctl start --task --agent [--scope --force]   low-level explicit task start
agentctl focus [--task]              print task goal/scope/TODO (re-read anytime)
agentctl note "..."                  shorthand progress note for the active task
agentctl progress --note             low-level progress command
agentctl finish --summary [--tests]  shorthand completion command -> review
agentctl complete --summary [--tests] low-level completion command -> review
agentctl gate approve|reject --task --by [--note]   review -> done / blocked
agentctl handoff create --from --to --summary [--artifact]   write task packet
agentctl handoff list|show|mark                       inspect or close packets
agentctl loop list|show|run <id> --once                inspect or run bounded loops
agentctl loop auto --checkpoint <name> --once          run checkpoint loop policy
agentctl refresh                     re-record doc hashes after plan/rules changed
agentctl board [--json]              show the task board
agentctl task show <id>              show one task
agentctl check --mode <m> [--message-file f] [--commit-range r] [--json]
agentctl status [--json]             show the current session
```

`check` modes: `manual` (lifecycle hook), `pre-commit`, `commit-msg`, `pre-push`, `ci`.
Exit codes: `0` ok, `1` violations, `2` usage error, `3` no active session.

## Core loop

1. Human or supervisor agent keeps `PROJECT_PLAN.md` and task docs directionally correct.
2. Human starts a worker with `按 .agent 规范开始工作。` or an equivalent task request.
3. Worker reads `.agent/WORKFLOW_ENTRY.md` and runs `agentctl work --agent <name>` before editing. This resumes an
   active task or auto-claims the next assigned `ready`/`todo` task, then runs the
   `work-start` checkpoint loop.
4. If no task exists, the worker creates and starts one from the current request
   with `agentctl work --agent <name> --auto-create --title "..." --scope "..."`.
5. Worker records progress with `agentctl note`.
6. Workflow checkpoints run bounded loops continuously without a daemon:
   `work-start` runs plan triage, `pre-finish`/`post-finish` run document hygiene,
   and `experiment-check` can be invoked for experiment monitoring.
7. Worker creates task packets with `agentctl handoff create` when outputs feed downstream tasks.
8. On resume/compaction the session-start hook re-injects the focus; run
   `agentctl focus` to re-anchor a long task at any time.
9. Worker runs `agentctl finish` (task -> `review`, lock released).
10. Reviewer approval is optional for human oversight; when used, `agentctl gate approve`
   moves the task to `done` and checks the plan box.
11. Git hooks verify session, doc updates, commit format, task IDs, and review/done
   state before commit/push.

## Loop Contracts

Loops are small one-shot cycles stored under `.agent/loops/`. Every loop must
answer six questions: Trigger, Execute, Check, Feedback, Memory, and Next.
Checkpoint policy lives in `.agent/loops/checkpoints.json`; it decides which
loops run at `work-start`, `pre-finish`, `post-finish`, and `experiment-check`.

```bash
agentctl loop list
agentctl loop show daily-plan-triage
agentctl loop run daily-plan-triage --once
agentctl loop auto --checkpoint experiment-check --once
```

`agentctl work` and `agentctl finish` call the relevant checkpoint loops
automatically. Each run writes a durable report under `.agent/loops/runs/` and
updates `.agent/loops/state.json`. See `docs/loop-engineering.md`.

## Multi-agent split (example)

For data collection split across agents, create disjoint write scopes
(Fan-out/Fan-in):

```bash
agentctl task create --id T-101 --title "collect 1-20"  --owner agent1 --scope data/raw/001-020
agentctl task create --id T-102 --title "collect 21-40" --owner agent2 --scope data/raw/021-040
agentctl task create --id T-103 --title "collect 41-60" --owner agent3 --scope data/raw/041-060
agentctl task create --id T-199 --title "merge + validate" --owner supervisor --scope data/manifest
```

`agentctl work` refuses to start a task whose write scope overlaps another
in-progress task owned by a different agent, so agents do not clobber each other.

## Intended interaction model

After installation, humans should not need to drive workflow commands. A normal
agent prompt is:

```text
按 .agent 规范开始工作。
```

Humans mostly inspect and edit durable documents:

```bash
# open .agent/PROJECT_PLAN.md and .agent/tasks/*.md
# edit them if direction, priorities, scope, or acceptance criteria changed
```

Running `agentctl board`, `agentctl task show`, or `agentctl gate approve` remains
available, but it is not a required human step in the normal loop. Agents should
notice human document edits, re-read them, refresh their receipt, and keep going.

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
