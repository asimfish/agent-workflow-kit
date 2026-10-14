# TB4CDAE5DCCCABB66-002 - Publish native390e1d5 re-review rejection and eval ledger

Status: in_progress
Owner: codex-native-retirement-review-z9zb1r
Agent: codex-native-retirement-review-z9zb1r
Created: 2026-10-14 12:48:21
Updated: 2026-10-14 12:48:21

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

- Goal: Commit only controller-generated native re-review and signed eval/probe evidence for parent scope repair
- Non-Goals:
- Dependencies:
- Expected Deliverables:
- Definition of Done: Normal-hook generated-record-only commit references done reviewTB4CDAE5DCCCABB66-001, isolated GitNexus staged detection finds no source-symbol changes, compatibility approval and policy remain unchanged, and own session is released without push

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `.agent/gates/, .agent/bus/, .agent/handoffs/`
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

- Tests command: python3 tools/agentctl.py check --mode manual
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
