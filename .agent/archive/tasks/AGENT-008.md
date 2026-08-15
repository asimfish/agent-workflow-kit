# AGENT-008 - make agent bootstrap and github references explicit

Status: done
Owner: codex
Agent: codex
Created: 2026-06-30 03:58:16
Updated: 2026-06-30 03:58:16

## Format Rules

- Keep this task doc factual and current; do not paste long reasoning transcripts.
- Preserve all top-level headings. Add subsections only under the existing headings.
- Update `Status:` through the workflow commands when possible.
- Use stable paths relative to the repo root.
- If a human edits this file, agents must re-read it and run `python3 tools/agentctl.py refresh` before continuing.

## Task Contract

- Goal: Make bootstrap instructions and GitHub standards explicit enough that a user can hand this repo link to an agent.
- Non-Goals: Do not change lifecycle semantics or hook implementations unless verification finds a gap.
- Dependencies: Current README, install script, GitHub standards, and templates.
- Expected Deliverables: README agent-bootstrap instructions, external GitHub/Conventional Commits references, and confirmation notes for the user's six questions.
- Definition of Done: README tells an agent how to install from the repo URL, and GitHub standards cite the external conventions they follow.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `README.md, .agent/, templates/, docs/`
- Files likely to touch: `README.md`, `.agent/rules/github-standards.md`, `templates/project/.agent/rules/github-standards.md`, this task doc.
- Files explicitly out of scope: runtime behavior, unrelated templates, credentials.

## Stage Plan

Use one checkbox per stage. Do not delete completed stages; append changed or new stages with a short reason.

- [x] Stage 1: Verify README/install/hook behavior in the current repository.
- [x] Stage 2: Check external GitHub/Conventional Commits references.
- [x] Stage 3: Update bootstrap and standards docs, then push.

## Stage Log
- 2026-06-30 04:03:09 Added explicit agent bootstrap instructions and external GitHub standards references.

Format: `- YYYY-MM-DD HH:MM:SS <short factual update>`.


## Verification

- Commands to run:
  - `python3 tools/agentctl.py check --mode manual`
- README/rules grep for bootstrap and external references
- Expected result:
  - Workflow checks pass and task-specific acceptance criteria are met.
- Result:
  - README contains concrete GitHub clone/install instructions and an Agent Bootstrap section.
  - GitHub standards templates include external references.

## Completion Record
- Summary: Made README bootstrap instructions and GitHub standards references explicit for link-only agent installation.
- Tests: README/rules grep; agentctl check manual
- Completed-at: 2026-06-30 04:03:10
