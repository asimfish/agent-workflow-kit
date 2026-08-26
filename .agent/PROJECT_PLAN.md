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
- [x] TR334F227892EF8BEA-001 - independently review TA08B0CC413F151F5-018 interlock self-healing (owner: independent-reviewer-018)
- [x] TA08B0CC413F151F5-018 - resource and worktree lease deadlock self-healing and diagnostics (owner: cursor)
- [x] T126754FDB1001EB1-005 - re-review TA08B0CC413F151F5-017 archive-state fix (owner: supervisor)
- [x] T126754FDB1001EB1-004 - review TA08B0CC413F151F5-017 idempotent requests (owner: supervisor)
- [x] TA08B0CC413F151F5-017 - idempotent submission boundary with request ids for auto-create and run start (owner: cursor)
- [ ] T126754FDB1001EB1-003 - archive aged board history (owner: supervisor)
- [x] T126754FDB1001EB1-002 - review TA08B0CC413F151F5-016 rename references (owner: supervisor)
- [x] TA08B0CC413F151F5-016 - point self-references at the renamed agent-workflow-kit repo (owner: cursor)
- [x] T126754FDB1001EB1-001 - review TA08B0CC413F151F5-015 archive feature (owner: supervisor)
- [x] TA08B0CC413F151F5-015 - archive aged done tasks out of the live board (owner: cursor)
- [x] T13F0C74DBED8E114-001 - independent review of supervisor log diagnostics (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-014 - surface supervisor logs in test failures (owner: cursor)
- [ ] TA08B0CC413F151F5-013 - repair invalid utf-8 bytes in changelog (owner: cursor)
- [x] TB764E661F72A7FD7-001 - independent review of plain prose rewrite (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-012 - rewrite readmes in plain human prose (owner: cursor)
- [x] TE228821572B5963B-001 - independent review of oss packaging (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-011 - open source packaging: license, contributing, architecture diagram, bilingual readme (owner: cursor)
- [x] TE475DD513ACDF9CA-001 - re-review of readme restructure fixes (owner: independent-reviewer-2)
- [x] T76B9194109FD03F0-001 - independent review of readme restructure (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-010 - restructure readme into a concise entry page (owner: cursor)
- [x] T5CCA8C280CC6FDD5-001 - independent review of missing-holder grace window (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-009 - grace window for missing-holder resource release (owner: cursor)
- [x] T4C7B83C03F3DE7D5-001 - independent review of orphaned resource self-heal (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-008 - release resources orphaned by finished runs (owner: cursor)
- [x] TBFEA0B292CCCB26E-001 - independent review of run state retention pruning (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-007 - retention pruning for terminal run leases and artifacts (owner: cursor)
- [ ] T4116DD2E51AFDABA-001 - independent review of portable windows kill signal (owner: independent-reviewer-2)
- [x] TCA99BA4AB445E207-001 - independent review of portable windows kill signal (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-006 - portable kill signal for windows run termination (owner: cursor)
- [x] TA4590F0D1CAC6C44-001 - independent review of windows run stop ci coverage (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-005 - verify windows run stop tree at runtime in ci (owner: cursor)
- [x] TF96DBC8BC409FA2D-001 - independent review of supervisor persistence hardening (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-004 - persist supervised run terminal state through lock contention (owner: cursor)
- [x] T258C28F1DAE958F4-001 - independent review of decided-review closure (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-003 - close decided review tasks on finish and reconcile backlog (owner: cursor)
- [x] T62E2091C45F77EE5-001 - independent review of gpu watchdog test deflake (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-002 - widen gpu watchdog phase-exempt test timing margins (owner: cursor)
- [x] T8BA964DFB191095A-001 - independent review of upgrade fixture deflake (owner: independent-reviewer-2)
- [x] TA08B0CC413F151F5-001 - make upgrade barrier legacy fixture hermetic (owner: cursor)
- [x] T3D6917C0AC71CAAE-001 - isolate stale session admission and supervise idle GPU runs (owner: codex)
- [x] TB0621800D60E51C4-002 - independent review of unified multi-session architecture (owner: independent-reviewer)
- [x] TD851EF749F675DC5-001 - review pre-push published-commit filter (owner: claude-sonnet-reviewer)
- [ ] TA80D9103F5316278-001 - independently review residual repo-wide Git guard closure (owner: claude-sonnet-reviewer)
- [x] TA74E81A93FF7EB9D-001 - review concurrent main integration and collision migration (owner: claude-sonnet-merge-reviewer)
- [ ] T622DE1DE69A1D0F8-001 - independently review T-098 concurrent isolation recovery (owner: claude-sonnet-reviewer)
- [x] T3B43382C2174B290-001 - review final main advance integration (owner: codex-gpt55-final-reviewer)
- [x] T17F063E6115138DE-008 - integrate final main advances for concurrent release (owner: codex)
- [ ] T17F063E6115138DE-007 - release approved concurrent main integration (owner: codex)
- [x] T17F063E6115138DE-006 - integrate concurrent main advances without history loss (owner: codex)
- [ ] T17F063E6115138DE-005 - release pre-push published-commit fix (owner: codex)
- [x] T17F063E6115138DE-004 - exclude already-published commits from pre-push validation (owner: codex)
- [x] T17F063E6115138DE-003 - ignore already-published commits in pre-push scan (owner: codex)
- [ ] T17F063E6115138DE-002 - package and publish approved concurrent isolation release (owner: codex)
- [x] T17F063E6115138DE-001 - close residual repo-wide Git guard gaps (owner: codex)
- [ ] T-087 - close residual shared-checkout isolation boundaries (owner: codex)
- [ ] T-086 - integrate independent T-081 review evidence (owner: codex-integrator)
- [ ] T-085 - review T-081 resubmission and T-083/T-084 evidence (owner: independent-reviewer)
- [x] T-084 - independently review T-082 review record and T-083 isolation fixes (PR #17) (owner: independent-reviewer)
- [x] T-083 - close independent review isolation gaps (owner: codex)
- [x] TB0621800D60E51C4-001 - implement unified multi-session execution architecture (owner: codex)
- [x] T-105 - independently review T-104 change-aware debounce (owner: supervisor)
- [x] T-104 - make checkpoint debounce change-aware per its documented contract (owner: cursor)
- [x] T-103 - independently review T-102 hook override warnings (owner: supervisor)
- [x] T-102 - warn instead of silently overriding existing git hooks on init (owner: cursor)
- [x] T-101 - independently review T-100 eval target isolation (owner: supervisor)
- [x] T-100 - refuse eval run against a checkout with a live peer session (owner: cursor)
- [x] T-099 - independently review T-098 loop command scope guard (owner: supervisor)
- [x] T-098 - enforce scope on loop check commands (owner: cursor)
- [x] T-097 - independently review T-096 worktree escape hatch (owner: supervisor)
- [x] T-096 - make worktree escape hatch guidance actionable end to end (owner: cursor)
- [x] T-095 - independently review T-094 read-only hook latency (owner: supervisor)
- [x] T-094 - halve read-only hook latency by removing redundant status spawn (owner: cursor)
- [x] T-093 - independently review T-092 solo scan skip (owner: supervisor)
- [x] T-092 - skip contamination scan when no peers to avoid over-block and cost (owner: cursor)
- [x] T-091 - independently review T-090 symlink guard and efficiency (owner: supervisor)
- [x] T-090 - block symlink and hardlink creation aliasing peer scopes (owner: cursor)
- [x] T-089 - independently review T-088 scope containment (owner: supervisor)
- [x] T-088 - block ancestor and glob paths that exceed task scope (owner: cursor)
- [x] T-082 - independently review T-081 shell parse and shared-ref fixes (owner: supervisor)
- [x] T-081 - close newline, redirect, mv-source, and shared-ref gaps (owner: cursor)
- [x] T-080 - independently review T-079 argument-verified allowlist (owner: supervisor)
- [x] T-079 - verify read-only allowlist strictly and fix git read misclassifications (owner: cursor)
- [x] T-078 - independently review T-077 git and shell metacharacter hardening (owner: supervisor)
- [x] T-077 - default-deny git subcommands and shell metacharacters (owner: cursor)
- [x] T-076 - independently review T-075 opaque write isolation (owner: supervisor)
- [x] T-075 - exclusive opaque writes and default-deny unknown executables (owner: cursor)
- [x] T-074 - independently review T-073 write escape closure (owner: supervisor)
- [x] T-073 - close interpreter write escapes and explicit auto-create claim (owner: cursor)
- [x] T-072 - independently review T-071 loop test deflake (owner: supervisor)
- [x] T-071 - deflake loop inflight reconciliation regression (owner: cursor)
- [ ] T-070 - package document ownership release (owner: cursor)
- [x] T-069 - independently review T-065 T-067 T-068 (owner: supervisor)
- [x] T-068 - reject start on unknown task ids (owner: cursor)
- [x] T-067 - independently review T-066 ownership inventory extension (owner: supervisor)
- [x] T-066 - extend controller-owned files to registry, bus, handoffs, and eval runs (owner: cursor)
- [x] T-065 - independently review T-064 document ownership and receipts (owner: supervisor)
- [x] T-064 - enforce document ownership and scope-aware receipts (owner: cursor)
- [ ] T-063 - package approved multi-session migration release (owner: codex)
- [x] T-062 - second independent gate re-review for T-057 T-059 and T-061 (owner: independent-reviewer-2)
- [x] T-061 - close T-060 identity policy review findings (owner: codex)
- [ ] T-060 - independent gate review for T-057 and T-059 (owner: independent-reviewer)
- [x] T-059 - close T-058 terminal mutation identity gap (owner: codex)
- [ ] T-058 - independently review T-057 legacy session migration (owner: supervisor)
- [x] T-057 - migrate legacy workflow sessions (owner: codex)
- [x] T-056 - independently verify controller fork fail closed (owner: supervisor)
- [x] T-055 - prevent fork fallback to parent controller session (owner: codex)
- [ ] T-054 - independently re-review fork isolation fixes (owner: supervisor)
- [x] T-053 - close fork isolation review findings (owner: codex)
- [ ] T-052 - independently review forked-session isolation (owner: supervisor)
- [x] T-051 - isolate forked conversation sessions (owner: codex)
- [x] T-050 - independent supervisor reconcile T-049 PR #7 evidence (owner: supervisor)
- [x] T-049 - reconcile merged multi-session release (owner: codex)
- [x] T-048 - review T-047 binary signing mock fix (owner: supervisor)
- [x] T-047 - fix Windows binary signing regression mock (owner: codex)
- [x] T-046 - T-046 independent review T-045 recorded runtime identities (owner: supervisor)
- [x] T-045 - accept recorded reviewer runtime identities (owner: codex)
- [x] T-044 - Review T-043 signing-key binary write (owner: supervisor)
- [x] T-043 - fix Windows guidance receipt integrity CI (owner: codex)
- [x] T-042 - package approved multi-session release (owner: codex)
- [x] T-041 - independent review T-040 multi-session follow-up fixes (owner: supervisor)
- [x] T-040 - close T-038 multi-session review follow-ups (owner: codex)
- [x] T-039 - independent review T-038 multi-session coordination (owner: supervisor)
- [x] T-038 - coordinate concurrent sessions in one project (owner: codex)
- [ ] T-037 - remove GitHub reconciliation pagination caps (owner: supervisor)
- [ ] T-036 - bind GitHub reconciliation authority and compatibility (owner: supervisor)
- [ ] T-035 - paginate GitHub reconciliation file evidence (owner: supervisor)
- [ ] T-034 - harden GitHub reconciliation trust boundary (owner: supervisor)
- [ ] T-033 - reconcile merged GitHub reviews (owner: supervisor)

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
