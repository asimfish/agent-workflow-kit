# Documentation Standards

## Source Of Truth

- `.agent/PROJECT_PLAN.md`: long-term goal, milestones, task board, dependency map, project-level change log.
- `.agent/TASKS.md`: compact task index.
- `.agent/tasks/*.md`: task contract, stage plan, stage log, verification, completion record.
- `.agent/handoffs/*.md`: explicit cross-agent handoffs when one task output becomes another task input.
- `.agent/decisions/*.md`: architecture or process decisions that should outlive one task.
- `.agent/loops/*.md`: bounded loop contracts with Trigger, Execute, Check, Feedback, Memory, and Next.
- `.agent/loops/runs/*.md`: durable run reports for each loop cycle.
- `.agent/logs/*.md` and `.agent/logs/*.ndjson`: append-only audit trail.

## Maintenance Rules

- Keep the project plan skimmable; detailed evidence belongs in task docs or logs.
- Keep task docs factual. Avoid long reasoning transcripts.
- When a task changes scope, update its task contract before doing the new work.
- When a dependency changes, update both `.agent/PROJECT_PLAN.md` and `.agent/TASKS.md`.
- Archive obsolete detail instead of deleting useful history.

## Plan Edit Rules

- Preserve the `PROJECT_PLAN.md` top-level heading schema.
- Edit strategy, milestones, dependencies, risks, verification, and change log in place.
- Add task board rows only in this format: `- [ ] T-001 - short title (owner: agent-id)`.
- Check a task row only when the machine board status is `done`.
- Record non-trivial plan changes in `## Change Log` with timestamp, editor, change, and reason.

## Task Doc Edit Rules

- Preserve the task document top-level heading schema.
- Keep `Task Contract` stable once work begins; if it changes, record why in `Stage Log`.
- Use `Stage Plan` as the current TODO checklist. Do not delete completed stages.
- Use `Stage Log` for short factual progress entries only.
- Use `Verification` for commands, results, skipped checks, and reasons.
- Use `Completion Record` only when the task is ready for review.

## Agent Update Order

For normal work, agents update documents in this order:

1. Before scope or acceptance criteria changes: update the task contract and, if needed, the plan.
2. During work: append a short `agentctl note` progress entry.
3. Before finishing: update verification, artifacts, risks, and follow-ups.
4. Finish through `agentctl finish` so status and completion data stay synchronized.

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
- Keep loop run reports factual. Do not replace run reports with chat summaries.
