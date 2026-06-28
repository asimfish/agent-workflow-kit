# AGENT-002 - dogfood workflow kit in super_project

Status: done
Owner: codex
Agent: codex
Created: 2026-06-29 03:00:32
Updated: 2026-06-29 03:00:32

## Task Contract

- Goal: Dogfood Agent Workflow Kit inside the real `super_project` repository, not a temp fixture.
- Non-Goals: Do not change product scope beyond workflow runtime, hook, documentation, and project-local dogfood files.
- Dependencies: Existing runtime implementation, GitHub remote `asimfish/super_project`, authenticated `gh`.
- Expected Deliverables: Installed project runtime, verified Git hooks, verified handoff bus, fixed completion/commit lifecycle if needed, pushed commit.
- Definition of Done: Real repo can run `start -> progress -> handoff -> complete -> git commit -> push` without bypassing hooks.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `.agent/, tools/, templates/, docs/, README.md, .githooks/, .gitignore`
- Files likely to touch: `tools/agentctl.py`, `templates/project/tools/agent_workflow_hook.py`, `.agent/**`, `.githooks/**`, `.gitignore`, docs.
- Files explicitly out of scope: unrelated application code, secrets, personal global config.

## Stage Plan

- [x] Stage 1: Install the kit into `super_project` itself.
- [x] Stage 2: Create and start a real project task.
- [x] Stage 3: Verify handoff bus and lifecycle hook behavior.
- [ ] Stage 4: Complete, commit, and push through real Git hooks.

## Stage Log
- 2026-06-29 03:05:59 Fixed gate approval to synchronize task document Status with board and index.
- 2026-06-29 03:05:17 Fixed TASKS.md status/index synchronization discovered during dogfood.
- 2026-06-29 03:04:17 Synchronized board write scope with task doc to include .gitignore.
- 2026-06-29 03:03:43 Dogfood exposed existing-.gitignore edge; fixed init to append .agent/state and .agent/tmp ignores.
- 2026-06-29 03:02:09 Verified handoff bus in real repository and fixed gate/session hash refresh.
- 2026-06-29 03:01:11 Installed workflow runtime into super_project and fixed complete-session commit lifecycle.


## Verification

- Commands to run:
  - `python3 -m py_compile tools/agentctl.py templates/project/tools/agent_workflow_hook.py`
  - `python3 tools/agentctl.py check`
  - `python3 tools/agentctl.py handoff create/list/show/mark`
  - `git commit` through `.githooks/`
  - `git push` through pre-push gate
- Expected result: all checks pass without `--no-verify` or hook bypass.

## Completion Record
- Summary: Dogfooded workflow kit in super_project; installed runtime, verified handoff bus, fixed complete/session, gate/session, existing-.gitignore, TASKS.md index sync, and task-doc gate status sync lifecycle edges.
- Tests: py_compile; agentctl check; handoff create/list/show/mark; git check-ignore for .agent/state; TASKS.md sync; gate status sync
- Completed-at: 2026-06-29 03:06:00
