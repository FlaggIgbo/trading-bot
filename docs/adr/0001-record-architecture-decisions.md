# 0001. Record Architecture Decisions

## Metadata
- **Status**: Accepted
- **Date**: 2026-11-24
- **Deciders**: Engineering & Quantitative Team
- **Consulted**: Core Contributors
- **Informed**: All Team Members

---

## Context & Problem Statement
The trading bot automates scanning, candidate ranking, risk engine evaluations, order routing, and settlement against binary prediction markets. Because financial risk and automated capital allocation are involved, all significant architectural changes (concurrency models, database isolation, state-machine transitions, circuit breakers) must be formally documented with explicit trade-offs and safety invariants.

## Decision Drivers
- High risk of regressions in automated order execution.
- Need for explicit documentation of safety mechanisms (kill switches, max position limits, idempotency keys).
- Version-controlled design transparency.

## Considered Options
- **Option 1**: Architecture Decision Records (ADRs) kept in `docs/adr/`.
- **Option 2**: Code comments and docstrings only.
- **Option 3**: External wiki.

## Decision Outcome
Chosen Option: **Option 1 (ADRs in `docs/adr/`)**, because having ADRs committed directly with code guarantees that changes to execution architecture or risk rules are audited and reviewed in version control.

## Consequences
### Positive Impacts
- Auditable trail of design trade-offs, edge-case protections, and risk rules.
- Clarifies invariants (e.g., write intent before dispatching orders, single worker process).

### Negative Impacts / Trade-offs
- Requires documenting every non-trivial architecture or risk design shift before rollout.
