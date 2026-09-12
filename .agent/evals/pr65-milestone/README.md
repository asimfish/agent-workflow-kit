# Frozen PR65 Repair Evaluation

Owner: independent-reviewer-038-codex, acting as the user-authorized evaluator.
Preparation task: T6610CD0566A2EF36-002. Baseline:
221470284f1c99a9525821dd4f590e2f3ae7875c. Candidate: not supplied at freeze time.
This verifier was written without reading uncommitted worker fixes.

## Positive Oracles

Every case is required and expects exit 0 only for correct behavior. This suite
does not invoke or reinterpret the old defect probe. Baseline failure is negative
control evidence, never an accepted candidate score.

| Case | Split | Observable invariant |
|---|---|---|
| cr001-archived-child-cascade | held_in | Archived done child contributes to recursive parent closure, named completion records, plan boxes, flat counts and tree status; archive evidence stays unchanged |
| cr002-concurrent-parent-edges | held_in | Both merge orders preserve base and added edges; unfinished child prevents early closure; final record names every child; concurrent removal/reopen/closure and merged cycles signal conflict in both orders |
| heldout-live-state-precedence | held_out | Reopened live child takes precedence over stale archived done evidence; genuinely missing and empty milestones remain open; tree does not mask missing work |

The held_out storage-compatibility fixture is independently selected, not copied
from worker tests or used to implement the repair. The additional merge safety
checks express the conflict behavior announced by the user, not a specific
algorithm or exception message. No exact ordering of dependency sets is required.

## Execution Boundary

- The suite and runner live only in the reviewer's checkout, outside worker scope.
- The catalog embeds the SHA256 of runner.py in every argv; runner verifies its
  bytes before loading target code. The evaluator's suite hash therefore also
  binds the runner content. Freeze both together; do not tweak between targets.
- The target is cwd, never an embedded candidate path. Load only its committed
  tools/agentctl.py; disable bytecode writes. All fixture writes are in fresh
  TemporaryDirectory roots, removed on exit. The actual board CLI is read-only.
- The evaluator's reduced environment is preserved. No CODEX_THREAD_ID, session
  key or worker identity is synthesized, overridden or smuggled into the case.
- The fixtures seed explicit live/archive/done storage states and invoke the
  production merge dispatcher and shared done-path cascade. These are integration
  checks at persistence/domain boundaries, not fabricated gate approvals.
- This suite does not alone prove archive admission/authentication, full two-clone
  sync transport, or each done-path call site. Independent candidate review and
  regression/CLI checks remain required before any task gate.

## Signed Pair Protocol

1. Commit the policy and preparation ledger; retain that clean policy commit.
2. From that exact commit run:

   ```sh
   python3 tools/agentctl.py eval run pr65-milestone-repair --suite-file .agent/evals/pr65-milestone/suite.json --target /tmp/awk-pr65-codex-9eeJOt/baseline-2214702 --json
   ```

3. Record the returned run ID, suite hash, clean policy commit and target commit.
   Expected baseline: held_in 0/2, held_out 1/1; inspect assertion failures rather
   than treating timeouts, missing modules or harness errors as a valid red case.
4. Wait for an explicit committed candidate SHA. Do not inspect or evaluate the
   worker's unfinished checkout. Materialize a separate clean read-only candidate
   target only after that SHA is supplied.
5. Run candidate using the same policy commit, suite bytes, runner bytes and
   supervisor Git common directory (the HMAC key must not be copied or published).
   If ledger publication moved the review branch, pin this same checkout to the
   recorded policy commit temporarily with a clean tree, refresh the real session
   receipt if required, run eval, then restore fix/38-review-ledger-codex before
   recording further ledger changes. Do not inherit another conversation's task.
6. Only then compare signed reports. All three candidate cases must pass, and
   neither split may regress. Any policy change requires re-running both targets.
   The suite uses an absolute reviewer path intentionally; relocating it changes
   policy and requires a new baseline, not a silently edited report.

At preparation time neither eval gate nor task gate is authorized. Later approval
requires the full independent review, the unchanged signed pair and explicit
candidate-bound test evidence, not just aggregate metrics.

## Evidence Storage

Signed original reports stay under .agent/state/evals/runs/ in this review clone.
They are local HMAC-authenticated evidence, not portable attestations. Durable
run IDs, hashes, results and limitations belong in the review task/log; never
commit the signing key. Publish only reviewer-owned .agent artifacts on
fix/38-review-ledger-codex; no PR, merge, worker-source edit or candidate mutation.
