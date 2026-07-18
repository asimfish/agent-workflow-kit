# Agent Workflow Entry

This file is the single project-level entry point for all agents.

## Human Prompt

The human can start work with only this sentence:

```text
按 .agent 规范开始工作。
```

English equivalent:

```text
Follow .agent and start work.
```

The human does not need to describe the workflow commands. The agent is
responsible for reading this file and operating the workflow.

## Agent Startup

When working in this repository, do this before any edit or mutating command:

1. Read `AGENTS.md`, `.agent/PROJECT_PLAN.md`, `.agent/TASKS.md`, and the relevant task doc under `.agent/tasks/`.
   If a loop is relevant, read `.agent/loops/` as well.
2. Enter the autonomous work loop:

   ```bash
   python3 tools/agentctl.py work --agent <agent-name>
   ```

   This also runs the `work-start` checkpoint loop from
   `.agent/loops/checkpoints.json`.

3. If no existing task matches the user's request, create and start one yourself:

   ```bash
   python3 tools/agentctl.py work --agent <agent-name> --auto-create --title "<current request>" --scope "<paths>"
   ```

4. Follow the focus printed by `agentctl work`.

`agentctl work` identifies the current Codex, Claude Code, or Cursor
conversation from its host runtime, updates its own heartbeat, and prints other
active conversations in the same checkout. The generated local view is
`.agent/state/SESSIONS.md`; it is runtime awareness, not durable project truth.

Use the backend name as `<agent-name>` when no project-specific agent ID is
assigned, for example `codex`, `claude`, `cursor`, or `agent`.

## Upgrades And Older Sessions

If the kit was just reinstalled/upgraded, or this conversation began under an
older kit version, diagnose the transition before editing:

```bash
python3 tools/agentctl.py migrate
```

The command is read-only and returns one action. Follow it until a rerun returns
`continue`: re-read and `refresh` changed documents, restart only when a trusted
`SessionStart` identity is missing, inspect pre-identity or stale claims without
auto-releasing them, or repair the managed install from the latest kit checkout.
A matching legacy singleton record is moved only by the existing `status` path
after the report tells the agent it is safe to do so. Never continue an old-kit
conversation alongside new-kit writers in the same checkout; reopen it first so
the installed SessionStart hook can establish isolated state.

After bootstrap, terminal-only/default identity is rejected by both the
controller dispatcher and PreToolUse for every non-read-only command. `init` is
the sole bootstrap exception because it installs the identity hook itself.

## During Work

- Keep edits inside the active task write scope.
- Treat another conversation's active or stale task/scope/file claim as owned.
  Native hooks refresh heartbeats and register structured file writes
  automatically. Do not bypass a session guard.
- If the workflow entry, plan, task index, task doc, agent registry, rules, or
  checkpoint policy changed, re-read them and run:

  ```bash
  python3 tools/agentctl.py refresh
  ```

- After each meaningful phase, record a short factual update:

  ```bash
  python3 tools/agentctl.py note "<short factual update>"
  ```

- When the next step is a bounded feedback cycle, run one loop cycle only:

  ```bash
  python3 tools/agentctl.py loop run <loop-id> --once
  ```

  Every loop must close Trigger, Execute, Check, Feedback, Memory, and Next, and
  must write a report under `.agent/loops/runs/`.

- For experiment or benchmark monitoring, use the project checkpoint:

  ```bash
  python3 tools/agentctl.py loop auto --checkpoint experiment-check --once
  ```

- Before starting a multi-cycle checkpoint, inspect durable runtime state. Resume
  only an interruption marked safe; stop it explicitly when the plan changed:

  ```bash
  python3 tools/agentctl.py loop status
  python3 tools/agentctl.py loop cycle --checkpoint <name> --cycles <n>
  python3 tools/agentctl.py loop resume
  python3 tools/agentctl.py loop stop --reason "<why this runtime is abandoned>"
  ```

  If status says an in-flight cycle or one-shot execution has an unknown result,
  do not resume or replay it.
  Wait for the recorded command to exit, inspect its side effects, then reconcile
  it with `loop stop --ack-inflight --reason "<what was verified>"`.

  The runner is bounded and cooperative. Never bypass an escalated follow-up or
  start a competing runtime to hide an interrupted one.

