# Historical conversation replay: stale admission and GPU supervision

## Evidence read

The four supplied Anygent conversations were inspected through their read-only
status and recent-message APIs. Message totals below exclude platform messages,
as defined by that API. Two source conversations were still running, so no
commands were sent to their hosts or experiments.

| Conversation | Status at read | Recent range read | Last item recovered |
| --- | --- | --- | --- |
| `tgz1pg715uemzft8qw1vtj3k7` | running; status count 177689 | 50 messages, IDs 8127701-8140854; visible total 169017 | monitoring an active handover collection; last meaningful update reported 35/100 reusable samples and about 75% GPU utilization |
| `hzepgrvoul67exsedqboonra5` | done; status count 115925 | 50 messages, IDs 8012082-8099295; visible total 115925 | v22/v453 both failed before Isaac because `psilab/isaaclab.sh` was absent; new work was blocked by stale T035/T053/T063 claims |
| `ieej2jw9pine68gus2ywqlrv7` | done; status count 276731 | 50 messages, IDs 8135606-8137057; visible total 276731 | conversation ended while v493/v494 and two audit watchers remained active; GPU averages were 83.4% and 57.1% |
| `zcoai5fbfdo3psmx4tvygo736` | running; status count 97227 | 50 messages, IDs 8139493-8141683; visible total 97165 | an unmanaged RMBench job entered GPU5 after seed195; the agent was building a persistent exact-PGID pause/resume contamination guard |

## Replay verdicts

### `hzepgrvoul67exsedqboonra5`: stale dispatch solved

The old failure was global: identifiable stale peers made `migrate` fail before
the replacement conversation could select unrelated work. The new migration
report leaves every stale record untouched but returns `continue` with warnings.
Normal `work/start` admission still rejects the same task, overlapping scope, or
exclusive work. Therefore the stale-session dispatch failure is solved without
creating an unsafe auto-release path.

The missing `psilab/isaaclab.sh` is an independent runtime packaging defect. The
new run supervisor would record its immediate nonzero exit and release resources,
but it cannot restore a missing application file. That part is intentionally not
classified as solved by this change.

### `tgz1pg715uemzft8qw1vtj3k7`: conditionally covered

This workload was making real progress and using the GPU. A registered run would
remain `active` because log/output progress and utilization reset idle evidence;
it would not be killed merely during an encoder, audit, or other low-utilization
phase if the child reports a phase exemption. Its private supervisor also remains
alive after the chat exits, eliminating dependence on manual conversation polling
for process ownership and GPU state.

The observed services were launched through systemd rather than this controller.
They are not retroactively owned, and dataset-specific success auditing and queue
finalization remain application responsibilities. The history is solved only
after future launches are routed through host-local `agentctl run start`.

### `ieej2jw9pine68gus2ywqlrv7`: persistence covered after migration

The ended conversation with still-running collectors is safe when each collector
is a supervised run: its lease, output ownership, heartbeat, progress and resource
state survive the conversation. Active utilization/progress keeps the watchdog
from false reclamation, and stop or idle reclamation targets the owned process
group so descendants cannot retain GPU memory after the parent exits.

Existing systemd services and watchers are still unmanaged. `run adopt` can make
their PID and resource visible but cannot safely recreate a process group, so
automatic termination requires relaunch through `run start`. Verdict: the new
architecture covers the failure mode, but the historical jobs need migration.

### `zcoai5fbfdo3psmx4tvygo736`: idle holder solved; contamination remains partial

For a registered, exclusively leased GPU run that holds memory, reports low GPU
utilization for consecutive samples, and makes no log/output/explicit progress,
the new state machine reaches `suspected_idle`, `grace`, `reclaimable`, then
`reclaiming`. It terminates only its verified host-local process group and releases
the resource after the run reaches a terminal state. This directly covers the
reported “memory occupied, utilization zero, left indefinitely” condition.

The history also contains a different condition: an unmanaged RMBench process
entered a GPU already used by seed195. Aggregate device utilization cannot safely
attribute that activity to one owner, and this controller does not pause/resume
foreign jobs or replace a cluster scheduler. System-managed peers are fenced by
the resource lease, but later unmanaged contamination remains fail-safe and needs
the workload-specific exact-lineage guard described in that conversation. Fixed
GPU placement and queue optimization are likewise outside this change.

## Executable replay map

| Historical invariant | Regression evidence |
| --- | --- |
| Known stale peers do not deny unrelated dispatch and are never auto-released | `test_identifiable_stale_peer_is_advisory_and_never_auto_released` |
| A real stale conflicting claim still blocks until explicit release | `test_stale_claim_remains_blocking_until_explicit_release` |
| One idle sample is insufficient; progress resets suspicion | `test_gpu_watchdog_requires_consecutive_idle_samples_and_no_progress` |
| Confirmed idle reclaims the process tree and lease; real progress survives | `test_supervised_gpu_watchdog_reclaims_idle_but_preserves_progress` |
| Explicit progress is owner-bound and supports bounded phase exemptions | `test_gpu_watchdog_progress_is_owner_bound_and_phase_exempt` |
| Remote auto-kill and invalid targets are refused; probe errors fail safe | `test_gpu_watchdog_fails_safe_for_remote_termination_and_probe_errors` |
| Long sample intervals retain heartbeat/stop responsiveness; CPU runs remain compatible | `test_gpu_watchdog_keeps_heartbeats_between_samples_and_rejects_bad_policy` |

## Overall decision

The new system resolves the two controller defects: global denial from known stale
sessions, and indefinitely idle GPU allocations for host-local runs launched and
owned by the controller. It deliberately does not claim control over historical
raw SSH/systemd processes, unmanaged cross-project GPU contamination, missing
runtime files, semantic experiment gates, or cluster placement. Those residuals
are migration or scheduler/application concerns rather than hidden success claims.
