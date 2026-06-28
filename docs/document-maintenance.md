# Document Maintenance

## The Document Graph

Use three layers:

- Plan layer: `.agent/PROJECT_PLAN.md` and `.agent/TASKS.md`.
- Task layer: `.agent/tasks/*.md`, one file per task.
- Evidence layer: `.agent/logs/`, `.agent/handoffs/`, `.agent/decisions/`.

The plan layer stays short. The task layer records durable task facts. The evidence layer records raw progress and receipts.

## Update Timing

- Start of task: `agentctl start`.
- After a phase: `agentctl progress`.
- When scope changes: edit the task contract first.
- When sequencing changes: edit `.agent/PROJECT_PLAN.md`.
- End of task: `agentctl complete`.

## Cleanup

- Completed task docs remain in `.agent/tasks/`.
- If logs become too large, move old logs to `.agent/archive/YYYY-MM/`.
- Do not delete decision records unless the decision was created by mistake.

## Review Questions

During review, check:

- Can a fresh agent resume from the task doc?
- Does the project plan match current task statuses?
- Are write scopes still disjoint?
- Are blockers visible?
- Are completed tasks supported by verification evidence?

