# Concurrent Task-ID Collision Resolution

Date: 2026-07-20
Integration task: `T17F063E6115138DE-006`

## Context

Two full clones allocated numeric task IDs before clone/session namespacing was
available. Both histories are valid, but several IDs came to describe different
work. Commit `cd69df4` prevents recurrence by deriving automatic IDs from a
private checkout key and conversation identity.

## Resolution

The definitions already merged on `origin/main@ef035f5` remain canonical in the
current board, plan, task index, task documents, and gate documents. The other
definitions remain immutable and reachable through the integration branch's
first-parent history (`8ac5e64..93133f4`) and PRs #17/#25. For example:

```bash
git show 93133f4:.agent/tasks/T-088.md
git show 93133f4:.agent/gates/T-088.md
```

| ID | Canonical main meaning | Concurrent feature meaning |
| --- | --- | --- |
| `T-081` | close newline, redirect, mv-source, and shared-ref gaps | concurrent session/worktree isolation hardening |
| `T-082` | review T-081 shell parsing and shared-ref fixes | review T-081 concurrent isolation |
| `T-088` | block ancestor and glob paths beyond task scope | bind late provider payload IDs to bootstrap sessions |
| `T-089` | review T-088 scope containment | review T-088 provider bootstrap binding |
| `T-090` | block symlink/hardlink aliases into peer scopes | reconcile hook and shell runtime identities |
| `T-091` | review T-090 symlink guard and efficiency | real Claude runtime recovery verification |
| `T-092` | skip contamination scan when no peers exist | independent gate for feature T-090 |
| `T-093` | review T-092 solo-scan optimization | independent regate for feature T-088 |
| `T-095` | review T-094 read-only hook latency | close shell parsing and shared-ref gaps |
| `T-096` | make worktree escape-hatch guidance actionable | review feature T-095 parsing/shared-ref fixes |
| `T-098` | scope-check loop `Check` commands | recover and complete concurrent-session safeguards |

No new task may reuse these ambiguous feature meanings. Follow-up work uses the
namespaced IDs recorded in this checkout, beginning with
`T17F063E6115138DE-001`.
