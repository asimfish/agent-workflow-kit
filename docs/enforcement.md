# Enforcement Model

This kit uses three enforcement layers.

## Layer 1: Agent Entry Protocol

`.agent/WORKFLOW_ENTRY.md` is the single source of truth for startup behavior.
`AGENTS.md`, `.cursor/rules/agent-workflow.mdc`, `.cursor/hooks.json`,
`.codex/hooks.json`, and `.claude/settings.json` point the agent to that entry
and tell it what must happen before work starts.

The human prompt can be only:

```text
按 .agent 规范开始工作。
```

The agent expands that short prompt into:

```bash
python3 tools/agentctl.py work --agent codex
```

`work` resumes the current task or auto-claims the next assigned `ready`/`todo`
task, then creates a read receipt with hashes for the workflow entry, plan, task
index, agent registry, operating and GitHub rules, checkpoint policy, and task
document. Later checks detect when those files change and require an explicit
`agentctl refresh`. `agentctl note` and `agentctl finish` enforce the same receipt
directly, so a note cannot silently erase evidence that instructions changed.

The plan and task-index receipts are scope-aware: another task's index row,
another task's plan checklist row, and plan Change Log appends are excluded from
this session's receipt, so unrelated task lifecycles do not force a refresh on
every peer. Plan-body edits, rule changes, this task's own row, and this task's
own document still invalidate the receipt.

Automatic task allocation uses a private checkout namespace plus the workflow
session key to produce `T<16-hex-shard>-NNN` IDs. The namespace is stored in
local Git metadata rather than committed project files. This prevents two full
clones of the same board snapshot, two worktrees, or two conversations from
silently assigning the same default ID; explicit `--new-id` values are unchanged.

Shell commands whose writes cannot be statically enumerated - inline
interpreter code (`python -c`, `node -e`), interpreter/script invocations,
`./project-scripts`, `rsync`, `dd`, archive extraction, and download `-o`
targets - are classified as mutating, so they require an active task session
and pass through the session-level guard. Because their target paths stay
opaque, the guard additionally reconciles the working tree on every mutating
action: a tracked file modified outside every live session's effective scope
is reported as an escaped write and blocks further mutations until it is
reverted, claimed through a task scope, or committed separately by a human.
`agentctl` invocations are exempt from the opaque classification; the
controller enforces its own per-command identity and mutation policy.

Shell commands are classified before execution: an explicit read-only
allowlist passes freely, path-extractable writes are checked against the
active scope, and everything else — interpreters with inline code, project
scripts, test/build runners, archive and download tools, nested shells, and
unknown executables — is an opaque write. Opaque writes require an active
task session and exclusive use of the checkout: beside another live session
they are refused with worktree guidance, because their output paths cannot
be attributed afterwards. Worktree allocation is a pre-start transition from
a committed `todo`/`ready` task, not an implicit migration of an active dirty
shared checkout. Every mutating action also reconciles the working tree; a
tracked file modified outside every live session scope (including
tracked dotfiles such as `.env`) blocks further mutations until reverted,
claimed, or committed separately by a human.

Three hardening rules close the remaining static-analysis gaps. Git
subcommands are default-deny: only an explicit read allowlist (`status`,
`log`, `diff`, `show`, flag-aware `config --get`, `stash list`, listing-only
`branch`/`reflog` forms, and similar) passes as read-only, while `restore`,
`checkout`, `stash`, `rm`, `mv`, `apply`, `cherry-pick`, `revert`, `fetch`,
`reflog expire/delete`, branch creation/deletion/configuration, and every
unrecognized subcommand require Git exclusivity, with restore/checkout/rm/mv
path targets additionally scope-checked. A write-classified command that
yields no checkable path escalates to opaque instead of passing on an empty
path list. Command substitution, backticks, and process substitution
anywhere in a command make the whole command opaque, including inside quoted
literals (fail closed).

Git operations that mutate repository-wide metadata require exclusivity across
all registered worktrees, not just one checkout. This includes force-updating
or deleting tags, mutating Git config, deleting refs with `:ref`/`+:ref` push
refspecs, plus-prefixed force refspecs, parameterized `--force-with-lease`,
forced/deleting pushes, branch rewrites, pruning, reflog mutation, garbage
collection, and worktree removal. Combined short flags are interpreted by
capability (`tag -fa`, `branch -df`, and `push -fu` cannot bypass the guard).

