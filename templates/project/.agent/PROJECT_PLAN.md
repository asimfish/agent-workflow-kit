# Project Plan

This file is the long-term source of truth for coordinated agent work.

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

- [ ] T-000 - Replace this starter task with real work (owner: supervisor)

## Agent Allocation

| Agent | Responsibility | Current Task | Write Scope |
|---|---|---|---|
| supervisor | planning, task split, final review | T-000 | `.agent/`, docs |

## Dependencies

- T-000 has no dependencies.

## Risks

- Multiple agents editing the same files can cause lost work. Avoid by assigning disjoint write scopes.
- Plans can become stale. Avoid by updating this file whenever scope, status, or sequence changes.
- Task docs can become noisy. Keep durable facts and current status; move raw details to logs.

## Verification

- `python3 tools/agentctl.py check`
- Project-specific tests listed in each task document.

## Change Log

- Initial plan created by Agent Workflow Kit.

