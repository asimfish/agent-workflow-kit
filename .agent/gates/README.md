# Gates

Human or reviewer approval records live here.

An agent reviewer must be registered with a supervisor/planning/review role and
must first enter a separate in-progress review task. `--by` must match that
active reviewer session; the worker task owner cannot decide their own task.
The reviewer must also run in a host-issued Agent/Thread runtime that did not
participate in the worker task; raw runtime identifiers are stored only as
hashes.

Use:

```bash
python3 tools/agentctl.py gate approve --task T-001 --by reviewer --note "verified"
python3 tools/agentctl.py gate reject --task T-001 --by reviewer --note "needs fixes"
```
