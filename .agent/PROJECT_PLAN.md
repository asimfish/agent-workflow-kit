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
- [ ] T-034 - harden GitHub reconciliation trust boundary (owner: supervisor)
- [ ] T-033 - reconcile merged GitHub reviews (owner: supervisor)
- [x] T-032 - prevent completion evidence injection (owner: codex)
- [x] T-031 - fix Windows dispatch identity and decoding (owner: codex)
- [x] T-030 - close T-029 independent review findings (owner: codex)
- [x] T-029 - harden installation review gates and native hooks (owner: codex)
- [x] T-028 - verify windows dispatch process-tree cleanup (owner: codex)
- [x] T-027 - close PR 3 merge blockers (owner: codex)
- [x] T-026 - add supervisor dispatch acceptance workflow (owner: codex)
- [x] T-025 - add harness evaluation baseline (owner: codex)
- [x] T-024 - add managed worktree leases (owner: codex)
- [x] T-023 - add resumable autonomous loop runtime (owner: codex)
- [x] T-022 - finalize dispatch release metadata (owner: codex)
- [x] T-021 - add codex session dispatch transport (owner: codex)
- [x] T-020 - validate github install link (owner: codex)
- [x] T-019 - post-merge validate guidance workflow (owner: codex)
- [x] T-018 - mark supervisor guidance PR ready (owner: codex)
- [x] T-017 - route supervisor guidance to specific codex sessions (owner: codex)
- [x] T-016 - prepare supervisor guidance branch for merge (owner: codex)
- [x] T-015 - add pre-push new branch range regression (owner: codex)
- [x] T-014 - fix pre-push range for first feature branch push (owner: codex)
- [x] T-013 - support supervisor guidance from advanced models to codex (owner: codex)
- [x] T-012 - add bounded loop cycle runner (owner: codex)
- [x] T-011 - add project doctor diagnostics (owner: codex)
- [x] T-010 - add repeatable loop regression test (owner: cursor)
- [x] T-009 - retire obsolete placeholder tasks (owner: codex)
- [x] T-008 - dogfood installed loop workflow end to end (owner: codex)
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
- [x] AGENT-003 - review dogfood workflow results (owner: reviewer)
- [x] AGENT-002 - dogfood workflow kit in super_project (owner: codex)

- [x] T-000 - Replace this starter task with real work (owner: supervisor)

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
