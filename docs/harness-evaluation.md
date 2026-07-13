# Harness Evaluation

Harness changes need a verifier-grounded feedback loop. A passing unit test is
necessary, but it does not show that a new workflow fixes known failures without
damaging other tasks. The project eval layer compares one unchanged suite across
a baseline checkout and an isolated candidate checkout.

## Trust Boundary

- Run `agentctl eval` from the supervisor checkout.
- Keep the suite file outside the candidate write scope. It may be the project
  default `.agent/evals/suites.json` or a private absolute `--suite-file`.
- Pass the candidate worktree through `--target`; commands execute there, while
  reports and decisions are written in the supervisor checkout.
- Suite commands are argv arrays. The evaluator never invokes a shell.
- Verifier subprocesses receive a reduced environment without API keys or other
  arbitrary session variables.
- Run reports and decisions are signed with a supervisor-local HMAC key stored
  at `.git/agent-workflow/eval-hmac.key` with owner-only permissions where the
  platform supports them. `show`, `compare`, and `gate` reject edited evidence.
- This layer is not an OS sandbox. Run untrusted candidate code only inside the
  permissions and isolation boundary provided by the host agent runtime.

The repository owner can edit both evaluator and policy. For stronger
separation, run the supervisor from a protected checkout and use a suite file
that the candidate cannot write.

## Suite Schema

`.agent/evals/suites.json` is a versioned JSON catalog. Every suite must contain
at least one `held_in` and one `held_out` case:

```json
{
  "version": 1,
  "suites": {
    "workflow-integrity": {
      "description": "Deterministic workflow checks",
      "cases": [
        {
          "id": "known-regression",
          "split": "held_in",
          "required": true,
          "argv": ["python3", "tools/agentctl.py", "check", "--mode", "manual"],
          "timeout_seconds": 120,
          "expected_exit_codes": [0],
          "artifacts": []
        }
      ]
    }
  }
}
```

`held_in` cases prove that a proposed change fixes known weaknesses. `held_out`
cases protect behavior not used to design the change. `required` failures always
reject a candidate even when its aggregate score does not regress. Artifact
paths must be relative to the target checkout.

## Supervisor Flow

Use one policy checkout and two clean targets:

```bash
agentctl eval list
agentctl eval run workflow-integrity --target <baseline-worktree> --json
agentctl eval run workflow-integrity --target <candidate-worktree> --json
agentctl eval compare --baseline <baseline-report> --candidate <candidate-report>
agentctl eval gate --baseline <baseline-report> --candidate <candidate-report> \
  --by <reviewer>
```

`run` persists full case evidence under `.agent/state/evals/runs/`: suite hash, policy
commit, target commit and dirty state, bounded stdout/stderr, timeout, artifacts,
and split metrics. `gate` writes accept and reject decisions under
`.agent/state/evals/decisions/`; failed proposals remain locally visible instead of being
discarded from history. Reports are bound to the local supervisor signing key;
copying JSON to another clone does not make it trusted there. `.agent/state/` is
Git-ignored because this evidence is machine-local; durable review facts belong
in the task completion record.

Acceptance requires:

- both reports use the current suite hash and one clean policy commit;
- report case evidence and recomputed metrics agree;
- baseline and candidate remain on one clean commit through the full evaluation;
- candidate `held_in` and `held_out` scores are each at least the baseline score;
- every required candidate case passes.

This is the evaluation foundation for later memory curation and bounded harness
proposal loops. It does not automatically edit, accept, merge, or schedule a
harness change.
