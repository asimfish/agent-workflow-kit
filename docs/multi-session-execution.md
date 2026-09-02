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

## Identity Binding, Forks, And Clones

Some nested CLIs expose their conversation ID only in hook payloads and do not
export it to later shell subprocesses. `SessionStart` normally exports the
provider identity; if that export is absent, the first `agentctl work` hook
atomically binds the hashed payload identity to the hashed host runtime in
checkout-local Git-common-dir state. Only that provider conversation may reuse
the runtime-owned task; a second payload on the same pending or active runtime
and checkout fails closed. Independent worktrees use distinct binding keys, raw
provider IDs and checkout paths are never written to the binding record, and
exactly one active session may be pinned by its anonymous workflow key —
multiple matches fail closed instead of selecting one.

Forked or cloned conversations are isolated as separate runtime instances. A
persisted workflow ID is bound to the host runtime that created it, so a child
that inherits the parent's environment cannot use that stale ID to resume the
parent's task. Native `parent`/`fork`/`branch`/`clone` hook metadata supplies an
additional instance key when needed; `.agent/state/SESSIONS.md` shows the
hashed fork lineage without storing the raw parent ID.

A full Git clone has a different Git common directory, so it never reads or
writes the source clone's live session records. Committed plan/task files are
only a snapshot across independent clones; live cross-clone coordination is
intentionally not inferred. Default `work --auto-create` task IDs are clone-
and conversation-safe (`T<checkout/session shard>-NNN`), so branches created
from the same board snapshot do not reuse the same automatic ID.

## Opaque Writers And Escaped Writes

Commands whose written paths cannot be enumerated statically — interpreters
(`python3 -c`, scripts), test/build runners, archive/download/extract tools,
nested shells, and unknown executables — are exclusive per checkout: refused
beside another live session and pointed at a task worktree; alone, they require
an active task session. Only explicit, argument-verified read forms (`ls`,
`cat`, plain `rg`/`fd`, safe Git/GitHub reads, print-only `sed`) pass without a
claim; tool names alone are never sufficient — output/exec options such as Git
`--output`, `find`/`tree` output actions, `xxd` output operands, and GNU
target-directory forms are path-checked or fail closed. Native
write/edit/notebook/filesystem-MCP tools are checked by every concrete source
and destination path; a mutating tool with no bounded path contract is opaque.

Every mutating action reconciles the working tree afterwards: a tracked file
modified outside every live session's scope is treated as an escaped write and
blocks further mutations until it is reverted, claimed, or committed separately
by a human.

## Document Ownership And Read Receipts

Controller-generated files — `board.json`, `TASKS.md`, the agent registry,
`state/`, the shared progress log, `gates/`, loop runtime/reports, the guidance
bus and handoffs, eval runs/decisions/keys, and the install manifest — are
never editable through agent tool calls, even when the task scope covers
`.agent/`. Each denial names the `agentctl` command that owns the file. Policy
definitions (`loops/checkpoints.json`, `evals/suites.json`) remain scope-based.
The active task's own document (`.agent/tasks/<id>.md`) is always inside the
effective write scope; `PROJECT_PLAN.md`, rules, and other tasks' documents
follow the declared scope.

Read receipts are scope-aware so shared-index churn does not stall the room:
another task being created or finished rewrites only its own `TASKS.md` row,
its own plan checklist row, and the plan Change Log, none of which invalidates
other conversations' receipts. Plan-body edits, rule changes, this task's own
row, and this task's own document still require re-reading plus
`agentctl refresh` before further writes.

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

## Interlock Self-Healing

The failure this section exists for: nobody is using a GPU, yet no task can
claim it, because a dead holder still owns the lease and fail-closed holder
binding stops everyone else from releasing it. The kit breaks these interlocks
along three lines, ordered from automatic to manual:

1. **Orphan sweeps.** Resource leases whose holder is demonstrably gone are
   released automatically: a run holder whose lease is terminal (or missing
   after a 10-minute registration grace window), and a conversation holder
   whose session record says `released` (or whose record is gone entirely and
   the lease is over an hour old). The sweep runs on every `run start` and —
   new — whenever any resource acquisition hits a conflict, so a contested
   acquire heals the orphan and retries once before reporting failure.
   Releasing a session with `sessions release` also frees every resource that
   session still holds. Stale sessions (lost heartbeat) are never auto-released
   because the conversation may still be working.
2. **Actionable rejections.** When an acquire is still refused after the
   self-heal pass, the error names the holding lease, its holder identity and
   task, when it was acquired, and whether the holder is live. If the holder is
   demonstrably not live, the message includes the exact recovery command.
3. **Operator escape hatch.** `agentctl resource release <lease-id>
   --force-stale --reason <why>` releases a lease whose holder is stale,
   released, terminal, or missing. Live holders are always refused, and a
   missing holder is honored only after the same registration grace windows the
   orphan sweep uses (10 minutes for run holders, 1 hour for conversation
   holders), so the flag cannot steal a resource whose holder is genuinely in
   use or still registering; the release records who forced it and why. The
   liveness check and the release are not atomic — a stale session could
   heartbeat back in the milliseconds between them — which is accepted because
   the flag is an explicit operator action against a holder already silent for
   at least 30 minutes, and the audit trail names who forced it. `agentctl
   doctor` reports every lease stuck without a live holder — including worktree
   leases whose task is already done and `release_failed` external locks — with
   the command that resolves each one.
4. **Locks held by another checkout on the same host.** A local resource lock
   is machine-wide (one directory per resource under
   `~/.agent-workflow/resource-locks/`, or `AGENT_WORKFLOW_RESOURCE_LOCK_DIR`),
   while lease registries and session records are per checkout. A project
   whose conversation died holding `gpu:0` therefore blocks every other
   project on the machine, and nothing in the newcomer's own registry says so.
   The lock's owner record names the holder's checkout, and that checkout's
   registry is the evidence: a holder its own registry proves dead
   (`released`, finished run, or missing past the registration grace) is
   released by the next `resource acquire` in any checkout; a stale session,
   a checkout that no longer exists, a legacy lock without a recorded
   checkout, or a checkout whose runtime state cannot be read from here
   (another user's project on a shared host -- "cannot read" is never
   treated as "nothing there") is refused with the holder's state and the
   exact command,
   `agentctl resource release --lock <resource> --force-stale --reason <why>`,
   which addresses the lock by resource name because the lease id lives in
   the other registry. Live holders are refused and cannot be forced. The
   forced release is written into the releasing checkout's registry as an
   audit row (`release_mode: force-stale-foreign`), and `agentctl doctor` in
   any checkout lists machine-wide locks whose holder is not live, so the
   interlock is visible from the project that is actually blocked.

### Locks held by another project on the same host

A local resource lock (`gpu:0`) is machine-wide: one directory per resource
under `~/.agent-workflow/resource-locks/` (or `AGENT_WORKFLOW_RESOURCE_LOCK_DIR`),
shared by every checkout on the host. Lease registries and session records,
however, are per checkout. So when project A's conversation dies holding
`gpu:0`, project B's registry looks clean while B can never claim the card.

The lock's `owner.json` therefore records the holder's checkout, and the same
evidence rules apply across checkouts by reading that checkout's registry:

- A holder that its own registry proves dead (lease released, run finished,
  or lease missing past the registration grace) is released by B's next
  `res
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
