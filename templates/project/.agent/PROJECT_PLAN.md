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

Format: `- [ ] T-001 - short task title (owner: agent-id)`.
Use `[x]` only when the task is `done`.

- [ ] T-000 - Replace starter task with real work (owner: supervisor)

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
