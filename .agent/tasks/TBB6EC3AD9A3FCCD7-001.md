# TBB6EC3AD9A3FCCD7-001 - Independent sync safety review exact 2fc8ac4 and unchanged eval

Status: in_progress
Owner: independent-sync-reviewer-5a24a0be
Agent: independent-sync-reviewer-5a24a0be
Created: 2026-10-10 12:29:54
Updated: 2026-10-10 12:29:54

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

- Goal: Independently accept the frozen first-sync and Windows sidecar contracts without source changes or identity overrides.
- Non-Goals: No source, tests, docs, policy or suite edits; no push, parent-checkout mutation or remote CI attestation.
- Dependencies: Source TC86DDDF6B83D255E-001 and CI T64B983E7A6C13C25-001 are frozen at 2fc8ac476a598d3a4b3aa8ff1d973adaab59d03c.
- Expected Deliverables: Reviewer registration, task, signed-eval references and two controller-generated gates; ledger-only fast-forward-compatible commits.
- Definition of Done: Exact 2fc8ac476a598d3a4b3aa8ff1d973adaab59d03c has no reproducible material review findings; independent plan-note probes pass; unchanged workflow-integrity clean baseline 6282337 and clean candidate evals have an accepted signed decision; both worker tasks receive genuine independent rerun-tests gates; only reviewer/controller .agent ledger is committed and native exact-head Linux and Windows CI remain explicitly pending.

## Context To Read Before Starting

- `AGENTS.md`
- `.agent/PROJECT_PLAN.md`
- `.agent/TASKS.md`
- `.agent/rules/agent-operating-rules.md`
- `.agent/rules/github-standards.md`

## Work Scope

- Allowed write scope: `.agent/gates/`
- Files likely to touch: Own task doc and .agent/gates/; controller-generated registration, board, index, plan checklist, progress and checkpoint records.
- Files explicitly out of scope: tools/, tests/, docs/, templates/, .github/, suite definitions, rule policies, parent and CI worker worktrees.

## Stage Plan

Use one checkbox per stage. Do not delete completed stages; append changed or new stages with a short reason.

- [x] Stage 1: Review exact source and both worker contracts; verify genuine runtime independence and independent plan-note boundary probes.
- [x] Stage 2: Commit clean reviewer policy ledger; run unchanged workflow-integrity on clean baseline and candidate and accept signed comparison.
- [ ] Stage 3: Gate both workers with their recorded rerun-tests contracts; finish own task and commit only review ledger with remote CI explicitly pending.

## Stage Log
- 2026-10-10 12:44:18 LEDGER HANDOFF ONLY; review remains in_progress, no gates and no finish. Completed read-only review of2fc8ac476a598d3a4b3aa8ff1d973adaab59d03c covered branch-local publication/refusals, autostash conflict retention, authority/receipt revalidation, TaskBoard prose preservation/conflicts and Windows sidecar boundaries. Independent plan-note probes passed4/4 in12.588s including real installed-project stale-note denial before push and successful refresh retry. Encoding audit then reproduced accepted P2 at tools/agentctl.py:701/714: native UTF8 locale2/2 pass, actual ASCII locale2/2 UnicodeDecodeError, actual Latin1 locale2/2 silent-corruption failures; script /private/tmp/awk-peer-sync-review-20261010/non-utf8-plan-probe.py and raw chinese-plan-native-utf8-2fc8ac4.log, chinese-plan-default-ascii-2fc8ac4.log, chinese-plan-default-latin1-2fc8ac4.log in the same directory. User invalidated2fc8ac4; earlier no-finding statement and accepted eval-decision-8b86bf700d6b do NOT authorize this or any new candidate. Old runs eval-workflow-integrity-7974eccb423d and eval-workflow-integrity-117aa52479c7 remain signed historical evidence under ignored .agent/state/evals/, not reusable final acceptance; raw state/key never enters Git. Preserve original84c4fd0b32395d79588e8c7db5d8802fd066d750 policy ancestry without rebase or cherry-pick. Source gate canceled before decision and all owned processes reaped; both worker gates absent. Parent reports minimal UTF8 fix RED-to-GREEN and62-case finish underway; reviewer has not revalidated that source yet. Parent will merge this clean ledger with committed repair, then reviewer fast-forwards and reruns unchanged clean baseline6282337/newcandidate evals, actual ASCII/Latin1 probes and both formal contracts. Native exact-head Linux/Windows CI remains pending. No source/CI/parent-worktree edits or push.
- 2026-10-10 12:37:31 NEW P2 blocker discovered during user-requested encoding audit: _git_merge_file writes UTF8 but subprocess text=True omits encoding (line701), now used for TaskBoard human prose (line714). Actual locale subprocess probes on exact2fc8ac4: UTF8 mode0/native codec passes2/2; LC_ALL=C with PYTHONUTF8=0/PYTHONCOERCECLOCALE=0 raises UnicodeDecodeError for both Chinese note tests; ISO8859-1 locale silently returns mangled notes with conflict=false and fails2/2. Windows cp1252 decoder boundary separately raises UnicodeDecodeError; native Windows runner locale/mode not yet observed. Probe and raw logs under /private/tmp/awk-peer-sync-review-20261010/non-utf8-plan-probe.py and chinese-plan-*-2fc8ac4.log. Stopped source gate before approval, terminated/reaped owned gate processes; both workers remain review and neither gate file exists. Policy startup84c4fd0 and signed eval decision8b86bf700d6b are old-head evidence only, not final acceptance. Minimal repair is explicit encoding=utf-8 in _git_merge_file plus non-UTF8 default regression; await new frozen candidate before gates.
- 2026-10-10 12:31:30 Clean registration-policy commit84c4fd0b32395d79588e8c7db5d8802fd066d750. Unmodified workflow-integrity suite hash1cc914f29a7bb136bf9f773402449fcb2c30f6bfda24f222a57780421587ae04: baseline eval-workflow-integrity-7974eccb423d and candidate eval-workflow-integrity-117aa52479c7 each passed2/2 with clean unchanged target commits and clean identical policy. Signed decision eval-decision-8b86bf700d6b accepted=true. Raw signed JSON stays ignored under .agent/state/evals/runs/ and decisions/; no state/key copied into Git. Formal task gates rerun next; remote CI remains pending.
- 2026-10-10 12:30:36 Reviewed exact 2fc8ac476a598d3a4b3aa8ff1d973adaab59d03c against baseline 6282337 and both DoDs. No concrete material finding remains. Independent disposable plan-note probes pass4/4 in12.588s: remote-only board instruction preserves text, denies stale-receipt push with origin unchanged, refresh retry succeeds; local-only note plus peer rows preserves own receipt; competing notes conflict. Actual reviewer host-runtime:5a24a0beb5ee1765af5f998cca719dc0 differs from source8188dc0a and CI209a1e44. No source writes or push; exact-head Linux/native Windows CI pending.

Format: `- YYYY-MM-DD HH:MM:SS <short factual update>`.


## Verification

- Tests command: python3 -m unittest tests.test_ci_workflow tests.test_read_receipt -v
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
