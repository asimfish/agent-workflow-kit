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

Replaying the README install on a blank project as a person in a plain
terminal found that the kit's first commit could not pass the kit's own
hooks: pre-commit wanted an active task, which a terminal without a
conversation identity cannot claim; pre-push wanted a task id; the only way
through was `--no-verify` twice, which is what every deployment so far had
quietly done. The commit that first adds `.agent/install-manifest.json` is
now the adoption commit and the hooks accept it without a task, provided it
contains nothing but what `init` wrote (managed files are checked against
the manifest hashes, extra paths and deletions are refused by name, and
pre-push decides from the commit content so skipped local hooks change
nothing). The kit's own source checkout, which has no manifest, gets no
such exemption. `init` prints the exact `git add` and `git commit` lines on
a first install.

The same replay continued onto a second machine and found the mirror image:
a fresh clone of an adopted project has `.githooks/` and `.gitattributes` in
its tree but `core.hooksPath` and the merge driver are clone-local config
that `git clone` does not carry, so the first agent there worked with no
hooks active and nothing said so. `work` and `start` now wire both before
any task state is written when the setting is free, and refuse when
`core.hooksPath` already points elsewhere, naming the fix. The READMEs also
gained the reviewer's own `finish` (which closes the review task on the
recorded decision and which the walkthrough had left out), `--type code`
on the claim example so the worktree sentence next to it is true of it,
the `--reason` that `run stop` requires, and the real test count. When a
code task cannot get its worktree because the planning checkout is dirty,
the refusal now names the paths, or says `agentctl sync` when all of them
are ledger data another conversation has not published yet.

The task contract becomes the audit surface. Reading Prove2Me (Chen et al.,
2026), the platform behind the Fermat's Last Theorem formalization, made
one gap obvious: there, a proof is checked against a statement fixed before
anyone proves it, and humans audit only the statements; here, the `Task
Contract` section of every task document had been empty since the kit
began, and `finish --tests` was a sentence the worker typed. Now the
Definition of Done is required -- `finish` refuses without it, a review
task's recorded gate decision standing in for one -- and the tests command
is executed rather than transcribed: `finish` runs it from the checkout
root, refuses on a non-zero exit or a timeout (`AGENT_WORKFLOW_TESTS_TIMEOUT`,
default 1800s), and records `Tests-command`, `Tests-exit`, and
`Tests-duration`; `gate approve --rerun-tests` runs it again on the
reviewer's side and refuses approval if it fails, and the gate record keeps
the Definition of Done, the command, and the rerun result. Both fields are
accepted at creation (`--goal`, `--done`, `--tests-cmd` on `task create` and
`work --auto-create`, forwarded through the worktree bootstrap), by the new
`agentctl contract`, and by `finish` as a last resort; writing them through
the tool refreshes the read receipt. The task template, `WORKFLOW_ENTRY.md`,
both READMEs, and `docs/workflow.md` say to write the contract before the
work, because a Definition of Done written at `finish` is a claim and one
written at creation is a specification. A recorded `Tests-exit: 0` counts as
verification evidence wherever completion records are judged.

Review of this change rejected the first version and found three real
defects. The tests command was written into the completion record
unsanitized, so a command containing a newline could plant a forged
`Worker-runtimes:` line ahead of the real one, and the gate -- which read
only the first such line -- let a session on the same host runtime approve
its own task; a tests command must now be a single line (refused at every
entry point), the record line is guaranteed single-line regardless, and the
gate reads every `Worker-runtimes:` line so a forged one can only widen the
worker set. `agentctl contract` refreshed the read receipt without checking
it first, so a human's unread edit to the task document was silently
absorbed; it now blocks like `note` does. `gate approve --rerun-tests` ran
the command while holding the coordination lock, so every other session's
ledger command in the repository timed out for the duration; the rerun now
happens before the lock and the gate re-checks under it that the recorded
command is still the one that ran. Also from that review: tests commands
are stored verbatim (backticks used to be rewritten to quotes, so the
reviewer reran a different command than the worker), a timeout kills the
command's whole process group rather than just the shell in front of it,
and a Definition of Done written as an indented list is read as filled.

