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

- Goal: Independently re-accept repaired Windows receipt-path source7548d3a6e8a6c4672826d9d8d4b4d54f9e10d300 after exact01b native Windows failures, preserving unchanged claim/receipt semantics and CI scope.
- Non-Goals: No source, tests, docs, policy or suite edits; no push, parent-checkout mutation or remote CI attestation.
- Dependencies: Source TC86DDDF6B83D255E-001 is refrozen at 7548d3a6e8a6c4672826d9d8d4b4d54f9e10d300 after exact01b native Windows failures. CI T64B983E7A6C13C25-001 bytes and original configuration approval are unchanged; a new source gate/eval and exact-head native CI are required.
- Expected Deliverables: Clean new reviewer-policy commit, new signed-eval references, fresh source161 gate, retained CI-scope verification, and ledger-only fast-forward-compatible final commit.
- Definition of Done: Exact source7548d3a6e8a6c4672826d9d8d4b4d54f9e10d300 has no reproducible scoped material blockers; new clean committed reviewer policy owns unchanged workflow-integrity evals of clean baseline6282337 and clean candidate7548 with newly accepted signed decision; plan probes4, actual UTF8/ASCII/Latin1 Chinese probes2 each and WindowsPurePath3 pass; source recorded161-case contract receives a fresh genuine independent rerun-tests gate with AGENT_WORKFLOW_TESTS_TIMEOUT=1200; unchanged CI bytes and retained CI gate are verified with manual/config7 checks; own8-case contract finishes and only .agent ledger is committed without push. Previous609/01b source approvals are superseded, old native failure38025945186/job114136699583 retained, and new exact-head NativeWindows31/Linux398 CI remain required before merge.

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
- [x] Stage 3: Independently rerun and approve both worker contracts; record final local acceptance and the remaining exact-head remote CI requirement for ledger handoff.
- [x] Stage 4: Re-enter the real reviewer session after fast-forward to unified 609; independently review the minimal UTF-8 repair and native-locale regression.
- [x] Stage 5: Repeat the unchanged baseline/candidate evaluation under clean policy 609 and accept a new signed decision; supersede old 2fc authorization.
- [x] Stage 6: Reopen the genuine reviewer at7548 after native01b Windows failure; render and refresh canonical views, inspect the one-line path fix, tests and explicit legacy-receipt compatibility.
- [ ] Stage 7: Commit new clean review policy and rerun the unchanged suite against clean628 baseline and clean7548 candidate; accept a new signed decision.
- [ ] Stage 8: Repeat plan4, Chinese codec2 each, Windows-path3 and CI manual/config7 checks; freshly gate source161 with1200s timeout, retaining only unchanged CI configuration approval.
- [ ] Stage 9: Finish own8-case contract and prepare ledger-only FF-compatible handoff with new exact-head native CI still required.

