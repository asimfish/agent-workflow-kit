# AGENT-007 - validate workflow against original requirements

Status: done
Owner: codex
Agent: codex
Created: 2026-06-29 15:31:15
Updated: 2026-06-29 15:31:15

## Format Rules

- Keep this task doc factual and current; do not paste long reasoning transcripts.
- Preserve all top-level headings. Add subsections only under the existing headings.
- Update `Status:` through the workflow commands when possible.
- Use stable paths relative to the repo root.
- If a human edits this file, agents must re-read it and run `python3 tools/agentctl.py refresh` before continuing.

## Task Contract

- Goal: Test whether the current Agent Workflow Kit design satisfies the user's original requirements.
- Non-Goals: Do not redesign the system unless the validation finds a blocking gap.
- Dependencies: Current install templates, hooks, `agentctl`, documentation standards, GitHub standards.
- Expected Deliverables: A requirement-by-requirement validation report and smoke-test evidence.
- Definition of Done: Each original requirement is classified as pass, partial, or gap with concrete evidence from files or command output.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `.agent/, docs/, README.md, tools/, templates/, AGENTS.md, .githooks/, .github/`
- Files likely to touch: `.agent/tasks/AGENT-007.md`, optional validation report in `docs/`.
- Files explicitly out of scope: unrelated product code, secrets, remote repository settings.

## Stage Plan

Use one checkbox per stage. Do not delete completed stages; append changed or new stages with a short reason.

- [x] Stage 1: Map original requirements to implemented mechanisms.
- [x] Stage 2: Run fresh-project smoke tests for installation, autonomous task flow, hooks, docs, and Git checks.
- [x] Stage 3: Record pass/partial/gap results and complete the task.

## Stage Log
- 2026-06-30 01:56:53 Recorded requirement-by-requirement acceptance validation report.
- 2026-06-29 15:33:13 Defined validation criteria against the original workflow requirements.

Format: `- YYYY-MM-DD HH:MM:SS <short factual update>`.


## Verification

- Commands to run:
  - `python3 tools/agentctl.py check --mode manual`
  - Fresh-project install and autonomous workflow smoke tests
  - Hook and Git check simulations
- Expected result:
  - The system satisfies the original workflow requirements or records precise remaining gaps.
- Result:
  - Passed. See `docs/acceptance-validation.md`.
  - Caveats: agent-native hooks depend on client support; language-specific code lint must be configured per target project.

## Completion Record
- Summary: Validated Agent Workflow Kit against the original requirements and recorded pass/partial results.
- Tests: fresh-project acceptance smoke; hook simulations; Git check simulations; agentctl check manual
- Completed-at: 2026-06-30 01:56:53
