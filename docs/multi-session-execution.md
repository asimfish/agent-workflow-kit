# Multi-Session Execution

This document defines the ownership model when several conversations work in one
project or when one conversation is forked, copied, or resumed elsewhere.

## Canonical State

`.agent/board.json` is the canonical record for task identity, title, owner,
status, type, scope, and dependencies. `.agent/TASKS.md` and the task rows in
`.agent/PROJECT_PLAN.md` are generated views. Task documents remain the durable
contract, stage log, verification evidence, and completion record.

Use `agentctl reconcile check` to verify both directions. `render` regenerates
views but refuses to discard evidence that is absent from the board. `migrate`
imports only complete legacy records whose views agree.

## Execution Lease

`agentctl lease list` aggregates:

- conversation identity, task, scope, checkout, heartbeat, and fork lineage;
- managed worktree ownership;
- supervised or adopted background runs and their processes;
- local or SSH-addressed resource leases;
- the active bounded-loop runtime.

A copied chat history never copies authority. A child gets a distinct runtime
identity and may read the parent's durable task record, but it must claim a new
task or receive an explicit handoff. Same-task claims fail across every linked
worktree; overlapping-scope claims fail inside a shared checkout. Exclusive
maintenance blocks every other active or stale repository session.

A stale record remains authoritative while its checkout still exists. If its
persisted absolute checkout path no longer exists, it is reported as
`orphaned`: the audit record is retained, but it cannot block a new claim
because the old writer no longer has a project path. Permission or transient
filesystem probe failures remain `stale` and fail closed.

Migration compatibility and work admission are separate decisions. An
identifiable stale peer is an advisory warning from `agentctl migrate`; this
prevents one expired conversation from denying every new task in the checkout.
The stale claim is not released. `work/start` remains the enforcement point and
still rejects same-task, overlapping-scope, and exclusive-maintenance conflicts.
Pre-identity peer records remain a blocking migration inspection because their
owner and compatibility cannot be established safely.

## Isolation Policy

Task type selects the default:

| Type | Default |
|---|---|
| `code` | managed worktree |
| `experiment` | managed worktree with declared outputs |
| `docs` | shared checkout with disjoint scope |
| `review` | read-only |
| `maintenance` | exclusive worktree/repository access |
| `generic` | shared checkout |

Creating an isolated task through `work --auto-create` reserves its branch and
worktree before creating task state there. The planning checkout is left clean;
the agent continues from the printed path. A conversation that already owns a
different planning task releases that session before starting work in the new
task worktree.

For shared tasks, `work --auto-create` holds the repository coordination lock
across conflict preflight, durable task creation, and session start. A rejected
claim therefore leaves no board row, task document, or generated plan entry.

## Runs, Outputs, And Resources

Long-running commands use `agentctl run start`. The run owns its command hash,
cwd, supervisor and child PID birth markers, logs, declared outputs, heartbeat,
and resources. Outputs must be in the task scope or under the task-specific
artifact root, for example `.agent-artifacts/<task>/`.

`run adopt` is for an already-running inspected process. Process disappearance
becomes `exited_unknown`, never success; a worker must reconcile it with
`run finish`. Local resources use atomic filesystem locks. Remote resources use
an atomic directory on the named SSH host. Completion releases resources only
after the result is known. Only the holder conversation may stop or finish a
run. A supervised run claims a private single-use launch token atomically, so
replaying the hidden supervisor entrypoint cannot create a second child.
There is no public force-release override: conversation resources require the
matching conversation holder, while run cleanup presents the matching
`run:<lease-id>` holder.

An experiment cannot finish until one of its successful run leases has at least
one existing declared output.

### GPU supervision

Supervised runs can opt into GPU telemetry with canonical resource identifiers:

- `gpu:<index>` observes a GPU on the host running `agentctl`;
- `ssh://<host>/gpu:<index>` observes a remote GPU through structured SSH
  arguments and is report-only from the local host.

