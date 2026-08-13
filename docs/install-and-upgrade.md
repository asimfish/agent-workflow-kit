# Install And Upgrade

## Install

### Option A: ask an agent to install it

Start an agent inside your target project and say:

```text
Install https://github.com/asimfish/agent-workflow-kit.git into this project.
```

After installation, start normal work with:

```text
按 .agent 规范开始工作。
```

The agent should read the installed `AGENTS.md` and `.agent/WORKFLOW_ENTRY.md`,
then run the workflow by itself.

### Option B: install manually

```bash
git clone https://github.com/asimfish/agent-workflow-kit.git
cd agent-workflow-kit
./install.sh /path/to/your/project
# equivalent to:
python3 tools/agentctl.py init /path/to/your/project
```

### Install semantics

Normal installation is preflighted before any file is written. Existing
`AGENTS.md`, PR template, and Codex/Claude/Cursor hook JSON are merged in
managed sections while project content is preserved. Project-owned `.agent/`
plans, tasks, rules, and runtime history are seeded only when absent.
Re-running `init` is idempotent.

The install manifest records the kit version, schema, source commit, and
protocol epoch. A protocol-changing upgrade first replaces only the managed
controller and hook bridge with barrier-aware entrypoints, then waits while
active or uninspected stale conversations can still write.

On the first clone/install or after a managed hook upgrade, review and trust
the project hooks if the client asks. In Codex, inspect `/hooks`; in Claude
Code or Cursor, accept the project configuration prompt, then reopen the
conversation once so `SessionStart` runs. The repository cannot approve its
own hooks.

If a managed tool or workflow file was edited locally, installation stops
before making partial changes. Inspect the diff first, then explicitly replace
only the kit-managed files when appropriate:

```bash
python3 tools/agentctl.py init /path/to/your/project --force-managed
```

## Upgrade Existing Projects And Sessions

Give one agent this instruction from the target project:

```text
Upgrade this project's Agent Workflow Kit from https://github.com/asimfish/agent-workflow-kit.git.
Preserve project-owned .agent plans and task history, run agentctl migrate, and
follow its action until it returns continue. Never auto-release another session.
```

The agent reruns the same idempotent `init`. If writers remain, it upgrades the
managed enforcement entrypoints, defers template/state migration, and lists the
conversations that must finish or release. Existing supervised background runs
may finish during this drain; new tasks, runs, resources, worktrees, and file
writes are blocked.

After installation, a conversation created under the old protocol must re-read
its plan and task document and run:

```bash
python3 tools/agentctl.py upgrade rebind
```

Old-epoch writes remain blocked until that explicit rebind. Reopening is only
necessary when `migrate` reports that the client never supplied a trusted
conversation identity.

The drain barrier governs the kit-managed project entrypoints. Arbitrary copied
old controllers and untrusted processes that ignore project hooks require an OS
sandbox or separate access-control boundary.

## Migrate Actions

`agentctl migrate` narrows installation, identity, document receipt, and
stale-session state into one required action. It exits zero only for
`continue`.

| Action | Agent behavior |
|---|---|
| `continue` | Start or resume normal `.agent` work. |
| `refresh` | Read the named workflow/task documents, run `refresh`, and audit again. Matching legacy singleton state is first moved by `status`. |
| `restart` | Reopen the conversation so `SessionStart` establishes a trusted identity, then audit again. |
| `inspect_sessions` | Inspect pre-upgrade claims whose identity is unknown; release only a verified closed conversation, then audit again. |
| `inspect_stale` | Inspect the recorded task and working tree; release only after explicit verification. |
| `repair_install` | Rerun `init` from the latest kit, resolving managed-file conflicts without overwriting project state. |

`migrate` does not download code, edit task state, refresh receipts, or release
claims. An already-running process cannot receive a new `SessionStart`
identity; that is the only case where reopening the conversation is required.
Identifiable stale peers are reported as warnings instead of globally blocking
unrelated work selection; `work/start` still rejects same-task,
overlapping-scope, and exclusive conflicts.

## Identity Policy

On Codex, Claude Code, and Cursor integrations that invoke the installed
`SessionStart` hook, a missing provider conversation ID is replaced by a fresh
workflow ID for that startup. A terminal-only or default identity is never
accepted for controller mutations after bootstrap. The explicit exception is
`init`, which must be able to install or repair the hooks that establish an
identity; its existing merge/conflict checks remain authoritative.

The controller defaults every other command to identity-required, with a small
audited read-only allowlist, and the pre-tool hook applies the same policy to
script-path and `python -m tools.agentctl` shell commands. When identity is not
trusted, read-only `sessions list` scans atomic records without creating a lock
or refreshing `.agent/state/SESSIONS.md`; a trusted SessionStart or normal work
cycle refreshes that generated view later. Two agents therefore cannot silently
fall back to one terminal ID, including through low-level commands such as
`task create`.