## Stage Log
- 2026-10-10 13:25:17 Reopened real TBB reviewer after FF-only7548d3a6e8a6c4672826d9d8d4b4d54f9e10d300. Controller reconcile render and reread/refresh corrected reopened done-checkbox; no manual shared metadata edits. Exact01b native Windows failure38025945186/job114136699583 retained;609/01b source approval and eval superseded. Static one-line _hash_docs as_posix fix, three path/legacy tests and compatibility doc reviewed; _receipt_view/claim checks and CI configuration bytes unchanged. Actual runtime5a24a0be distinct from workers. New clean reviewer policy commit before new unchanged baseline628/candidate7548 eval; formal source161 rerun will use1200s timeout. New NativeWindows31/Linux398 CI still mandatory before merge; no source edits or push.
- 2026-10-10 12:57:07 NEW exact609 independent acceptance complete: source formal rerun62/62 in250.858s and CI recorded manual rerun0.5s both approved to done with actual distinct reviewer runtime5a24a0be. Config7, independent plan4, real UTF8/ASCII/Latin1 Chinese2/2 each and new clean-policy609 signed decision4ee366f197a9 all pass. Own stages and verification now factual and complete; no scoped blockers, no source/CI edits or push. Old2fc eval authorization superseded. Finish own recorded CI+receipt8-case command then ledger-only normal commit; native exact-head remote Linux/Windows CI remains mandatory before merge.
- 2026-10-10 12:50:03 NEW609 acceptance evidence, not reuse of2fc: fast-forward-only kept84c4 policy history and source/CI bytes identical to17f0d4e. Real reviewer runtime5a24a0be remains independent of worker8188dc0a and CI209a1e44. Clean policy609 unchanged-suite baseline eval-workflow-integrity-e86c4e62f152 and candidate eval-workflow-integrity-49faac94a04e passed2/2 each; new signed decision eval-decision-4ee366f197a9 accepted=true. Only self-generated resume ledger was temporarily stashed to retain exact clean policy609, then restored; canonical claim and registry remained intact. New exact609 independent plan probes4/4 in13.997s; real native UTF8/ASCII/Latin1 Chinese probes2/2 each (utf8_mode=0 throughout), original error and silent corruption repaired. Raw signed JSON remains ignored .agent/state/evals/; raw probe/config logs in /private/tmp/awk-peer-sync-review-20261010/ with6090626 suffix. Own contract now explicitly targets609. Formal source62 rerun follows; native exact-head remote Linux/Windows CI pending publication, no push.
- 2026-10-10 12:44:18 LEDGER HANDOFF ONLY; review remains in_progress, no gates and no finish. Completed read-only review of2fc8ac476a598d3a4b3aa8ff1d973adaab59d03c covered branch-local publication/refusals, autostash conflict retention, authority/receipt revalidation, TaskBoard prose preservation/conflicts and Windows sidecar boundaries. Independent plan-note probes passed4/4 in12.588s including real installed-project stale-note denial before push and successful refresh retry. Encoding audit then reproduced accepted P2 at tools/agentctl.py:701/714: native UTF8 locale2/2 pass, actual ASCII locale2/2 UnicodeDecodeError, actual Latin1 locale2/2 silent-corruption failures; script /private/tmp/awk-peer-sync-review-20261010/non-utf8-plan-probe.py and raw chinese-plan-native-utf8-2fc8ac4.log, chinese-plan-default-ascii-2fc8ac4.log, chinese-plan-default-latin1-2fc8ac4.log in the same directory. User invalidated2fc8ac4; earlier no-finding statement and accepted eval-decision-8b86bf700d6b do NOT authorize this or any new candidate. Old runs eval-workflow-integrity-7974eccb423d and eval-workflow-integrity-117aa52479c7 remain signed historical evidence under ignored .agent/state/evals/, not reusable final acceptance; raw state/key never enters Git. Preserve original84c4fd0b32395d79588e8c7db5d8802fd066d750 policy ancestry without rebase or cherry-pick. Source gate canceled before decision and all owned processes reaped; both worker gates absent. Parent reports minimal UTF8 fix RED-to-GREEN and62-case finish underway; reviewer has not revalidated that source yet. Parent will merge this clean ledger with committed repair, then reviewer fast-forwards and reruns unchanged clean baseline6282337/newcandidate evals, actual ASCII/Latin1 probes and both formal contracts. Native exact-head Linux/Windows CI remains pending. No source/CI/parent-worktree edits or push.
- 2026-10-10 12:37:31 NEW P2 blocker discovered during user-requested encoding audit: _git_merge_file writes UTF8 but subprocess text=True omits encoding (line701), now used for TaskBoard human prose (line714). Actual locale subprocess probes on exact2fc8ac4: UTF8 mode0/native codec passes2/2; LC_ALL=C with PYTHONUTF8=0/PYTHONCOERCECLOCALE=0 raises UnicodeDecodeError for both Chinese note tests; ISO8859-1 locale silently returns mangled notes with conflict=false and fails2/2. Windows cp1252 decoder boundary separately raises UnicodeDecodeError; native Windows runner locale/mode not yet observed. Probe and raw logs under /private/tmp/awk-peer-sync-review-20261010/non-utf8-plan-probe.py and chinese-plan-*-2fc8ac4.log. Stopped source gate before approval, terminated/reaped owned gate processes; both workers remain review and neither gate file exists. Policy startup84c4fd0 and signed eval decision8b86bf700d6b are old-head evidence only, not final acceptance. Minimal repair is explicit encoding=utf-8 in _git_merge_file plus non-UTF8 default regression; await new frozen candidate before gates.
- 2026-10-10 12:31:30 Clean registration-policy commit84c4fd0b32395d79588e8c7db5d8802fd066d750. Unmodified workflow-integrity suite hash1cc914f29a7bb136bf9f773402449fcb2c30f6bfda24f222a57780421587ae04: baseline eval-workflow-integrity-7974eccb423d and candidate eval-workflow-integrity-117aa52479c7 each passed2/2 with clean unchanged target commits and clean identical policy. Signed decision eval-decision-8b86bf700d6b accepted=true. Raw signed JSON stays ignored under .agent/state/evals/runs/ and decisions/; no state/key copied into Git. Formal task gates rerun next; remote CI remains pending.
- 2026-10-10 12:30:36 Reviewed exact 2fc8ac476a598d3a4b3aa8ff1d973adaab59d03c against baseline 6282337 and both DoDs. No concrete material finding remains. Independent disposable plan-note probes pass4/4 in12.588s: remote-only board instruction preserves text, denies stale-receipt push with origin unchanged, refresh retry succeeds; local-only note plus peer rows preserves own receipt; competing notes conflict. Actual reviewer host-runtime:5a24a0beb5ee1765af5f998cca719dc0 differs from source8188dc0a and CI209a1e44. No source writes or push; exact-head Linux/native Windows CI pending.

