# ADR-002: Checkout-Local Write Scopes

## Status

Accepted for the checkout-local scope admission change, 2026-10-23.

## Context

The controller rejected overlapping repository-relative scopes even when two
different tasks had separate managed worktrees. Two `src/` scopes therefore
serialized unrelated code work despite distinct files and Git indexes.
This conflicted with the intended worktree-per-task concurrency model.

## Driving Factors

- Preserve same-task ownership, fork identities, and shared-checkout write guards.
- Admit independent work without new human commands or per-write global scans.
- Keep external outputs, scarce resources, and repository maintenance explicit.

## Candidates

### Option A: Global Relative-Scope Exclusion

Retain the existing blanket refusal. Simple and conservative, but forces workers
to split directory labels or wait even when their physical files are isolated.
It does not itself protect an undeclared external output or resolve Git merges.

### Option B: Checkout-Local Scope Exclusion

Use existing managed checkout identity to distinguish isolated repository files.
Preserve the current session checks for shared checkouts and repository-wide
same-task/exclusive claims. Normal integration handles branch-level conflicts.
This matches Git's isolation boundary without another registry or service.

## Decision

Choose Option B. Allocation no longer compares relative scopes across managed
worktrees; managed task start does not treat an imported peer board row as a
physical file collision. One task still has at most one unreleased worktree
lease, even when different conversations use different agent names.

Concurrent startup exposed a lock inversion: allocation held the worktree lease
lock while its task-creation child waited for coordination, while `start` held
coordination and waited for that lease lock. Bootstrap now releases the lease
lock after Git allocation, then takes coordination followed by the lease lock
for short, in-process task creation. It reloads the lease and verifies the
registered checkout before writing, so retirement in between cannot recreate a
deleted checkout. Slow Git allocation does not hold the coordination lock.

No public CLI, lease schema, or protocol epoch changes. Older controllers remain
conservatively restrictive; drain/rebind normally when an installer requires it.
Existing shared tasks are not silently relocated, and unknown board claims in
an unmanaged shared checkout retain their conservative scope backstop.

## Impact

- Allocation and managed task start change; hook, receipt, run-output and
  resource authorization policies do not.
- Shared checkout: same/overlapping physical scope remains refused.
- Separate managed checkouts: different tasks may both own `src/` and `tests/`.
- Structured paths resolve inside the owner's checkout. A peer worktree's
  absolute path is not authorized by an identical relative scope.
- External artifacts still use declared task-specific output roots; shared GPUs
  and other scarce resources still require declared resource leases.
- Scripts are cooperative, not OS-sandboxed. Undeclared external side effects
  require external permissions/isolation; this change does not make them safe.
- Same-file branch changes may conflict at merge. Integration must preserve
  both tasks' intent and rerun affected tests; no automatic overwrite is added.
- Session/run ownership still prevents retiring a checkout that is in use.

## Evidence

`tests/test_worktree_workflow.py` covers committed active board snapshots,
explicit leases, shared agent names, and duplicate task rejection.
`tests/test_simulated_collaboration.py` installs a synthetic project through
normal hooks, reproduces the original false rejection, then develops the same
relative path in two worktrees while checking task evidence and cross-path
denials. Existing shared-session, fork, resource and retirement regressions
remain required; no real collection project is used.
