# AGENT-005 - make human steering optional

Status: done
Owner: codex
Agent: codex
Created: 2026-06-29 13:37:03
Updated: 2026-06-29 13:37:03

## Task Contract

- Goal: Make human steering optional by letting agents create or claim their own task context from the current request.
- Non-Goals: Do not remove review gates, Git hooks, or the documented plan/task files.
- Dependencies: Existing `work`, `note`, `finish`, task docs, board, and hook bridge.
- Expected Deliverables: `work --auto-create` support, updated agent-facing instructions, and docs that describe humans as optional reviewers/editors rather than command operators.
- Definition of Done: A new agent can be told to use the installed kit and can start work even when no pre-created task is assigned, while hooks still enforce plan/task docs and GitHub commit rules.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `tools/, templates/, docs/, README.md, AGENTS.md, .agent/`
- Files likely to touch: `tools/agentctl.py`, `AGENTS.md`, templates, hook bridge messages, docs.
- Files explicitly out of scope: unrelated runtime architecture, remote GitHub settings, secrets.

## Stage Plan

- [x] Stage 1: Add autonomous task creation to the `work` entry command.
- [x] Stage 2: Update hooks, AGENTS, templates, and docs to make human steering optional.
- [x] Stage 3: Dogfood a fresh-project flow where no task exists before the agent starts.

## Stage Log
- 2026-06-29 13:43:56 Dogfooded fresh-project auto-create flow without any pre-created worker task.
- 2026-06-29 13:43:11 Implemented work --auto-create and updated docs/hooks so humans only steer through plan/task files.
- 2026-06-29 13:37:29 Defined AGENT-005 around auto task creation and optional human steering.


## Verification

- Commands to run:
- `python3 -m py_compile tools/agentctl.py templates/project/tools/agent_workflow_hook.py tools/agent_workflow_hook.py`
- Fresh project smoke with `work --auto-create --title ... --scope ...`
- `python3 tools/agentctl.py check --mode manual`
- Expected result:
- Agent can create/claim/start a task with one command, record progress, finish, and pass workflow checks.

## Completion Record
- Summary: Added autonomous task creation to the work entry command and reframed docs/hooks so humans steer by editing plan/task docs instead of running workflow commands.
- Tests: py_compile; fresh-project work --auto-create smoke; agentctl check manual
- Completed-at: 2026-06-29 13:43:56
