# Code Standards

> Default rules below align with the `skill-patterns.md` engineering standard from
> `super_skill_team`. Each project should append language-specific rules once the
> tech stack is known.

## Default Rules

- Prefer the existing project style over new abstractions.
- Keep changes scoped to the active task's write scope.
- Add or update tests when behavior changes.
- Do not introduce new dependencies without documenting why.
- Do not commit generated artifacts unless the project explicitly tracks them.
- Never persist secrets, tokens, keys, cookies, or private credentials.

## Python Tooling (stdlib-only)

Applies to scripts under `tools/` and `scripts/`:

| Rule | Requirement |
|------|-------------|
| Dependencies | stdlib only (`argparse`, `json`, `os`, `re`, `sys`, `pathlib`, ...) |
| CLI | must support `--help` |
| JSON output | must support `--json` for machine consumers |
| Exit codes | `0` = ok, `1` = warning, `2` = hard error |
| Determinism | no LLM/API calls inside tools |
| Secrets | credentials come from env vars, never hardcoded |
| Main guard | must have `if __name__ == "__main__":` |

## Communication / Confidence Tagging

When an agent reports a finding or recommendation, tag confidence so vague words
("maybe", "probably") are replaced by an explicit level:

- 🟢 `verified` — backed by evidence (tests pass, logs, explicit code).
- 🟡 `medium` — judgment from experience / partial evidence.
- 🔴 `assumed` — assumption that still needs confirmation.

Output shape: bottom line first -> What (with confidence) -> Why -> How to act ->
options that need a human decision.

## Naming

- New files: `snake_case` unless the repository already uses another convention.
- Keep one name per concept; do not rename the same thing across the codebase.
