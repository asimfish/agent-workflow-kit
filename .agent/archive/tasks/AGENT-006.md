# AGENT-006 - harden document and github templates

Status: done
Owner: codex
Agent: codex
Created: 2026-06-29 13:48:50
Updated: 2026-06-29 13:48:50

## Task Contract

- Goal: Harden plan/task/GitHub templates so agent-maintained documents stay consistent as task volume grows.
- Non-Goals: Do not change the lifecycle state machine or remove existing low-level commands.
- Dependencies: Existing templates, documentation standards, GitHub standards, and `agentctl task create` generation.
- Expected Deliverables: Stronger project plan template, stronger task template, clearer document maintenance rules, and generated task docs that start with structured placeholders.
- Definition of Done: A fresh installed project has enough template structure that agents can update plans, logs, verification, and GitHub submissions without inventing formats.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `tools/, templates/, docs/, README.md, AGENTS.md, .agent/`
- Files likely to touch: `templates/project/.agent/PROJECT_PLAN.md`, `templates/project/.agent/tasks/_template.md`, `.agent/rules/*.md`, `docs/document-maintenance.md`, `tools/agentctl.py`.
- Files explicitly out of scope: agent runtime architecture, non-template application code, secrets.

## Stage Plan

- [x] Stage 1: Audit existing templates and rules for ambiguity.
- [x] Stage 2: Strengthen templates and rules for plan edits, task records, and GitHub submissions.
- [x] Stage 3: Verify generated task docs and workflow checks.

## Stage Log
- 2026-06-29 13:54:06 Verified fresh project templates and generated task docs after receipt-order fix.
- 2026-06-29 13:53:05 Fixed start receipt ordering so generated task status updates do not immediately stale the session.
- 2026-06-29 13:52:17 Strengthened plan/task/GitHub templates and synchronized task doc status on work start.
- 2026-06-29 13:49:18 Audited existing plan/task/GitHub templates and identified missing format invariants.


## Verification

- Commands to run:
  - `python3 -m py_compile tools/agentctl.py`
  - Fresh project install and `task create` output inspection
  - `python3 tools/agentctl.py check --mode manual`
- Expected result:
  - Templates and generated task docs expose explicit update formats and pass workflow checks.
- Result: passed; generated task docs include `Format Rules`, structured logs, verification defaults, and `Status: in_progress` after `work`.

## Completion Record
- Summary: Hardened plan, task, documentation, GitHub, and PR templates; fixed start receipt ordering so generated task docs stay synchronized.
- Tests: py_compile; fresh-project install/task create/work/check smoke; agentctl check manual
- Completed-at: 2026-06-29 13:54:06