## Concurrent Conversations

- Multiple conversations may share one checkout only while their task scopes
  are disjoint. Each conversation gets an independent session record under the
  Git common directory; one conversation never resumes another conversation's
  task merely because the agent name is the same.
- Task creation, start, note, finish, and the generated session view use atomic
  writes and an advisory coordination lock. Hooks reject writes outside the
  active scope and reject a path already claimed by another live session.
- Git index, HEAD, branch, merge, and push mutations require exclusive use of a
  checkout. If another conversation is active, allocate a task-scoped worktree
  instead of bypassing the guard.
- A stale session remains visible and keeps its claims. After inspecting its
  task document and working tree, an agent may transfer that task by running:

  ```bash
  python3 tools/agentctl.py sessions release <session-key> --reason "<verified reason>"
  python3 tools/agentctl.py start --task <existing-task> --agent <agent-name>
  ```

  Releasing presence does not erase the task, plan, notes, or working files.

## Supervisor Dispatch

When the human names this agent as a supervisor and provides a Codex session ID:

1. Decompose the request into a bounded task with explicit scope and acceptance
   evidence.
2. If another agent is changing the current checkout, commit the task plan and
   allocate a clean task-scoped worktree. Run the worker or dispatch command from
   the printed path:

   ```bash
   python3 tools/agentctl.py worktree create --task <task-id> --agent <codex-worker>
   ```

3. Register the worker profile if needed, then create and dispatch one guidance
   packet:

   ```bash
   python3 tools/agentctl.py guidance create \
     --from-agent <supervisor> --to-agent <codex-worker> \
     --to-model <model> --to-reasoning-effort <effort> \
     --to-session <session-id> --task <task-id> \
     --summary "<bounded phase>" --plan-file <plan-path> --dispatch
   ```

4. After the bounded Codex turn returns, inspect the task document, diff, and
   verification evidence. Commit the bounded worker turn, leave its checkout
   clean, then run the supervisor-owned acceptance check:

   ```bash
   python3 tools/agentctl.py guidance verify <packet-id> --by <supervisor> \
     --target <worker-worktree>
   ```

   A zero Codex exit code is transport evidence, not task completion. Verification
   accepts only a signed immutable task contract and matching receipt, the exact
   worker acknowledgement, and a matching task in `review`, `approved`, or `done`
   with completion/test evidence. The signed decision records the worker HEAD,
   tree, and evidence hashes, so it remains auditable after worktree release.
   Run verification from the supervisor's own active planning/review session;
   `--target` reads evidence from the worker checkout without transferring its
   ignored runtime state.
   Send another packet only when the recorded rejection requires a new bounded
   implementation turn; otherwise gate or hand off the task.
5. For a harness, workflow, rule, or agent-runtime change, run one unchanged eval
   suite against clean baseline and candidate worktrees before approval:

   ```bash
   python3 tools/agentctl.py eval run <suite> --target <baseline-path> --json
   python3 tools/agentctl.py eval run <suite> --target <candidate-path> --json
   python3 tools/agentctl.py eval gate --baseline <baseline-id> \
     --candidate <candidate-id> --by <reviewer>
   ```

   The supervisor owns the suite and decision. A candidate must not edit or
   replace its verifier policy.

Dispatch uses the target session's existing Codex trust, approvals, and sandbox.
Never add a dangerous bypass, acknowledge guidance for the worker, or approve
work without independent evidence. Omit `--dispatch` when only a durable
file-based handoff is possible.

## Finish And Commit

Before claiming a phase complete:

1. Run the task verification commands.
2. Update task artifacts, tests, risks, and follow-ups.
3. Move the task to review:

   ```bash
   python3 tools/agentctl.py finish --summary "<what changed>" --tests "<commands run>"
   ```

   `finish` runs `pre-finish` and `post-finish` checkpoint loops automatically.

GitHub commits and pushes must obey `.agent/rules/github-standards.md`.
Git hooks enforce Conventional Commits, task IDs, staged workflow docs, and
review/done state before push.

## Human Role

Humans steer by editing:

- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/tasks/*.md`
- `.agent/rules/*.md`

Agents must treat those edits as updated instructions, refresh their read
receipt, and continue. Humans should not need to run workflow commands during
normal development.
