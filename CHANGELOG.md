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

A day-one replay of the README walkthrough on a blank project, with three
concurrent conversations, a dead GPU holder, and an independent reviewer,
confirmed the coordination guarantees and found four gaps between the
prose and the tool: `doctor` is now identity-free so a human in a plain
terminal can run it, `init` gitignores the default artifact root
`.agent-artifacts/` and re-runs append only the missing entries, the
`finish` hint and gate refusals name the reviewer registration command,
and both READMEs now use the artifact root in the `run start` example,
show the reviewer registration step, and say that `agentctl` is shorthand
for `python3 tools/agentctl.py`.

Chasing that PR's flaky CI run found two real defects rather than a test
problem. First, `run start` is the supervisor's parent, so a supervisor
that crashed before claiming its lease lingered as a zombie that still
answered `kill(pid, 0)` with a matching birth marker; the pre-claim death
detector reported "alive" for its whole 30s budget, the replacement spawn
from #34 never fired, and the run surfaced later as `exited_unknown`. The
parent now checks its own `Popen` handle, and on Linux `_pid_alive` reads
the zombie state from `/proc`. Second -- exposed the moment the first fix
made the death visible -- the supervisor token came from
`secrets.token_urlsafe`, which starts with `-` one time in 64, and argparse
then rejected `--token -...` as a missing value: about 1.5% of every `run
start` never launched its payload. Tokens are hex now and the supervisor
argv attaches values with `=`. The GPU watchdog regression tests, whose
0.05s idle windows and 2--5s budgets assumed a fast machine, were resized
for loaded runners (reproduced locally at 3x CPU oversubscription) and now
dump the supervisor log when they fail.

The same day-one replay left a machine-wide `gpu:0` lock behind when its
project directory was deleted, and a second project on the host could not
get past it: its `doctor` saw a clean registry, `resource release
--force-stale` could not find a lease it did not own, and the acquire
refusal offered no way out. Resource locks are host-wide while ledgers are
per checkout, so the lock's owner record now names the holder's checkout
and that checkout's registry is the evidence. The next `resource acquire`
from any project releases holders their own registry proves dead
(released, finished, or missing past the registration grace); stale
sessions, deleted checkouts, and legacy locks are refused with the holder's
state and the new `resource release --lock <resource> --force-stale
--reason` command, which is recorded as an audit row in the releasing
checkout; live holders cannot be forced; and `doctor` in any checkout lists
machine-wide locks without a live holder. A holder checkout that exists but
cannot be read from here (another user's project on a shared host) is
`unknown`, never `missing`: `_git` swallows errors and `glob` skips
unreadable directories, so without an explicit readability probe an
empty-looking registry would have aged into an auto-release of a card
still in use. Twelve two-checkout regression tests cover the evidence rules,
including that one.

Both READMEs were rewritten for a first-time reader. They now open with the
three guarantees the kit exists for (one shared plan, one owner per task,
nothing merges unreviewed), label who does what in the walkthrough (you,
agent, reviewer), state the rules as a short list, and add a "when
something is stuck" table that maps each symptom to the command that
resolves it. The status section stopped being a log of every fix -- that
history lives here -- and the Chinese edition is written as Chinese rather
than translated sentence by sentence.

Small things the reviews of the last three changes turned up: the pre-push
task-reference check treated any `WORD-123` token as a task id, so a commit
body mentioning `non-UTF-8` or `SHA-256` could not be pushed, while a
multi-segment id such as `TR024-REVIEW-001` was read as the non-existent
`REVIEW-001`; ids glued to a preceding letter or hyphen are prose now,
middle segments are kept whole, and when a commit carries a `Refs:` trailer
only the ids on it are resolved against the board. The
Windows CI job also runs the zombie-supervisor and dash-token tests. `doctor`
reports a machine-wide lock record that names no resource instead of
skipping it, and the interlock section of the multi-session guide counts
its own items correctly.

Several machines, one remote. Sessions and locks never leave a machine, so
between machines the ledger under `.agent/` is the only channel -- and a
two-clone experiment showed it did not work as one: the pre-push hook
refused to push a claim until the task reached review, so a second machine
could not learn that a task was taken; that second machine could then
`start` the same task and the owner flipped without a word; and two clones
that each finished a task conflicted on `board.json`, `TASKS.md`, and
`PROJECT_PLAN.md` with nothing to resolve them. Three changes close this.
Commits that touch only ledger data under `.agent/` (board, index, plan,
task documents, logs, gates, run reports, handoffs, decisions, bus,
archive) are pushable at any task status; anything that changes behavior --
loop contracts, checkpoint wiring, rules, evals, policy -- and any code
still waits for review. `agentctl init` commits a
`.gitattributes` and registers an `agent-ledger` merge driver per clone that
merges the ledger per task id -- one side changed wins, a deletion racing an
advance keeps the advance, a competing edit resolves to the later lifecycle
status then the newer timestamp -- with `progress.md` as a union merge,
`loops/state.json` kept local, and plan prose merged as text with real
conflicts left for a human; `doctor` reports a clone without the driver.
`start`/`work --task` refuse a task the board shows `in_progress` when no
session in this checkout ever held it, unless `--takeover --reason` is
given, which is recorded in the task document, the progress log, and the
board entry. `agentctl sync` does the ledger-only commit, pull, re-render,
push round trip, stages ledger data only, and refuses if anything else is
staged. A side whose JSON does not parse is left as a conflict rather than
read as a deletion, and a task archived on one side stays archived when the
other side only touched its done entry. Eleven two-clone regression tests.
CI then caught the one ledger file the driver could not help with: loop run
reports were named by the second, so two clones running `work` in the same
second wrote two different files with one name and the rebase stopped on
an add/add conflict. Report names now carry a six-character nonce derived
from the host and the checkout path. The kit's own repository now carries
the `.gitattributes` block too, so its worktree rebases merge the board
instead of stopping on it.

Day-to-day follow-through on the multi-machine work. `.agent/WORKFLOW_ENTRY.md`
now tells agents to `sync` after claiming, creating, or finishing a task and
how to take over a task claimed elsewhere; `work` and `finish` print the
reminder when a remote exists; `board` marks claims from other checkouts
with their age and `doctor` flags ones quiet for a day. `sync` autostashes
unrelated local edits so a dirty tree no longer stalls the pull after the
ledger was committed, and a ledger whose `tasks` is not an object is a
conflict, not a merge. `run stop` now works from the moment `run start`
returns: a stop that lands before the supervisor registered the payload is
recorded and the supervisor cancels the launch or the stop signals the
payload as it appears, instead of the old "run process is not alive"
refusal; the supervisor also escalates a requested stop to a kill after
`--kill-seconds`, so a payload that ignores SIGTERM can be stopped without
a watchdog -- a gap Windows CI had already brushed against. Review of this change found and fixed two more: a stop that
landed before the supervisor's claim used to make the claim fail and strand
the lease, and the payload could receive a second SIGTERM from the
supervisor; the claim now accepts a lease that is already stopping and every
stop path records that it signalled before it signals. `sync` warns when
git kept an autostash it could not re-apply.