Example:

```bash
python3 tools/agentctl.py run start \
  --output .agent-artifacts/T-123/checkpoints/ \
  --resource gpu:0 \
  --gpu-watchdog \
  --gpu-idle-seconds 600 \
  --gpu-grace-seconds 300 \
  --gpu-idle-action terminate \
  -- python3 train.py
```

The private supervisor persists after the conversation exits. A fixed heartbeat
keeps ownership observable while an independent timer samples at the configured
interval. It combines utilization, allocated memory, log/output
metadata, and explicit progress. The state machine is `active ->
suspected_idle -> grace -> reclaimable -> reclaiming`. Two or more consecutive
low samples are mandatory. Progress, an explicit phase exemption, or a probe
error resets or suspends reclamation; one 0% sample can never terminate a run.

A child process inherits `AGENT_WORKFLOW_RUN_ID` and may report a legitimate
low-utilization phase without copying a supervisor token:

```bash
python3 tools/agentctl.py run progress \
  --phase compile --token kernel-cache-v2 --idle-exempt-seconds 900
```

The same conversation identity and task ownership are still required. The child
command runs in its own process group, so explicit automatic termination covers
its descendants instead of releasing the GPU when only the parent exits. Policy
may be supplied by CLI flags or the optional `gpu_watchdog` object in
`.agent/runtime-policy.json`; CLI values take precedence. The default action is
`report`. A globally enabled policy applies only to runs that declare canonical
GPU resources, so CPU-only runs remain compatible. `terminate` is rejected for SSH-observed resources because killing a
local SSH client does not prove the remote GPU process stopped. To reclaim a
remote GPU automatically, run agentctl on that host so it owns and supervises
the actual process. Raw SSH/systemd jobs that bypass `run start` remain outside
the automatic-watchdog boundary. `run adopt` records an existing PID and its
resources, but cannot retroactively create a safe private supervisor or process
group, so it does not enable automatic GPU termination.

Terminal run leases, released resource leases, and per-run artifacts
(stdout/stderr/supervisor logs and command payloads) age out automatically:
`run start` opportunistically prunes entries older than
`run_artifact_retention_days` from `.agent/runtime-policy.json` (default 14;
`0` disables pruning). Live and non-terminal leases, recent state, and
`release_failed` resource leases are never pruned — the last of these flags
manual attention. Independently of retention, the same pass releases
resource leases whose holding run already resolved: holder binding is
fail-closed, so without this a release that hit a registry lock stall at
run completion would strand the resource forever.

## Upgrade Barrier

The install manifest records schema, kit version, source commit, and protocol
epoch. A protocol-changing `init`:

1. enters `draining`;
2. replaces only the managed controller and hook bridge with barrier-aware
   entrypoints, including the no-blocker path that immediately enters
   `validating`;
3. allows existing sessions to note, finish, stop their own runs, and release
   their own leases;
4. migrates templates and state only after the writer set is empty;
5. records `steady` at the new epoch.

An old conversation must re-read its plan and task document, then run
`agentctl upgrade rebind`. Until then, mutating commands are blocked. Existing
supervised processes may finish during a drain because their command and output
ownership was established before the barrier.

The barrier governs the kit's managed project entrypoints. An arbitrary copied
old controller or an untrusted process that ignores project hooks is outside the
cooperative protocol and requires an OS sandbox or access-control boundary.

## Efficiency

Safety checks are proportional to the action. Read-only hooks use a 60-second
heartbeat debounce and launch a due heartbeat in the background, so repository
coordination work does not delay the read itself. The heartbeat still validates
conflicts under the shared lock and updates the canonical conversation record;
`sessions list` refreshes the generated human-readable view. Mutations run full
identity, scope, peer, claimed-file, and contamination checks. Full task-view
reconciliation runs at finish, CI, and upgrade boundaries. `agentctl capsule`
returns bounded current-task context instead of replaying project history.
