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
