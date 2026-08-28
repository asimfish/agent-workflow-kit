# Worktree merge-back: from `finish` to an approved merge

Code and experiment tasks run in isolated worktrees on their own feature
branches. That isolation is the point -- but it also means the task's
completion record is born on the feature branch, while the review gate and
the shared board live in your planning checkout. `agentctl reconcile
merge-back` moves the ledger records between the two; this page documents
the full path, verified end to end on this repository.

If you skip this page, the failure you will hit first is a gate approval
that fails with an error about runtime evidence or an unknown task, even
though the work is plainly done. The gate is not wrong; it is reading a
checkout whose ledger has not heard about your task yet.

## The shape of the problem

After `agentctl finish` inside a worktree you have:

- on the **feature branch**: the task document with its final status, the
  updated board entry, notes, and (after review) the gate record;
- on the **planning checkout** (usually `main` or your planning branch):
  a board that still shows the task `in_progress`, or does not show it at
  all if it was auto-created inside the worktree.

`gate approve` reads the checkout it runs in. The reviewer's session, the
task document, and the board entry must all be visible **in that same
checkout**, or the gate refuses.

## Step by step

### 1. Inside the worktree: finish, then commit the ledger

```bash
agentctl finish --summary "..." --tests "..."
git add .agent/tasks/<TASK-ID>.md .agent/board.json .agent/TASKS.md
git commit -m "docs: record finish for <TASK-ID>"   # Refs: <TASK-ID>
```

Do not leave the finish record uncommitted. An uncommitted ledger cannot be
synced, reviewed, or recovered if the worktree is deleted.

### 2. Sync the ledger into the planning checkout

Do **not** `git merge` the feature branch into the planning branch just to
move ledger state -- board files conflict on every parallel task. From the
planning checkout, let the controller move the per-task records:

```bash
# preview what would move
agentctl reconcile merge-back --from-ref <feature-branch> --dry-run

# import the finished task's board entry, task document, and gate record,
# then re-render TASKS.md and PROJECT_PLAN.md
agentctl reconcile merge-back --from-ref <feature-branch>

git add .agent && git commit   # commit the imported ledger
```

By default merge-back auto-discovers every task on the source branch that
already reached `review`, `approved`, or `done` and is missing or behind in
this checkout. Name tasks explicitly with `--task <TASK-ID>` (repeatable)
to import earlier states or to refresh an entry whose status already
matches. It never regresses a local status that is ahead of the source, it
runs only from the planning checkout, and it leaves sessions, leases, and
loop state untouched.

If you need to move a record by hand (for example from a repository that
predates the subcommand), the equivalent manual steps are: copy the task
document with `git show <branch>:.agent/tasks/<TASK-ID>.md`, merge the one
`board.json` entry (never the whole file), and re-render the views with
`agentctl reconcile render`.

### 3. Independent review in the planning checkout

The reviewer must be a session whose runtime never touched the
implementation. In practice:

```bash
# register the reviewer once (planning checkout)
agentctl agents add --id <reviewer> --role "independent review"

# the reviewer needs an active review task before the gate will listen;
# create one (type review, owner <reviewer>) if it does not exist yet
agentctl task create --id <REVIEW-TASK-ID> --title "review of <TASK-ID>" \
    --type review --scope ".agent/" --owner <reviewer>
AGENT_WORKFLOW_SESSION_ID=<reviewer-session> agentctl work \
    --agent <reviewer> --task <REVIEW-TASK-ID>

# if agents.json or the board changed since the reviewer session started:
AGENT_WORKFLOW_SESSION_ID=<reviewer-session> agentctl refresh

AGENT_WORKFLOW_SESSION_ID=<reviewer-session> agentctl gate approve \
    --task <TASK-ID> --by <reviewer> --note "..."
```

Commit the gate record (`.agent/gates/<TASK-ID>.md`) plus the ledger sync
from step 2 in one commit on the planning side.

### 4. Merge the code

Push the feature branch, open the PR, wait for CI, merge. The gate record
from step 3 is what makes the merge legitimate; CI checks task/ledger
consistency but does not replace the human-visible review trail.

### 5. Clean up

```bash
agentctl worktree release <lease-id>        # after the branch is merged
agentctl reconcile close-decided-reviews    # closes the review task
```

If the merge deleted the local feature branch out from under the worktree
(e.g. `gh pr merge --delete-branch`), `worktree release` may report a
mismatch; release it with the lease id and record the reason.

## Conflict rules when ledgers diverge

| File | Rule |
|---|---|
| `.agent/board.json` | Union of task entries. For the same task id, the finishing/approved side wins; never resurrect an older status. |
| `.agent/TASKS.md` | Row-wise union; one row per task id. |
| `.agent/PROJECT_PLAN.md` | Keep both sides' checklist lines; a task checks off only when its gate decision exists. |
| `.agent/loops/state.json` | Keep the planning checkout's version; loop state is checkout-local bookkeeping. |

## Failure messages and what they actually mean

| You see | Likely cause | Do |
|---|---|---|
| gate approve: "no host runtime evidence" / task unknown | Ledger not synced into this checkout | Step 2, then retry |
| gate approve: reviewer session not bound / no active task | Reviewer claimed their task in a different checkout | Re-claim in this checkout, `agentctl refresh`, retry |
| gate approve: read-receipt guard rejects | `agents.json` or board changed after the reviewer session started | `agentctl refresh` as the reviewer, retry |
| push rejected: task must be review/approved/done | You are pushing ledger commits for a task still `in_progress` | `agentctl finish` first, or push after the gate decision |
| worktree release: lease/path mismatch | Branch was deleted or checkout moved after merge | Release by lease id and note the reason |
