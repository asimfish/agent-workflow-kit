# Hermes Design Learnings

This note summarizes Hermes-style designs worth absorbing into Agent Workflow Kit.

## What Hermes Adds Beyond Our Current Kit

Our current kit is a repository-local governance layer:

- read plan before work,
- task docs as handoff artifacts,
- lifecycle hooks for Codex/Claude/Cursor,
- Git hooks and CI gates for commit discipline.

Hermes-style systems add a runtime layer:

- persistent agent profiles,
- task boards or queues,
- task packets,
- shared memory,
- human approval gates,
- cross-agent or cross-CLI routing,
- scheduled/background execution.

## References Reviewed

| Source | Relevant Design |
|---|---|
| NousResearch/hermes-agent | Self-improving agent with memory, skills, multi-surface gateway, scheduled automations. |
| NousResearch/hermes-agent issue #344 | True multi-agent definition: specialized roles, dependency-aware DAGs, shared context, retry/recovery, health checks. |
| NousResearch/hermes-agent issue #413 | Cross-CLI orchestration: wrap Claude Code, Codex, Gemini, Aider, etc. as workers; pass messages through PTY/headless transport. |
| NousResearch/hermes-agent issue #377 | Shared memory pools so downstream agents read upstream artifacts directly instead of forcing every handoff through a supervisor. |
| NousResearch/hermes-agent issue #514 | Agent registry and A2A-style interoperability: discover agents by capability and route tasks across machines/frameworks. |
| tonbistudio/hermes-multi-agent-workflow | Config-driven pipeline skeleton: sources -> intake -> dedup -> score -> parallel research -> route -> human gate -> fulfill -> deliver. |
| heliogil/hermes-factory-showcase | Factory loop: cron, inbox/outbox task packets, `flock` race protection, ephemeral Docker executor, closer/reviewer retry loop. |
| alchaincyf/hermes-agent-orange-book | Conceptual framing: self-improvement, three-layer memory, skill evolution, Kanban orchestration, OS-level boundary. |

## Better Designs To Absorb

### 1. Separate Generic Engine From Domain Config

The strongest pattern from `hermes-multi-agent-workflow` is "fat engine, thin skill": deterministic routing/scoring/state transitions belong in code; domain specifics belong in config and templates.

For this kit:

- keep `agentctl.py` generic,
- move project-specific task taxonomies into `.agent/workflow.json`,
- move agent capabilities into `.agent/agents.json`,
- keep rules and output formats in Markdown templates.

### 2. Add A Board, Not Only A Plan

`.agent/PROJECT_PLAN.md` is good for humans, but poor as a machine state source. Add a machine-readable board:

```text
.agent/board.json
```

Recommended states:

- `todo`
- `ready`
- `in_progress`
- `blocked`
- `review`
- `approved`
- `done`
- `failed`
- `shelved`

The Markdown plan remains the narrative source of truth; `board.json` becomes the runtime state receipt.

### 3. Introduce Task Packets

Hermes factory designs use inbox/outbox packets. This is useful when many agents work asynchronously.

Recommended shape:

```text
.agent/bus/
  inbox/<agent>/<task-id>.json
  outbox/<agent>/<task-id>.json
  done/<task-id>.json
  failed/<task-id>.json
```

Each packet should include:

- task ID,
- owner,
- input artifacts,
- allowed write scope,
- required output contract,
- dependency IDs,
- retry count,
- deadline or stale threshold.

### 4. Add Agent Profiles

Hermes discussions repeatedly point to named persistent specialists. We should add:

```text
.agent/agents.json
```

Each profile:

- name,
- role,
- allowed tools,
- model preference,
- write scopes,
- required rules,
- memory paths,
- output contract.

This lets a supervisor assign tasks by capability rather than hard-coded agent names.

### 5. Add Shared Memory With Boundaries

Shared memory is valuable, but unsafe if global. Use scoped pools:

```text
.agent/memory/project.md
.agent/memory/tasks/<task-id>.md
.agent/memory/agents/<agent>.md
.agent/memory/public/
.agent/memory/private/
```

Rules:

- Task memory is readable by downstream dependent tasks.
- Agent-private memory is not automatically shared.
- Secrets are never stored.
- Handoffs reference artifact paths instead of copying large content.

### 6. Add Human Gates Explicitly

For risky stages, a task should not advance from `review` to `approved` without a gate artifact:

```text
.agent/gates/<task-id>.md
```

Gate fields:

- decision: approve / reject / modify / shelve,
- approver,
- timestamp,
- required changes,
- artifacts reviewed.

This is better than treating `done` as a single undifferentiated endpoint.

### 7. Add Race Protection

Hermes factory patterns use lock discipline. For this kit:

- create `.agent/locks/<task-id>.lock`,
- use atomic lock acquisition for `start`,
- prevent two agents from owning the same active task,
- warn when write scopes overlap.

This should be deterministic and not rely on the model.

### 8. Add Cross-CLI Worker Adapter Later

The stronger Hermes direction is not one agent tool; it is a supervisor that can route work to Codex, Claude Code, Cursor, Gemini, etc. The right abstraction is:

```text
agentctl dispatch --task T-001 --profile coder-codex
agentctl dispatch --task T-002 --profile reviewer-claude
```

Do not build this before board/packet/profile state exists.

## Recommended Roadmap

1. Add `.agent/board.json` and make `agentctl` update it alongside Markdown.
2. Add `.agent/agents.json` profile registry.
3. Add `.agent/bus/` task packet inbox/outbox.
4. Add lock files and write-scope overlap checks.
5. Add human gate commands: `gate request`, `gate approve`, `gate reject`.
6. Add shared memory pools with explicit visibility.
7. Add dispatch adapters for Codex, Claude Code, and Cursor after the state model is stable.

## What Not To Copy

- Do not make the runtime depend on one Hermes installation.
- Do not make memory global by default.
- Do not route every handoff through a supervisor summary.
- Do not auto-approve risky fulfillment stages.
- Do not put domain logic into `agentctl.py`; keep it in config.

