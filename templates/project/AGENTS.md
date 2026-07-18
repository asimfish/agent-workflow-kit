# Agent Operating Entry

All agents working in this repository must follow `.agent/WORKFLOW_ENTRY.md`.

The human may start work with only:

```text
按 .agent 规范开始工作。
```

Agents must translate that short prompt into the full autonomous workflow:
read the `.agent` plan/task/rule documents, run `python3 tools/agentctl.py work
--agent <agent-name>` before editing, record progress with `note`, finish phases
with `finish`, and obey `.agent/rules/github-standards.md` for commits and
pushes.

After this kit is upgraded, or when resuming a conversation that started under
an older kit, the agent must first run `python3 tools/agentctl.py migrate` and
follow its reported action until it returns `continue`. Migration diagnosis is
read-only: never auto-release another session or claim that documents were read.
An older already-open conversation must be reopened after upgrade so the new
SessionStart hook establishes an isolated identity before it writes project state.

Do not edit project files before the workflow entry succeeds. Git hooks and CI
remain the final enforcement layer.