A second review found the same hole one character wider: the single-line
rule refused only `\r` and `\n`, but `str.splitlines()` -- which every
reader of the task document used -- also breaks on form feed, vertical tab,
the C1 and Unicode separators, so a form feed reproduced the forgery, and
a hand-inserted `## Notes` line inside the record ended the section early
and hid the real `Worker-runtimes` line even without any control character.
The fix closes the class rather than the character: one predicate covers
every control and boundary character (writers collapse them, the tests
command refuses them), task documents are read as physical lines only, the
completion record must be the document's last and only such section or the
gate and the evidence check refuse it as edited by hand, `finish` finds the
record header as a line rather than as a substring, the gate widens the
worker set with this checkout's own session records for the task, the
gate note is one line, and a bad tests command is refused before any
worktree is created rather than inside the bootstrap.

A third review found the last open door: the kit's own `note`, `--title`,
`--takeover --reason`, and `agents add` wrote their text into the task
document verbatim, so a note containing real line breaks became new lines
of the document; and the section reader matched headers case-insensitively
while the tamper check compared exact case, so a planted `## completion
record` was the record for every reader and invisible to the check. Now
one header predicate serves reader and checks alike, every prose field the
CLI writes into a task document is one physical line (agent ids are names:
letters, digits, `_`, `.`, `-`), `## Stage Log` is located as a line, and
`finish` refuses instead of rewriting when the document already has a
misplaced or duplicated record. The same review pinned down what the
runtime check can promise: inside one repository a same-conversation
self-review is refused whatever the committed text says; in another clone
the check is exactly as trustworthy as the checkout that wrote the record,
and the READMEs now say so instead of implying more (`docs/enforcement.md`
follows in its own docs task).

A fourth review showed the first half of that promise resting on too thin
a base: the gate learned the worker's runtimes from session records, and a
session record is one file per session key that the next `work` of the same
key overwrites -- so a conversation that finished a task, edited only the
runtime value in the record, and then opened a review task under the same
session id left no record naming it, and approved its own work on the same
checkout. A fifth review then showed that recording those runtimes at
`finish` alone was not enough either: a conversation that never finishes --
it writes the record by hand and releases its session -- left nothing local,
and the starter of a handed-off task could review what its successor
finished. Every save of a task-bound session (claim, note, refresh,
release, finish) now appends the session's runtimes to a task-keyed record
under the Git common dir, which no later `work` of the same key touches,
and the gate unions that record with the committed text and any live
session record. So inside one repository every runtime that ever held a
session on the task is refused as its reviewer, whatever the committed
record says; in another clone only the committed record exists.
A sixth review found two writers of the session row that bypassed that
recording -- the heartbeat the read-only hook runs on every tool call, and
`sessions release` -- so a conversation that joined a session by heartbeat
alone was known to the live row but never to the task-keyed record. Every
session row now reaches disk through one writer that records, so the list
in this entry is true of the code. The record's read-modify-write also
takes a lock of its own, because its writers hold different locks --
heartbeat and release the coordination lock, refresh and contract none --
and two sessions recording the same task at once could otherwise drop a
runtime the gate was about to need. Alongside: plain `work --agent <id>
--task` validates the agent name like `agents add` does, the plan bullet is
rendered from flattened board fields, and `finish` re-checks the record's
structure under the lock, after the tests command has run, before it
writes.

A plan had one level: a task either existed or was done, and a request too
large for one contract was either one oversized task or a handful of
siblings whose relationship lived in someone's head. Tasks of type
`milestone` give the plan a second level. A milestone is a node, not work:
it cannot be claimed and `work` never selects it; its children are created
with `--parent <milestone>` (through `task create` or `work --auto-create`,
which the worktree bootstrap forwards) and become its `deps`; and it closes
by itself the moment its last child reaches `done` -- at the gate, when a
review task finishes with a decision, in the closed-review sweep, on
`reconcile merge-back` and `reconcile github`, and when `sync` pulls
another machine's approval, repeating so a milestone of milestones closes
in the same step and `sync` commits the result. The completion record it
writes names the children, so the plan reads as a chain of evidence from
the leaves to the root. Dependency edges that would close a cycle, directly
or through `--parent`, are refused at creation with the cycle spelled out.
`board --tree` prints milestones with their children indented and lists the
tasks under no milestone last; the flat board marks each milestone with how
many children are done. Eleven tests cover the shape rules, the cascade
through each done-transition, both cross-machine paths, and the rendering.

Milestone review fixes: archived completed children still satisfy their parents
and appear in tree/progress views. Concurrent parent additions preserve both
dependency edges; races with completion, removal, reopening, and merged cycles
stop for reconciliation. Regression coverage includes actual archive commands,
two independently installed clones syncing through a bare remote, both merge
orders, and nested completion with archived children.
