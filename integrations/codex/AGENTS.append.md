## Agent Workflow Kit Protocol

Before editing this repository:

1. Run `python3 tools/agentctl.py work --agent codex`.
2. Read the focus printed by `agentctl work`.

After each meaningful phase, run `python3 tools/agentctl.py note "<short factual update>"`.
Before completion, run `python3 tools/agentctl.py finish --summary "<what changed>" --tests "<commands run>"`.
