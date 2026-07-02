# Project Plan

This file is the long-term source of truth for coordinated agent work.

## Format Rules

- Keep this file skimmable. Durable direction belongs here; detailed evidence belongs in task docs or logs.
- Preserve these top-level headings unless a project owner explicitly changes the schema.
- Use the shared status vocabulary: `todo`, `ready`, `in_progress`, `review`, `approved`, `done`, `blocked`, `failed`.
- Keep task IDs stable. Do not rename a task ID after work starts; create a replacement task and record the reason in Change Log.
- When a human edits this file, agents must re-read it and run `python3 tools/agentctl.py refresh` before continuing.

## Long-Term Goal

Define the durable project outcome in one or two paragraphs.

## Current Strategy

- Primary agent-team pattern: Supervisor.
- Coordination method: file-based handoff through `.agent/`.
- Write isolation: each task must define a clear write scope.
- Verification gate: task-specific tests plus `agentctl check`.

## Milestones

| Milestone | Target | Status | Exit Criteria |
|---|---|---|---|
| M1 | Define project workflow | in-progress | Plan, rules, hooks, and task docs exist |

## Task Board
- [x] T-007 - escalate repeatedly failing checkpoint follow-ups (owner: codex)
- [x] T-006 - generic executor for custom loop contracts (owner: codex)
- [x] T-005 - loop feedback drives next cycle via follow-up packets (owner: codex)
- [x] T-004 - simplify README structure (owner: codex)
- [x] T-003 - add continuous loop automation (owner: codex)
- [x] T-002 - add minimal loop runtime (owner: codex)
- [x] T-001 - simplify agent startup prompt (owner: codex)
- [x] AGENT-009 - support adoption baseline for existing repositories (owner: codex)
- [x] AGENT-008 - make agent bootstrap and github references explicit (owner: codex)
- [x] AGENT-007 - validate workflow against original requirements (owner: codex)
Format: `- [ ] T-001 - short task title (owner: agent-id)`.
Use `[x]` only when the task is `done`.

- [x] AGENT-006 - harden document and github templates (owner: codex)
- [x] AGENT-005 - make human steering optional (owner: codex)
- [x] AGENT-004 - simplify autonomous agent interaction (owner: codex)
- [ ] AGENT-003 - review dogfood workflow results (owner: reviewer)
- [x] AGENT-002 - dogfood workflow kit in super_project (owner: codex)

- [ ] T-000 - Replace this starter task with real work (owner: supervisor)

## Agent Allocation

| Agent | Responsibility | Current Task | Write Scope |
|---|---|---|---|
| supervisor | planning, task split, final review | T-000 | `.agent/`, docs |

## Dependencies

Format: `- T-002 depends on T-001 because <reason>.`

- T-000 has no dependencies.

## Risks

- Multiple agents editing the same files can cause lost work. Avoid by assigning disjoint write scopes.
- Plans can become stale. Avoid by updating this file whenever scope, status, or sequence changes.
- Task docs can become noisy. Keep durable facts and current status; move raw details to logs.

## Verification

- `python3 tools/agentctl.py check`
- Project-specific tests listed in each task document.

## Change Log

Format: `- YYYY-MM-DD HH:MM:SS - <agent-or-human> - <change and reason>.`

- Initial plan created by Agent Workflow Kit.
- 2026-07-02 17:58:00 - codex - added T-005 board row manually; task-create dedup regex matched AGENT-005 as T-005 (fixed in tools/agentctl.py).
