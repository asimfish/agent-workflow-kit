# $task_id - $title

Status: todo
Owner: $owner
Agent: $agent
Created: $created_at
Updated: $updated_at

## Format Rules

- Keep this task doc factual and current; do not paste long reasoning transcripts.
- Preserve all top-level headings. Add subsections only under the existing headings.
- Update `Status:` through the workflow commands when possible.
- Use stable paths relative to the repo root.
- If a human edits this file, agents must re-read it and run `python3 tools/agentctl.py refresh` before continuing.

## Task Contract

The reviewer judges the work against this section, so fill it before the work
starts (`--goal`, `--done`, `--tests-cmd` on `work --auto-create`, or
`agentctl contract`). `finish` refuses while Definition of Done is empty.

- Goal:
- Non-Goals:
- Dependencies:
- Expected Deliverables:
- Definition of Done:

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `$scope`
- Files likely to touch:
- Files explicitly out of scope:

## Stage Plan

Use one checkbox per stage. Do not delete completed stages; append changed or new stages with a short reason.

- [ ] Stage 1:
- [ ] Stage 2:
- [ ] Stage 3:

## Stage Log

Format: `- YYYY-MM-DD HH:MM:SS <short factual update>`.

- No updates yet.

## Verification

- Tests command:
- Commands to run:
  - `python3 tools/agentctl.py check --mode manual`
- Expected result:
  - The tests command exits 0 (`finish` runs it and records the result; the
    reviewer reruns it with `gate approve --rerun-tests`), workflow checks pass,
    and the Definition of Done holds.

## Completion Record

- Summary:
- Tests: not run
- Artifacts: none
- Follow-ups: none
