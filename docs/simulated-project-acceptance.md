# Simulated Project Acceptance

These journeys exercise freshly installed temporary Git projects, not ChemBench
or another user's checkout. They use the real installer, controller, Codex hook
bridge, Git hooks, supervised subprocesses, resource locks and loop runner.
Conversation identities are explicitly simulated in subprocess environments:
this is repeatable workflow evidence, not certification of live LLM clients.
No SSH connection, GPU, cloud service or downloaded dataset is needed.

## Journeys

`tests/test_simulated_collaboration.py` covers four end-to-end journeys:

- Four same-name document workers claim disjoint catalog, pricing, validation
  and replay tasks concurrently. Barriers coordinate three rounds of notes;
  every marker must occur exactly once in its own document and the shared log.
  There are no failure retries or automatic refreshes. Copied-history task
  theft, peer file/document writes and overlapping task creation are refused.
  Completing validation leaves other workers productive. A human plan-body
  change blocks progress until an explicit re-read/refresh.
- A copied-history child carries its parent's workflow ID into the real
  `SessionStart` hook, but has a different simulated host runtime. The hook
  clears the inherited ID and records lineage. The child cannot claim its
  parent's live task, but can start a disjoint task while the parent continues.
  After the parent explicitly releases its task, a successor receives a new
  claim and completes the existing partial output without losing prior notes.
  The old parent cannot note, finish, refresh, rebind, reclaim or write to the
  successor's files/document. Denials leave the task board and both documents
  unchanged. The child retains its own claim and continues after the successor
  finishes, without an unrelated receipt refresh.
- Two experiment workers receive separate managed worktrees and collect exact
  synthetic record ranges 1-20 and 21-40. Files do not appear in each other's
  checkout. A real supervised collector holds a synthetic resource slot until
  a release marker; its peer cannot steal the slot or stop it, but can still
  record its own progress. Success releases the slot for the second worker.
  Result content, no missing/duplicated records and completion test exits are
  asserted. Git rejects an unstructured commit, then accepts a normal task-linked
  commit and push to a temporary local bare remote without bypassing hooks.
- A two-cycle checkpoint is interrupted after cycle one has completed and
  before cycle two starts. A peer cannot resume or stop it, and failed attempts
  must leave its durable state/reports unchanged. The owner resumes only the
  remaining cycle. The completed runtime and task-tagged reports persist; the
  peer's task document remains untouched by the owner's recovery.

The focused regression in `tests/test_document_ownership.py` checks complete
multi-hyphen IDs such as `T-SIM-CATALOG-001`: a peer row does not invalidate
receipts, but the owner's entire index/plan row still does. Comparing only the
`T-SIM` prefix would either cause false blocking or silently miss a change.

## Repeat The Checks

Agents and maintainers run these from the kit checkout; the human does not need
to run them during normal project development:

```bash
python3 -m unittest tests.test_simulated_collaboration -v
python3 -m unittest tests.test_document_ownership -v
python3 -m unittest tests.test_task_loop_isolation tests.test_upgrade_barrier_workflow tests.test_claim_upgrade -v
python3 -m unittest discover -s tests -v
```

Existing Linux CI discovers the new journeys automatically. Windows CI runs
its existing selective suites; it does not certify these new POSIX-oriented
journeys. Temporary projects, worktrees and local bare remotes are cleaned up.
Subprocesses have bounded waits and cleanup paths. Synchronization uses barrier
and file/state events, not assumptions about a worker finishing after a sleep.

## Failure Evidence And Repair

At baseline `866b99c`, four-session notes with multi-hyphen IDs were falsely
blocked by `.agent/TASKS.md` changes. The focused regression reproduces this
without concurrency: start one task, start a disjoint peer, then note on the
first task without refresh. It exits 1 instead of 0 (6.7 seconds locally).
The previous row regex excluded suffix hyphens in the index and could parse
only a prefix in the plan. The repair recognizes the complete ID. The installer
distributes this corrected controller source directly; there is no separate
`templates/project/tools/agentctl.py` to maintain. No receipts are skipped,
ownership weakened, write permissions broadened or instructions auto-acknowledged.

The initial journey test also assumed an exit-2 hook denial and a top-level
`resume_safe`. Actual Codex denial is exit-0 JSON and loop status nests that
field under `runtime`. Those fixture expectations were corrected separately;
the subsequent collector and interruption journeys passed before the receipt
repair. Broken-barrier diagnostics now retain the original worker errors.

## Scope Of The Evidence

These tests validate concrete protocol behavior and project artifacts, not
the quality or intentions of an LLM. They do not establish cross-machine
realtime coordination, remote GPU telemetry, client-side hook trust, OS-level
isolation, unlimited scheduling, performance budgets or Fable transport.
Local fake resource slots exercise ownership, not GPU behavior.
The fork payload, provider environment and historical notes are controlled
fixtures. They prove the identity/claim protocol, not a model's comprehension
of copied chat history. The local handoff uses an explicit inspected release;
it does not simulate a crash or authorize automatic takeover of a live worker.

Unchanged supervisor-owned `workflow-integrity` baseline/candidate evals and
native independent review supplement the regressions. Its compile/manual
smoke cases are not substitutes for the journey assertions. Exact candidate,
commands, results and remote CI are recorded in the task, gate and PR evidence.
See [architecture boundaries](architecture/README.md) for the broader design.
