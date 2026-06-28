# Reference Notes

This kit was designed after reviewing `https://github.com/asimfish/super_skill_team.git` at commit `722c52d`.

Borrowed design ideas, expressed here as project-local tooling:

- Plan files should be executable by agents with zero prior context.
- Complex work should use persistent `task_plan`, `findings`, and `progress` style artifacts.
- Multi-agent work should choose an explicit team pattern such as Pipeline, Fan-out/Fan-in, Supervisor, or Hierarchical.
- Cross-agent integration needs concrete artifacts and verifiers, not prose-only instructions.
- Hooks are the enforcement layer for rules that agents tend to forget.

This repository intentionally implements a small project workflow layer rather than copying the full upstream skill library.

## Related Open Source Projects To Track

These projects are useful references. Do not copy code without a license review; absorb patterns and reimplement narrowly.

| Project | URL | Useful Pattern |
|---|---|---|
| planning-with-files | https://github.com/OthmanAdi/planning-with-files | `task_plan.md`, `findings.md`, `progress.md`; scoped plan directories; completion gate; recovery after context loss. |
| 1Password agent-hooks | https://github.com/1Password/agent-hooks | Cross-agent hook bundle layout; per-agent config paths; install script that never overwrites existing hook config. |
| agent-hooks | https://github.com/weykon/agent-hooks | Adapter matrix for Claude Code, Cursor, Codex, Windsurf, Kiro, OpenCode, Gemini; useful for future multi-agent hook registration. |
| claude-code-hooks-mastery | https://github.com/disler/claude-code-hooks-mastery | Self-validating prompts and hook-backed validators for plan/spec artifacts. |
| ClaudeForge | https://github.com/alirezarezvani/ClaudeForge | CLAUDE.md/AGENTS.md generation, sync, pruning, line-cap validation, and guardian hooks. |
| context-mode | https://github.com/mksglu/context-mode | Cursor `preToolUse`, `postToolUse`, and `stop` hooks for context injection and observation. |
| anywhere-agents | https://github.com/yzhao062/anywhere-agents | Shared config bootstrap pattern for keeping AGENTS.md, skills, and settings synced from a central repo. |
| NousResearch/hermes-agent | https://github.com/NousResearch/hermes-agent | Self-improving memory/skills, multi-surface gateway, scheduled automations, emerging multi-agent Kanban/orchestration model. |
| tonbistudio/hermes-multi-agent-workflow | https://github.com/tonbistudio/hermes-multi-agent-workflow | Config-driven Hermes pipeline skeleton with intake, dedup, scoring, parallel research, routing, human gate, fulfillment, and delivery. |
| heliogil/hermes-factory-showcase | https://github.com/heliogil/hermes-factory-showcase | Task packets, inbox/outbox, cron workers, race locks, ephemeral execution, closer/reviewer retry pattern. |

## Absorption Notes

- Add an explicit active-plan pointer if this kit grows beyond one `.agent/PROJECT_PLAN.md`.
- Keep hook installers non-destructive: create missing config, warn on existing config, and provide merge instructions.
- Preserve a universal `AGENTS.md` entry, but generate native hook/rule files per tool.
- Prefer deterministic validators over prompt-only policy when a failure would corrupt project state.
- Track agent support as a matrix because lifecycle event names differ across tools and versions.
- See `docs/hermes-design-learnings.md` for the Hermes-specific absorption roadmap.
