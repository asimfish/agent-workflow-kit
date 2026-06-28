# GitHub Standards

> Aligned with the `auto_git` standard from `super_skill_team`, plus this kit's
> task-driven gate. The git hooks enforce a machine-checkable subset of these rules.

## Iron Law

- Never `git push --force` / `--force-with-lease` to `main`, `master`, `develop`,
  or shared `release/*` branches without explicit written owner approval.
- Every commit must be atomic and reversible: it should make sense on its own in
  `git bisect`.
- Never commit secrets, tokens, keys, cookies, or private credentials.

## Branches

Use descriptive, lowercase, hyphenated branches that carry the task ID:

- `feature/T-001-short-name` — new capability
- `bugfix/T-002-short-name` — defect fix
- `hotfix/<version>` — urgent production patch
- `release/<version>` — release preparation
- `chore/T-003-short-name` — tooling, CI, deps (no product behavior change)

Rules:

- Lowercase, hyphens, no spaces; one branch per task.
- Do not work directly on `main` unless the project owner explicitly allows it.
- For parallel agents, give each agent a dedicated branch or git worktree to avoid
  index/HEAD contention (see `agent-operating-rules.md`).

## Commits (Conventional Commits + atomic)

Format:

```text
<type>(<scope>): <short imperative summary>

[optional body: explain why, not what]

Refs: T-001
```

Allowed types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`, `build`.

Atomic commit checklist:

- [ ] Staging is intentional (use `git add -p` when changes are mixed).
- [ ] Tests/docs that belong to the change are updated in the **same** commit.
- [ ] No unrelated formatting sweeps bundled with the change.
- [ ] Include a task ID in every commit title or body.
- [ ] Keep each commit scoped to one task.
- [ ] Stage the relevant task doc or progress log when staging code/data changes.

## Safety Rules (non-negotiable)

- Before `reset --hard`, `clean -fd`, `branch -D`, `push --delete`, or any
  force-push: show the impact (`git log`, affected branches) and confirm.
- `pre-commit` must scan staged content for secrets (private keys, tokens).
- If a remote URL or credential looks suspicious, stop and ask — do not exfiltrate.

## Pull Requests

Every PR body must include:

```markdown
## Summary
-

## Task
Refs: T-001

## Changes
- User-visible or architectural changes

## Verification
- [ ] command:

## Risk / Rollout
- Feature flags, migrations, backwards compatibility

## Agent Notes
- Files touched:
- Risks:
- Follow-ups:
```

Do not create or merge PRs with failing verification. Require review (human or
reviewer agent) before merging to a protected branch.

## Push Gate

The installed `pre-push` hook checks commits before they leave the machine:

- Every pushed commit must use Conventional Commits.
- Every pushed commit must include a task ID.
- Every pushed task must have a task document.
- A task must be `review` or `done` before push.
- A `done` task must have a filled Completion Record.
- A `done` task must be checked off in `.agent/PROJECT_PLAN.md`.

To share incomplete work, mark the task `review` only when the task document
records what remains.