Format: `- YYYY-MM-DD HH:MM:SS <short factual update>`.


## Verification

- Tests command: python3 -m unittest tests.test_ci_workflow tests.test_read_receipt -v

### Current 7548 acceptance

- Candidate: `7548d3a6e8a6c4672826d9d8d4b4d54f9e10d300`; clean baseline: `628233753573bf0c5ffee718982f26f39399c1b3`.
- Historical native failure: Exact `01b06cb` Windows run `38025945186`, job `114136699583`: 25/28 sync cases passed, three peer-receipt cases failed (milestone children, unrelated autostash, roundtrip). Old609/01b local source approval and eval authorization are not reused for7548.
- Static repair review: Only production change since01b is `_hash_docs` repository-relative key serialization from `str(...)` to `.as_posix()`. Semantic `_receipt_view`, claim/receipt checks and refresh authority remain unchanged. Windows path APIs now reach the existing task-scoped filters; own rows and human instructions still invalidate receipts. Old backslash-key receipts fail comparison until an explicit authorized refresh; no silent normalization.
- Coverage read: Three Windows-path API regressions cover canonical keys/peer-only rows, own rows/human direction, and legacy receipt refusal without mutation. Native failed cases remain enabled, with no skip or verifier change. Recorded source contract expanded to161 cases across affected shared callers.
- Runtime: Actual reviewer `host-runtime:5a24a0beb5ee1765af5f998cca719dc0` remains distinct from source worker `host-runtime:8188dc0a82f9eda0bd147ed3c18c35eb` and CI worker `host-runtime:209a1e440f9461eb1bcb21a9df63f704`; no identity override.
- New policy/eval/probes/source161 acceptance: Pending execution; old results below are historical, not new evidence. Source161 gate will use `AGENT_WORKFLOW_TESTS_TIMEOUT=1200`, as authorized.
- CI `.github/`, templates, rule policy, unchanged eval suite and CI gate bytes match01b. Preserve original CI configuration approval only; rerun manual and configuration7 checks. This does not attest native Windows execution of the repaired source.
- Merge condition: New exact-head Native Windows31 and Linux398 must pass. PR67 remains unmerged; no push, parent-worktree/source/test/doc/CI edit, policy weakening or raw eval/key commit.

### Superseded 609 local acceptance