The read-only allowlist is argument-verified, not name-based: `sort -o` and
`uniq` with an output operand are path-checked writes, `base64 -o` likewise;
`awk`, `yq`, and inline `perl` are opaque because their embedded languages
can write arbitrary files; `sed` counts as read-only only for provably
print-only scripts (line addresses plus `p`), and both dash and old-style
bundled `tar` option words are recognized for extract/create/update modes.
Git `diff`/`log`/`show --output` destinations are resolved after leading `-C`
options and checked as file writes; external-diff/textconv configuration,
execution flags, and pager-opening grep forms are opaque. `rg --pre`,
`fd --exec`, `ag --pager`, `find -fprint`, `tree -o`, and both normal and
stdin-based `xxd` output operands cannot inherit the read-only classification
of their executable name. GNU target-directory forms are checked, `mv` claims
its deleted sources as well as its destination, while in-place `sed`/`perl` is
opaque because its embedded program can write additional files. Curl write-out,
libcurl, stderr, SSL-session, and output-directory options plus `gh --web` are
write/execution surfaces. Newlines, background/pipeline control operators,
dynamic shell variable/positional expansion, globs in mutation targets, unsafe
environment assignments, and cwd-changing `env` wrappers are opaque because
their final effects cannot be proven statically.
Host/process/remote mutations such as `sysctl -w`, `kill`, mutating `gh`
actions, and default `wget` downloads are opaque beside peers; a conservative
set of `gh ... view/list/status` actions remains read-only.
Static shell parsing still cannot PROVE arbitrary commands are read-only, so
the guarantee model remains: verified reads and path-checked writes may run
beside peers; everything else requires an exclusive checkout or a task
worktree.

Shell parsing itself is hardened against composition tricks: newlines
separate commands exactly like `;`, `&>`/`&>>`/`>|`/`>&file` redirects are
recognized and their targets scope-checked, `mv` treats every source operand
as a write (a rename deletes the source), and `timeout`/`nice`/`stdbuf`/
`setsid`/`env` wrappers are stripped before classification. Git operations
that rewrite state shared by every worktree — branch deletion/rename/copy,
`reflog expire/delete`, pruning fetches, `gc`/`prune`/`update-ref`, tag
deletion, forced or deleting pushes, and `worktree remove/move/prune` —
require that no other conversation is live in ANY checkout of the
repository, not just the current one.

Path guards enforce document ownership on top of the write scope. The active
task's own document is always part of the effective scope, while
controller-generated files (`board.json`, `TASKS.md`, the agent registry,
`state/`, the shared progress log, `gates/`, loop runtime and reports, the
guidance bus and handoffs, eval runs/decisions/keys, and the install manifest)
are rejected for direct agent edits regardless of scope; the denial names the
owning `agentctl` command. Policy definitions such as `loops/checkpoints.json`
and `evals/suites.json` stay scope-based. Direct edits to `PROJECT_PLAN.md`,
rules, or another task's document require the declared scope to cover them,
which worker scopes should not.

If there is no assigned task for the current request, the agent creates and starts
one itself:

```bash
python3 tools/agentctl.py work --agent codex --auto-create --title "<current request>" --scope "<paths>"
```

Codex, Claude Code, and Cursor project hooks call `tools/agent_workflow_hook.py`.
Codex and Claude match every tool call; Cursor uses generic `preToolUse` plus
`beforeShellExecution` and `beforeMCPExecution`, all routed through the same
guard. Known structured file/notebook/MCP mutations contribute every source and
destination path. Unknown non-read tools and pathless mutations fail closed as
opaque beside peers. The session-start hook injects the protocol into context,
and the stop hook reminds the agent to record progress or complete the task.

### Long-task anti-drift (focus re-injection)

The session-start hook matches `startup|resume|clear|compact`. Whenever a long task is
resumed, cleared, or its context is compacted, the hook calls `agentctl focus` and re-injects
the active task's goal, write scope, and stage TODO plus the required reading list.
This keeps a long-running agent anchored to its task and plan instead of drifting.
Run `agentctl focus` manually any time to re-anchor.

Conversation forks are treated as new runtime owners. The hook binds a persisted
workflow session ID to the host runtime that supplied it and carries a hashed
parent lineage plus an optional fork-instance key. If a child inherits the
parent's environment, `agentctl` ignores the stale owner-bound ID. If a provider
reports identical parent/child IDs, `SessionStart` establishes an isolated local
instance; later mutating hooks block when that instance was not propagated.
If a nested CLI first creates a task from its host runtime and a later hook adds
a payload session ID, the hook reuses the already-recorded runtime session rather
than switching identities mid-task. When that payload ID was generated inside
the nested SDK and is absent from inherited environment variables, SessionStart
exports the normal identity and the first `agentctl work` hook claims an atomic,
checkout-specific local binding under the Git common directory when that export
does not reach the shell. Hook processes and the agent shell may also receive
different runtime-variable subsets. While the binding is still pending, the
hook tests at most one payload-derived environment for each supported provider
variable and accepts only one active workflow session. It then persists that
anonymous session key and stops probing. Zero matches remain pending; multiple
matches fail closed with an ambiguity error. The record contains only hashed
provider/runtime identities and the anonymous workflow key. Another payload
cannot replace a fresh pending binding or an active runtime session in that
checkout; independent worktrees use distinct binding keys. Malformed binding
state also fails closed. Explicit fork lineage or an instance key never uses
this fallback.

### Runtime commands

`agentctl` is the single controller:

