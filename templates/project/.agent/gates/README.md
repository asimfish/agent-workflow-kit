# Gates

Human or reviewer approval records live here.

Use:

```bash
python3 tools/agentctl.py gate approve --task T-001 --by reviewer --note "verified"
python3 tools/agentctl.py gate reject --task T-001 --by reviewer --note "needs fixes"
```

