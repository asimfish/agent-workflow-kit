# Plan Design

## Long-Term Plan Shape

A good long-term plan has:

- A stable goal.
- Milestones with exit criteria.
- A task board with owners and scopes.
- Dependency ordering.
- Verification gates.
- A change log.

## Task IDs

Use stable IDs:

- `T-001` for project work.
- `D-001` for data collection.
- `R-001` for research.
- `BUG-001` for defects.

Avoid renumbering after tasks are created. Rename titles instead.

## TODO Semantics

Use statuses consistently:

- `todo`: defined but not started.
- `in-progress`: active work exists.
- `blocked`: needs user input or external dependency.
- `review`: work done, waiting for review.
- `done`: verified and recorded.

## Plan Changes

If the plan changes while agents are working:

1. Update `.agent/PROJECT_PLAN.md`.
2. Update affected task docs.
3. Tell active agents to re-read and run `agentctl refresh`.
4. Avoid silently changing another agent's write scope.

