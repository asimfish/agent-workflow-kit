# Changelog

Detailed history lives in the task documents under `.agent/tasks/` and the
review records under `.agent/gates/`. Entries here map to merged, reviewed
pull requests.

## 0.5.x — 2026-08

The multi-session release (#30). One controller now tracks conversations,
runs, resources, worktrees, and loops as leases with recorded owners.
Forked or copied conversations cannot inherit a parent's authority. A dead
session's claims warn instead of blocking unrelated work, while real
conflicts still refuse. Code and experiment tasks get isolated worktrees
by default.

GPU supervision (#30): a run can lease `gpu:N` and opt into a watchdog
that reclaims the card only after sustained zero-utilization with memory
held, no progress, and an expired grace period. Compilation phases can
declare exemptions. Probe failures never kill anything. Remote GPUs are
report-only. Validated live on shared RTX 5090s.

Reliability work found by dogfooding: supervisors now survive registry
lock stalls and pre-claim deaths (#34), `run stop` was completely broken
on Windows because `signal.SIGKILL` does not exist there — caught the
first time CI actually ran the path on windows-latest (#35), review-type
tasks no longer demand reviews of their own reviews (#33, which also
closed 33 stuck historical tasks), old run leases and logs age out (#36),
and resources orphaned by finished runs release themselves (#37).

Two test-suite fixes (#31, #32), a rewritten README with an architecture
diagram (#38), and open-source packaging: license, contributing guide,
citation file, issue templates, bilingual README (#39).

Later in the cycle: aged done tasks archive off the live board via
`reconcile archive` (#42), self-references follow the repository rename to
agent-workflow-kit (#43), creation commands accept a `--request-id` token
that makes `work --auto-create` and `run start` idempotent under retries,
and worktree bootstrap survives `git worktree add` hangs (#44). Resource
interlocks -- leases held by dead runs or vanished conversations -- now
self-heal on the next acquisition attempt, `resource release --force-stale`
breaks provably dead locks, and `doctor` reports interlocked leases with
the exact recovery command (#45, registration grace windows honored in
#46). A board hygiene sweep closed 22 legacy review-status tasks (#47).

Task ids are no longer derived from the board alone: creation collects
claims from task documents, archives, live sessions, and worktree leases,
and refuses to overwrite a task id that belongs to someone else (#48).
Found by a seven-scenario acceptance run against a fresh clone, which the
fixed revision then passed end to end -- including adversarial state
surgery against the interlock, idempotency, and review-gate guards.

The finish-to-gate path for worktree tasks is now tooled: `reconcile
merge-back` imports a task's board entry, task document, and gate record
from its feature branch into the planning checkout, re-renders the views,
and refuses foreign ids, worktree checkouts, and status regressions. The
same change fixed the acceptance-run rough edges: explicit `--auto-create`
requests refuse to silently resume unrelated work, worktree and gate
refusals name the step that resolves them, plan rows accept multi-hyphen
task ids, and pre-push resolves commit references against the archive.