- `start` — read receipt + acquire task lock + write-scope conflict check + board `in_progress`.
- `work` — normal entry point; resume, claim, or auto-create a task and then start it.
- `focus` — print the current task focus (re-read before continuing).
- `note` — append a stage note to the task doc, log, and board.
- `finish` — write the completion record, free the lock, move the task to `review`.
- `gate approve|reject` — independent review gate: `review -> done`
  (auto-checks the plan box) or `-> blocked`. The command requires `--by` to
  match the active agent session, a registered supervisor/planning/review role,
  and a separate in-progress reviewer task; the task owner cannot decide their
  own task. Task completion records hashed host runtime identifiers from Codex,
  Claude, Cursor, or the hosting agent platform. Gate decisions require the
  current host runtime to match the reviewer session and differ from every
  runtime that participated in the worker task.
- `gate reconcile-github` — post-merge human-review reconciliation. It reads
  authoritative PR metadata through authenticated `gh`, requires `MERGED`,
  verifies the merge commit is in the current history, matches `--by` to
  GitHub's `mergedBy`, binds the PR repository to the checkout's `origin`, and
  confirms the complete GraphQL cursor-paginated PR file list changed the task
  document before moving `review -> done`. GitHub Enterprise queries and
  unqualified `--repo OWNER/REPO` arguments use the checkout origin's verified
  host. It records the PR URL, merge commit, actor, and
  timestamps in `.agent/gates/`; it does not merge a PR.
- `board` / `task` / `agents` — machine-readable task board, task scaffolding, agent registry.
- `refresh` — re-record doc hashes after the plan/rules/task docs changed.
- `migrate` — read-only post-upgrade audit that classifies install health,
  current/legacy identity, document receipts, and stale session claims. It never
  refreshes receipts or releases claims itself.

`start`, `progress`, and `complete` remain available as low-level equivalents for
debugging and scripted migrations.

## Layer 2: Local Git Hooks

Installed hooks live in `.githooks/`, and `init` sets `git config core.hooksPath .githooks`:

- `pre-commit`: requires an active agent session and staged task/plan/log updates when code or data changes.
- `commit-msg`: requires Conventional Commits and a task ID.
- `pre-push`: requires pushed commits to have task IDs and requires pushed tasks to be `review` or `done`.

A `done` task must have:

- a filled Completion Record,
- tests recorded,
- a checked task item in `.agent/PROJECT_PLAN.md`.

## Layer 3: GitHub CI Gate

The installed workflow `.github/workflows/agent-workflow-check.yml` runs `agentctl check --mode ci` on push and pull request. This catches bypassed local hooks in GitHub.

## Important Limitation

Git can enforce commit and push rules. Agent task start is enforced by the project protocol plus lifecycle hooks where the current tool supports them. Keep Git hooks and CI enabled because agent-native hooks can vary by product, trust settings, and client version.

`agentctl doctor` validates that all three managed hook entries are present and
that Cursor's mutating hooks fail closed. It also reports the remaining boundary:
project hooks still depend on the client loading them, repository trust, and
user or organization policy. A repository cannot prevent an administrator from
disabling its native hooks; Git hooks and required GitHub checks remain the
later enforcement layers.

Project hooks are coordination guardrails, not an operating-system sandbox.
They conservatively route unprovable commands to exclusive worktrees, but they
cannot contain hostile code or observe side effects performed outside the client
hook lifecycle. Use an external sandbox for untrusted execution.

No local hook can infer two distinct conversations when a client exposes no
different runtime/session/fork signal and does not run `SessionStart`. Supported
clients normally provide a unique current session or conversation ID; the
fail-closed fork path covers explicit but ambiguous parent metadata.

## Installation And Upgrade Safety

`agentctl init` computes all writes before mutation. It preserves project-owned
`.agent/` state, merges managed `AGENTS.md` and PR-template blocks plus provider
hook entries, and records exact hashes for kit-managed executables, Git hooks,
Cursor rules, and CI workflows in `.agent/install-manifest.json`. A locally
modified managed file aborts the whole install. `--force-managed` is an explicit
operator acknowledgement to replace those managed files after inspection.
Provider hook upgrades remove only the managed command node, preserving custom
commands that share the same matcher. `doctor` compares the effective matcher,
command, timeout, and fail-closed fields against the installed contract.

After an upgrade, an older conversation runs `agentctl migrate` before editing.
The audit returns `continue`, `refresh`, `restart`, `inspect_sessions`,
`inspect_stale`, or `repair_install` and exits nonzero until the transition is
resolved. `inspect_sessions` covers active or stale pre-upgrade claims that do
not carry trustworthy identity-source metadata; they are never inherited or
released automatically. Only a missing trusted `SessionStart` identity requires
reopening the conversation. Project-owned plans, task history, and stale claims
are not modified by the audit.

If a provider does not expose a unique conversation ID, each normal
`SessionStart` generates one and exports it to later hook/controller calls.
Terminal-only and default identities are rejected for every controller command
except an audited read-only allowlist and the `init` bootstrap needed to install
the hook itself. The dispatcher defaults new commands to identity-required; the
PreToolUse shell classifier carries the same allowlist for script-path and
`python -m tools.agentctl` invocations, and a regression test enumerates every
argparse command leaf in both forms. Under an untrusted identity, `sessions
list` reads atomic records without taking the coordination lock or regenerating
the local Markdown view, keeping the diagnostic allowlist write-free. This
prevents separate agents launched under one terminal or service process from
sharing state through either normal session commands or lower-level mutations
such as `task create`.
