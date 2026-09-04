# Project Plan

This file is the long-term source of truth for coordinated agent work.

## Format Rules

- Keep this file skimmable. Durable direction belongs here; detailed evidence belongs in task docs or logs.
- Preserve these top-level headings unless a project owner explicitly changes the schema.
- Use the shared status vocabulary: `todo`, `ready`, `in_progress`, `review`, `approved`, `done`, `blocked`, `failed`.
- Keep task IDs stable. Do not rename a task ID after work starts; create a replacement task and record the reason in Change Log.
- When a human edits this file, agents must re-read it and run `python3 tools/agentctl.py refresh` before continuing.

## Long-Term Goal

Provide an installable, project-level workflow kit that lets multiple AI agents
work from durable plans, task documents, loop feedback, and GitHub enforcement
instead of relying on chat memory or repeated human prompts.

## Current Strategy

- Primary agent-team pattern: Supervisor.
- Coordination method: durable `.agent/` packets plus bounded, explicit worker
  transports where the target runtime exposes a supported session interface.
- Write isolation: each task must define a clear write scope.
- Verification gate: task-specific tests plus `agentctl check`.
- Harness-change gate: supervisor-owned deterministic evals compare clean
  baseline and candidate worktrees before approval.

## Milestones

| Milestone | Target | Status | Exit Criteria |
|---|---|---|---|
| M1 | Define project workflow | done | Plan, rules, hooks, loop contracts, task docs, and dogfood evidence exist |
| M2 | Execute bounded supervisor-worker turns | in_progress | Fable can persist a plan, resume a named Codex session, and independently accept signed route/ack/task evidence from one real turn without weakening worker gates |
| M3 | Operational autonomy | in_progress | Resumable bounded cycles, worktree leases, scheduler adapters, budgets, stop conditions, and recovery tests are proven incrementally |
| M4 | Evidence-driven harness improvement | in_progress | Held-in/held-out evals gate changes before structured memory curation or bounded harness proposals are allowed |

## Task Board
- [x] T1801098AB51B5959-001 - review multi-checkout ledger sync TA08B0CC413F151F5-031 (owner: independent-reviewer-031)
- [ ] TA08B0CC413F151F5-031 - multi-checkout ledger sync: git merge driver for ledger files, pushable ledger-only claims, explicit takeover of foreign in_progress tasks, agentctl sync (owner: cursor)
- [x] T6A41B848ACDD2E58-001 - review hooks hardening TA08B0CC413F151F5-030 (owner: independent-reviewer-030)
- [x] TA08B0CC413F151F5-030 - post-review hardening: task-id references in commits, Windows CI test list, doctor report for lock records without a resource, docs count (owner: cursor)
- [x] T81204D354F17E8B3-001 - re-review README rewrite TA08B0CC413F151F5-029 (owner: independent-reviewer-029b)
- [x] T148B9308C5D771A2-001 - review README rewrite TA08B0CC413F151F5-029 (owner: independent-reviewer-029)
- [x] TA08B0CC413F151F5-029 - rewrite both READMEs for readability: mental model first, roles in the walkthrough, stuck-state quick reference, status section without the fix log (owner: cursor)
- [x] TA53E0FCAD2073DF3-001 - re-review of TA08B0CC413F151F5-028 after rejection (owner: independent-reviewer-028c)
- [x] T66D7BA7B09475618-001 - independent review of TA08B0CC413F151F5-028 (owner: independent-reviewer-028b)
- [x] TA08B0CC413F151F5-028 - cross-checkout orphan GPU locks: record holder checkout, verify liveness across checkouts, self-heal or name the recovery, doctor scans machine-wide locks (owner: cursor)
- [x] T9AB65133FAF94ED2-001 - independent review of TA08B0CC413F151F5-027 (owner: independent-reviewer-027)
- [x] TA08B0CC413F151F5-027 - harden GPU watchdog regression tests against slow runners; probe supervisor startup race (owner: cursor)
- [x] T36DC21235DACCCFF-001 - independent review of TA08B0CC413F151F5-026 (owner: independent-reviewer-026)
- [x] TA08B0CC413F151F5-026 - day-one acceptance fixes: doctor without identity, artifact gitignore, reviewer hints, README accuracy (owner: cursor)
- [x] T023R-001 - independent review of TA08B0CC413F151F5-023 (owner: independent-reviewer-023)
- [x] T025R-001 - independent review of the day-one walkthrough (owner: independent-reviewer-024)
- [x] TA08B0CC413F151F5-025 - add a day-one quick start walkthrough to both READMEs (owner: cursor)
- [x] TA08B0CC413F151F5-024 - refresh README for real-world use and document the worktree merge-back path (owner: cursor)
- [x] TA08B0CC413F151F5-023 - pave the worktree finish-to-gate path: merge-back tooling and doc (B-2) (owner: cursor)

## Agent Allocation

| Agent | Responsibility | Current Task | Write Scope |
|---|---|---|---|
| supervisor | planning, task split, final review | none | `.agent/`, docs |
| fable | advanced planning, bounded dispatch, evidence review | none | `.agent/`, docs |
| codex | implementation, verification, task completion | none | task-assigned scope |

## Dependencies

Format: `- T-002 depends on T-001 because <reason>.`

- Placeholder tasks T-000 and AGENT-003 are retired; active dependencies should be declared on new task rows.
- T-023 depends on T-012 for bounded checkpoint cycles and T-021 for the supervisor-to-Codex execution transport.

## Risks

- Multiple agents editing the same files can cause lost work. Avoid by assigning disjoint write scopes.
- A supervisor and worker sharing one checkout can contend for session state and
  Git index ownership. Use isolated worktrees for simultaneous turns until M3
  provides managed leases.
- A stale or incorrect external session ID can make dispatch fail. Preserve the
  guidance packet, record the failure receipt, and allow a bounded retry or
  file-only fallback.
- Plans can become stale. Avoid by updating this file whenever scope, status, or sequence changes.
- Task docs can become noisy. Keep durable facts and current status; move raw details to logs.

## Verification

- `python3 tools/agentctl.py check`
- Project-specific tests listed in each task document.

## Change Log

Format: `- YYYY-MM-DD HH:MM:SS - <agent-or-human> - <change and reason>.`

- Initial plan created by Agent Workflow Kit.
- 2026-07-02 17:58:00 - codex - added T-005 board row manually; task-create dedup regex matched AGENT-005 as T-005 (fixed in tools/agentctl.py).
- 2026-07-03 18:09:18 - codex - retired T-000 and AGENT-003 placeholders after T-001 through T-008 completed and T-008 was gate-approved.
- 2026-07-10 13:25:00 - codex - split maturity into durable workflow, bounded execution, and operational autonomy so runtime claims match tested capability.
- 2026-07-10 13:43:00 - codex - completed the bounded dispatch implementation and cleared stale worker allocation; M2 remains in progress pending real-session dogfood.
- 2026-07-10 22:18:00 - codex - started M3 with resumable cycle state and explicit stop/recovery semantics before adding scheduler or worktree automation.
- 2026-07-20 14:55:15 - codex - integrated concurrent task histories; main IDs remain canonical and feature collisions are recorded in `.agent/decisions/20260720-concurrent-task-id-collisions.md`.

- [x] TA08B0CC413F151F5-024 refresh README for real-world use and document the worktree merge-back path
