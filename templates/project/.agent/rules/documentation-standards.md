# Documentation Standards

## Source Of Truth

- `.agent/PROJECT_PLAN.md`: long-term goal, milestones, task board, dependency map, project-level change log.
- `.agent/TASKS.md`: compact task index.
- `.agent/tasks/*.md`: task contract, stage plan, stage log, verification, completion record.
- `.agent/handoffs/*.md`: explicit cross-agent handoffs when one task output becomes another task input.
- `.agent/decisions/*.md`: architecture or process decisions that should outlive one task.
- `.agent/logs/*.md` and `.agent/logs/*.ndjson`: append-only audit trail.

## Maintenance Rules

- Keep the project plan skimmable; detailed evidence belongs in task docs or logs.
- Keep task docs factual. Avoid long reasoning transcripts.
- When a task changes scope, update its task contract before doing the new work.
- When a dependency changes, update both `.agent/PROJECT_PLAN.md` and `.agent/TASKS.md`.
- Archive obsolete detail instead of deleting useful history.

## Task Doc Quality Bar

A task doc is complete only if a fresh agent can answer:

- What is the goal?
- What should not be done?
- What files may be edited?
- What has already been done?
- What remains blocked?
- How should completion be verified?

## Task Status Vocabulary

Use one shared status machine across `PROJECT_PLAN.md`, `TASKS.md`, the task docs,
and (once enabled) `.agent/board.json`:

```text
todo -> ready -> in_progress -> review -> approved -> done
                         └----> blocked        └----> failed
```

- `review` is required before `done`; do not jump straight to `done`.
- `blocked` must record the blocker in the task doc.

## Freshness

- Every write to a plan/task/board file updates its `last_updated` (or appends a
  dated log line). A fresh agent should be able to trust the newest timestamp.
- Keep `.agent/logs/*` append-only; never rewrite history, archive instead.

