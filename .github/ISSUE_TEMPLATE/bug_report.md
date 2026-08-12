---
name: Bug report
about: A workflow command, hook, or supervised run misbehaved
labels: bug
---

## What happened

<!-- The command you ran, what you expected, and what you got. -->

## Reproduction

```bash
# minimal steps, ideally against a fresh `agentctl init` project
```

## Evidence

- Controller output (paste the failing command's stdout/stderr):
- Relevant `.agent/tasks/<id>.md` stage log entries (when a task is involved):
- For supervised runs: `run show <id> --json` and the per-lease
  `<lease>.supervisor.log` tail.

## Environment

- OS (POSIX/Windows) and Python version:
- Client (Codex / Claude Code / Cursor / other):
- Kit version or commit:
