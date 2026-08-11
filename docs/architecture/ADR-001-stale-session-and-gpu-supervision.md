# ADR-001: Scope stale-session admission and supervise GPU leases

## Status

Accepted

## Context

An identifiable stale conversation in one checkout currently makes `migrate`
fail before `work` can evaluate the requested task and scope. Separately,
long-running GPU commands can outlive a conversation while GPU ownership,
utilization, and application progress are observed by unrelated ad-hoc tools.
This causes both availability failures and undetected idle allocations.

## Driving Factors

- Preserve fail-closed same-task, overlapping-scope, and resource ownership.
- Never release a stale claim or terminate a process from age or one 0% sample.
- Continue supervision after the launching conversation exits.
- Keep environment-specific GPU probing behind a replaceable interface.
- Persist one JSON supervision record beside the canonical run/resource leases.

## Candidates

### Option A: TTL release and fixed zero-utilization killer

- Pros: small implementation and fast resource recovery.
- Cons: can release disconnected but active writers and kill legitimate compile,
  planning, data-loading, evaluation, or encoding phases.

### Option B: Advisory migration plus lease-first progress-aware supervisor

- Pros: unrelated work remains available; admission guards still fence real
  conflicts; a private run supervisor owns the exact child and can combine
  consecutive telemetry with progress and grace periods.
- Cons: legacy raw SSH/systemd jobs must be migrated or adopted, and remote
  automatic termination needs a host-local supervisor.

### Option C: Hosted coordinator or external cluster scheduler

- Pros: strongest cross-host inventory, placement, and policy enforcement.
- Cons: adds a new availability boundary and is outside this installable,
  dependency-free project kit.

## Decision

Chosen: Option B.

`migrate` will report identifiable stale peers as warnings and leave their
records untouched. Actual `work/start` admission remains authoritative for
same-task, scope, checkout, and maintenance conflicts.

GPU supervision is opt-in per supervised run. Canonical GPU identifiers are
`gpu:<index>` for a host-local device and `ssh://<host>/gpu:<index>` for remote
observation. A probe registry isolates local and SSH `nvidia-smi` adapters.
Samples, policy, progress markers, and state are JSON fields on the run lease and
are copied to its resource leases. The state machine is:

`active -> suspected_idle -> grace -> reclaimable -> reclaiming`.

At least two consecutive low-utilization samples, minimum allocated memory, no
log/output or explicit progress, an idle timeout, and a grace timeout are
required. Probe errors and progress fail safe to a non-reclaimable state.
Automatic termination is opt-in and restricted to a local GPU run owned by the
same private supervisor; remote observation is report-only unless agentctl runs
on the remote host.

## Impact

- `migrate` becomes available in the presence of known stale peers without
  weakening task admission.
- `run start` can keep monitoring after the conversation exits and reclaim an
  explicitly opted-in idle local GPU process.
- Raw unmanaged SSH/systemd processes remain outside the ownership boundary and
  are reported as a migration requirement, not claimed as automatically safe.
