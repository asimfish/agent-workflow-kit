# Acceptance Validation

Date: 2026-06-30
Task: AGENT-007

## Result

The current Agent Workflow Kit satisfies the original workflow requirements for
project-level installation, plan-driven agent work, document maintenance, GitHub
submission rules, multi-agent coordination, and long-task focus recovery.

## Requirement Matrix

| Original need | Result | Evidence |
|---|---|---|
| Install the workflow into any project repo | pass | `install.sh` / `agentctl init` installs `.agent/`, `AGENTS.md`, hooks, GitHub Action, and tool scripts |
| Agents read plan/docs before work | pass | `agentctl work` creates the read receipt and prints task focus; lifecycle hook injects focus on session start |
| Agent can start without human running commands | pass | `work --auto-create --title --scope` creates, claims, and starts a task from the current request |
| Humans only inspect/edit plan/task docs | pass | AGENTS/docs instruct agents to treat human edits as steering input and run `refresh` themselves |
| Plan and task docs have fixed formats | pass | plan/task templates include `Format Rules`; documentation standards define plan/task edit rules |
| Stage progress is recorded durably | pass | `agentctl note` writes task doc stage log and `.agent/logs/progress.md` |
| Task completion updates status and docs | pass | `agentctl finish` writes completion record and moves task to `review`; `gate approve` moves to `done` |
| GitHub commits follow standards | pass | `commit-msg` enforces Conventional Commits and task IDs |
| GitHub push is gated by task state | pass | `pre-push` requires referenced tasks to be `review`, `approved`, or `done` |
| Code/data changes must include workflow docs | pass | `pre-commit` rejects staged code/data without `.agent` doc/log updates |
| Multi-agent split with isolated scopes | pass | disjoint task scopes can run in parallel; overlapping scope is rejected |
| Long-running agents avoid task drift | pass | session-start hook re-injects current task focus on startup/resume/compaction |
| Claude Code / Codex / Cursor integration | pass with caveat | templates exist for all three; enforcement depends on each client honoring project hook config |
| Project-specific code style enforcement | partial | default code standards exist; language-specific lint/test commands must be added per target project |

## Smoke Tests Run

- Fresh project install into `/tmp/agent-workflow-acceptance.*`.
- Verified `core.hooksPath=.githooks`.
- Verified Codex, Claude Code, Cursor, GitHub Action, plan, task, and rule templates exist.
- Simulated no-session mutating tool call and confirmed hook blocks it.
- Ran `work --auto-create` and confirmed task creation plus `Status: in_progress`.
- Simulated human plan edit and confirmed stale read receipt detection.
- Ran `refresh` and confirmed manual check passes.
- Verified `pre-commit` rejects code/data changes without `.agent` updates.
- Verified `commit-msg` rejects bad messages and accepts Conventional Commit + task ID.
- Verified `pre-push` accepts a task in `review`.
- Created data-split tasks and confirmed overlapping write scope is blocked.
- Ran session-start hook and confirmed task focus injection.

## Remaining Caveats

- Agent-native hooks vary by product and client version. Git hooks and GitHub CI
  remain the reliable backstop.
- The kit cannot know every project language stack upfront. Target projects should
  add language-specific lint/test commands to their task docs and code standards.
