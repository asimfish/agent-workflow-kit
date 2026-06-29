# Document Maintenance

## The Document Graph

Use three layers:

- Plan layer: `.agent/PROJECT_PLAN.md` and `.agent/TASKS.md`.
- Task layer: `.agent/tasks/*.md`, one file per task.
- Evidence layer: `.agent/logs/`, `.agent/handoffs/`, `.agent/decisions/`.

The plan layer stays short. The task layer records durable task facts. The evidence layer records raw progress and receipts.

## Update Timing

- Start of task: `agentctl work`.
- If no task exists: `agentctl work --auto-create --title ... --scope ...`.
- After a phase: `agentctl note`.
- When scope changes: edit the task contract first.
- When sequencing changes: edit `.agent/PROJECT_PLAN.md`.
- End of task: `agentctl finish`.

Humans may edit plan or task docs at any time to steer the project. Agents must
notice those edits, re-read the changed documents, run `agentctl refresh`, and
continue. Human document review is useful, but not a required workflow step.

## Required Formats

Plan task board rows:

```markdown
- [ ] T-001 - short title (owner: agent-id)
```

Task stage log rows:

```markdown
- YYYY-MM-DD HH:MM:SS short factual update
```

Plan change log rows:

```markdown
- YYYY-MM-DD HH:MM:SS - agent-or-human - change and reason.
```

Completion records are written by `agentctl finish`; agents should fill missing
artifacts, risks, and follow-ups before finishing.

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