- Reviewed candidate: `6090626ca8df203f8f372f380bd8952e54d15462`; baseline: `628233753573bf0c5ffee718982f26f39399c1b3`. Source/tools/tests/docs/CI bytes match repaired source `17f0d4e`; no reviewer source edits.
- Verdict: No reproducible material blockers in the scoped local review. Conditional pass for merge: exact-head remote Linux and native Windows CI remain required and are not attested here. Old `2fc8ac4` acceptance is superseded.
- Dimensions: Functional achievement and correctness/reliability pass with installed-project contracts and independent probes; architecture, function/API design, and maintainability pass with the narrow branch-local sync/prose/encoding diff and preserved receipt/claim checks. Performance has no acceptance target, claim, or measured regression signal in this change and is not a merge-relevant uncertainty.
- Runtime independence: Reviewer `host-runtime:5a24a0beb5ee1765af5f998cca719dc0`; source worker `host-runtime:8188dc0a82f9eda0bd147ed3c18c35eb`; CI worker `host-runtime:209a1e440f9461eb1bcb21a9df63f704`. Both controller-generated gates record these distinct actual runtimes.
- Source formal gate: `gate approve --task TC86DDDF6B83D255E-001 --rerun-tests` passed 62/62 in 250.858s (controller 251.1s), approved to done. CI formal gate: `gate approve --task T64B983E7A6C13C25-001 --rerun-tests` reran manual checks successfully in 0.5s, approved to done. Independent CI configuration tests passed 7/7 in 2.360s.
- Independent human-plan probes: 4/4 in 13.997s, including one-sided instructions preserved, peer rows not invalidating own receipt, explicit competing-note conflict, and real sync denial before push followed by reread/refresh success. `_receipt_view`, `_check_receipt`, and `_session_claim_error` remain unchanged from baseline.
- Chinese-note probes: Actual native UTF8, US-ASCII, and ISO8859-1 interpreter locales each passed 2/2 with `utf8_mode=0`; both round-trip and remote-only note/receipt assertions pass. These repair the previously reproduced exception and silent corruption; they do not substitute for native Windows CI.
- New unchanged-suite acceptance: Clean policy `6090626ca8df203f8f372f380bd8952e54d15462`, clean baseline/candidate targets before and after, suite hash `1cc914f29a7bb136bf9f773402449fcb2c30f6bfda24f222a57780421587ae04`. Baseline `eval-workflow-integrity-e86c4e62f152` and candidate `eval-workflow-integrity-49faac94a04e` passed 2/2 each; signed decision `eval-decision-4ee366f197a9` accepted=true, reasons empty.
- Raw signed evidence remains ignored in reviewer clone `.agent/state/evals/runs/{eval-workflow-integrity-e86c4e62f152,eval-workflow-integrity-49faac94a04e}.json` and `.agent/state/evals/decisions/eval-decision-4ee366f197a9.json`; no raw eval state or private key is committed.
- Raw probe/gate/config logs: `/private/tmp/awk-peer-sync-review-20261010/`, files `plan-note-probes-6090626.log`, `chinese-plan-{native-utf8,default-ascii,default-latin1}-6090626.log`, `source-gate-6090626.log`, `ci-gate-6090626.log`, and `ci-config7-6090626.log`. External probe scripts: `final-plan-note-probes.py` and `non-utf8-plan-probe.py` in that directory.
- No non-blocking improvement or further source repair requested. No full 394-case rerun, remote publication, policy/suite change, parent-worktree edit, or push performed. Final delivery is `.agent` ledger only on preserved `84c4fd0` policy ancestry.
- Commands to run:
  - `python3 tools/agentctl.py check --mode manual`
- Expected result:
  - The tests command exits 0 (`finish` runs it and records the result; the
    reviewer reruns it with `gate approve --rerun-tests`), workflow checks pass,
    and the Definition of Done holds.

## Completion Record

- Summary: Completed genuine independent review of repaired unified candidate6090626 against6282337. New unchanged clean-policy eval accepted; independent prose and actual UTF8/ASCII/Latin1 probes pass; source62 and CI manual formal gates approved with distinct actual runtime. Reviewer ledger only; old2fc approval superseded, exact-head remote Linux/native Windows CI required before merge.
- Tests: Source rerun62/62 in250.858s; CI gate manual exit0 in0.5s and config7/7 in2.360s; independent plan4/4 in13.997s; real UTF8/ASCII/Latin1 Chinese2/2 each with utf8_mode0; clean baseline/candidate eval2/2 each, new accepted signed decision eval-decision-4ee366f197a9 under clean policy609. Own recorded CI+receipt command reruns now. Raw proofs in /private/tmp/awk-peer-sync-review-20261010/ and ignored local .agent/state/evals; no keys/raw eval state in Git, source edits or push. Native remote CI pending.
- Tests-command: python3 -m unittest tests.test_ci_workflow tests.test_read_receipt -v
- Tests-exit: 0
- Tests-duration: 6.9s
- Worker-runtimes: host-runtime:5a24a0beb5ee1765af5f998cca719dc0
- Completed-at: 2026-10-10 12:57:21
- Completed-at-ns: 1791608241123540000
