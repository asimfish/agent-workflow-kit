# Changelog

Notable changes to the Agent Workflow Kit. Full per-change history lives in
the task documents under `.agent/tasks/` and their gate records under
`.agent/gates/`; every entry below maps to merged, independently reviewed
pull requests.

## 0.5.x — 2026-08

- Unified multi-session execution architecture: canonical per-task records,
  conversation/run/resource/worktree/loop leases, fork- and clone-history
  isolation, task-type worktree policy, and a protocol upgrade barrier
  (#30).
- Stale-session scoping: identifiable stale peers downgrade to migration
  warnings while same-task, overlapping-scope, and exclusive admission stay
  fail-closed (#30).
- GPU lease supervision: opt-in watchdog that reclaims idle GPU-holding runs
  only on consecutive low-utilization, allocated-memory, absent-progress,
  and expired-grace evidence, with phase exemptions, fail-safe probes,
  report-only remote GPUs, and process-group cleanup (#30); validated live
  on shared RTX 5090s.
- Review-gate recursion closed: review-type tasks that issued a recorded
  gate decision close on finish, and `reconcile close-decided-reviews`
  sweeps historical backlogs under the same evidence rule (#33).
- Supervisor durability: claim and terminal-state writes retry through
  registry lock stalls, heartbeats tolerate missed beats, supervisor stderr
  persists per lease, and `run start` confirms the claim with one automatic
  respawn before failing closed (#34).
- Windows correctness: the run-stop/taskkill path is exercised at runtime in
  CI, which immediately caught and fixed a crash — `signal.SIGKILL` does not
  exist on Windows (#35).
- Retention and self-healing: terminal run leases and per-run artifacts age
  out on a configurable window, and resources orphaned by finished runs
  release automatically with a grace window against startup races (#36,
  #37).
- Test reliability: hermetic upgrade-barrier fixtures and load-tolerant GPU
  watchdog regressions (#31, #32).
- Documentation: concise entry-page README with an architecture diagram,
  bilingual README, install/upgrade guide, and expanded multi-session
  reference (#38+).
