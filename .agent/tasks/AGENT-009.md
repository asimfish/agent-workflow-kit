# AGENT-009 - support adoption baseline for existing repositories

Status: done
Owner: codex
Agent: codex
Created: 2026-06-30 15:56:37
Updated: 2026-06-30 15:56:37

## Format Rules

- Keep this task doc factual and current; do not paste long reasoning transcripts.
- Preserve all top-level headings. Add subsections only under the existing headings.
- Update `Status:` through the workflow commands when possible.
- Use stable paths relative to the repo root.
- If a human edits this file, agents must re-read it and run `python3 tools/agentctl.py refresh` before continuing.

## Task Contract

- Goal: Allow Agent Workflow Kit to be adopted into existing repositories with non-compliant old history.
- Non-Goals: Do not weaken checks for new commits created after installation.
- Dependencies: Existing `agentctl init` and `pre-push` check behavior.
- Expected Deliverables: Installation-time adoption baseline, pre-push filtering after the baseline, and docs explaining the behavior.
- Definition of Done: A repo with old bad commits can install the kit, make a compliant new commit, and pass pre-push for the new adoption window.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `tools/, templates/, docs/, README.md, .agent/`
- Files likely to touch: `tools/agentctl.py`, templates/docs, this task doc.
- Files explicitly out of scope: unrelated runtime behavior and target-project source code.

## Stage Plan

Use one checkbox per stage. Do not delete completed stages; append changed or new stages with a short reason.

- [x] Stage 1: Add adoption baseline recording at init time.
- [x] Stage 2: Filter pre-push commit checks to baseline-and-newer commits.
- [x] Stage 3: Verify with a synthetic old-history repo and update docs.

## Stage Log
- 2026-06-30 15:59:13 Implemented and verified adoption baseline support for existing repositories with old non-compliant history.

Format: `- YYYY-MM-DD HH:MM:SS <short factual update>`.


## Verification

- Commands to run:
  - `python3 tools/agentctl.py check --mode manual`
- Synthetic old-history pre-push smoke
- Expected result:
  - New compliant commits pass pre-push even when older reachable commits predate workflow adoption.
- Result:
  - Passed. Synthetic repo had a bad old commit, adoption baseline at that commit, and a compliant new commit; `pre-push` over the full old-to-new range returned OK.

## Completion Record
- Summary: Added adoption baseline support so existing repositories only enforce pre-push rules on commits after workflow installation.
- Tests: py_compile; synthetic old-history pre-push smoke; agentctl check manual
- Completed-at: 2026-06-30 15:59:13
