# AGENT-004 - simplify autonomous agent interaction

Status: done
Owner: codex
Agent: codex
Created: 2026-06-29 13:13:56
Updated: 2026-06-29 13:13:56

## Task Contract

- Goal: Reduce daily human/agent interaction to a small autonomous work loop after installation.
- Non-Goals: Do not remove lower-level commands needed for debugging, review, or manual supervision.
- Dependencies: Existing board/session/hook/git-gate runtime.
- Expected Deliverables: `work`, `note`, and `finish` convenience commands; updated AGENTS/templates/docs/hook prompts.
- Definition of Done: A new agent can enter a project and run one command to resume or claim work, use one command to note progress, and one command to finish.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `tools/, templates/, docs/, README.md, AGENTS.md, .agent/`
- Files likely to touch: `tools/agentctl.py`, `AGENTS.md`, `templates/project/AGENTS.md`, hook bridge messages, docs.
- Files explicitly out of scope: unrelated runtime behavior, GitHub remote settings, secrets.

## Stage Plan

- [x] Stage 1: Add low-friction CLI aliases for normal agents.
- [x] Stage 2: Update hooks and AGENTS instructions to use the new work loop.
- [x] Stage 3: Dogfood in `super_project` and push through hooks.

## Stage Log
- 2026-06-29 13:17:55 Dogfooded simplified work/note/finish loop in a fresh temp project and refreshed read receipt after task-doc edits.
- 2026-06-29 13:16:52 Added low-friction work/note/finish commands and updated agent-facing docs/hooks.


## Verification

- Commands to run:
  - `python3 -m py_compile tools/agentctl.py templates/project/tools/agent_workflow_hook.py tools/agent_workflow_hook.py`
  - `agentctl work --agent codex`
  - `agentctl note "..."`
  - `agentctl finish --summary "..." --tests "..."`
  - `git commit` and `git push` through hooks
- Expected result: a normal agent-facing workflow requires no manual task-id lookup when an assigned task exists.

## Completion Record
- Summary: Added autonomous agent work loop commands and updated hooks/docs so installed projects guide agents through work/note/finish instead of manual task driving.
- Tests: py_compile; temp-project work/note/finish smoke; agentctl check manual
- Completed-at: 2026-06-29 13:17:55
