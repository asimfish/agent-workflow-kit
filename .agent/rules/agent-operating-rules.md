# Agent Operating Rules

## Iron Rules

1. Read the project plan and assigned task document before editing.
2. One task owns one bounded write scope.
3. Every meaningful phase produces a task-doc update.
4. The supervisor owns task decomposition and final synthesis.
5. Workers own one output contract and report files touched, tests run, and open risks.
6. Do not rely on chat memory for handoff; write the durable fact to `.agent/`.

## Team Patterns (pick exactly one primary structure)

Aligned with `agent-team-patterns.md` from `super_skill_team`. Declare the primary
pattern in `.agent/PROJECT_PLAN.md`; if you combine patterns, say so explicitly.

| Pattern | Shape | Use when | Avoid when |
|---|---|---|---|
| 1. Pipeline | A→B→C | strictly ordered, each step depends on previous | tasks can run in parallel |
| 2. Fan-out/Fan-in | A→[B,C,D]→E | independent parallel work, merged at the end | tasks have dependencies |
| 3. Expert Pool | Router→{X,Y,Z} | dynamic routing by input type | no clear routing signal |
| 4. Producer-Reviewer | P↔R loop | generate + review until a quality bar | no clear quality signal |
| 5. Supervisor | S→{any worker} | central dynamic dispatch, 3–8 workers | workers ≥ 10 (supervisor overloads) |
| 6. Hierarchical | Root→L1→L2 | large task trees, depth ≤ 3 | depth > 3 (use a DAG) |

Selection decision tree:

```text
Is the order strict?
├─ yes -> Pipeline
└─ no  -> Can tasks run independently in parallel?
          ├─ yes -> Fan-out/Fan-in
          └─ no  -> Need classification/routing?
                    ├─ yes -> Expert Pool
                    └─ no  -> Is there a clear quality signal to loop on?
                              ├─ yes -> Producer-Reviewer
                              └─ no  -> Need recursive decomposition?
                                        ├─ yes -> Hierarchical
                                        └─ no  -> Supervisor
```

Combination iron rules: the primary structure is unique; declare sub-structures in
the relevant stage; keep cross-layer data contracts file-based.

The data-collection example (agent1 = records 1-20, agent2 = 21-40, agent3 = 41-60)
is **Fan-out/Fan-in**: independent write scopes, merged by a deterministic rule.

## Worker Output Contract

Every worker update must include:

- Task ID.
- Current status.
- Files changed or artifacts created.
- Verification performed.
- Risks, blockers, and follow-ups.

## Concurrency Rules

- Prefer separate branches or worktrees for parallel implementation; assign each
  parallel agent a dedicated worktree + branch to avoid index/HEAD contention.
- Do not assign overlapping write scopes to different agents.
- If two tasks must touch the same file, serialize them in the dependency map.
- If an agent finds scope drift, update the task doc before editing outside scope.
