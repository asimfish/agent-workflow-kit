## Agent Workflow Kit Protocol

Before editing this repository, read and follow `.agent/WORKFLOW_ENTRY.md`.

The human can start with only:

```text
按 .agent 规范开始工作。
```

That short prompt means: run `python3 tools/agentctl.py work --agent codex`
before editing, auto-create a task if needed, follow the printed focus, record
progress with `note`, finish with `finish`, and obey the GitHub rules under
`.agent/rules/`.
