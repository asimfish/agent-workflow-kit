# Agent Bus

Machine-readable handoff packets live here.

```text
inbox/<target-task>/<packet-id>.json
outbox/<source-agent-or-task>/<packet-id>.json
done/<packet-id>.json
failed/<packet-id>.json
```

Create packets with:

```bash
python3 tools/agentctl.py handoff create --from T-001 --to T-002 --summary "..." --artifact path/to/output.json
```

Packets are receipts for cross-agent handoffs. Large artifacts should stay in their
normal project paths; packet JSON should reference those paths instead of copying
content.

