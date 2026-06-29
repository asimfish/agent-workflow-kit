## Agent Workflow Kit Protocol

Before editing this repository:

1. Run `python3 tools/agentctl.py work --agent codex`.
2. Read the focus printed by `agentctl work`.

If no task exists for the current user request, create and start one yourself:

```bash
python3 tools/agentctl.py work --agent codex --auto-create --title "<current request>" --scope "<paths>"
```

Humans normally steer by editing `.agent/PROJECT_PLAN.md` and task docs. Re-read
those files and run `python3 tools/agentctl.py refresh` when they change.

After each meaningful phase, run `python3 tools/agentctl.py note "<short factual update>"`.
Before completion, run `python3 tools/agentctl.py finish --summary "<what changed>" --tests "<commands run>"`.
