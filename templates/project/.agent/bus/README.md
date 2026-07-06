# Agent Bus

Machine-readable handoff packets live here.

```text
inbox/<target-task>/<packet-id>.json
inbox/<target-agent>/<guidance-packet-id>.json
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

Supervisor guidance packets let a stronger planning model, such as Fable, give
durable instructions to a worker agent, such as Codex:

```bash
python3 tools/agentctl.py guidance create \
  --from-agent fable \
  --to-agent codex-gpt55xhigh \
  --to-model gpt5.5xhigh \
  --to-session xxx \
  --task T-101 \
  --summary "Plan for the next implementation phase" \
  --plan-file .agent/plans/T-101-fable-plan.md
```

When that Codex worker runs
`python3 tools/agentctl.py work --agent codex-gpt55xhigh --session-id xxx`,
unacknowledged guidance addressed to the matching agent/session is printed in the
task focus. If the packet is bound to the active task, `finish` is blocked until
the worker acknowledges it:

```bash
python3 tools/agentctl.py guidance ack <packet-id> --by codex-gpt55xhigh
```
