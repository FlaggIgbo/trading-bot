# [ADR-XXXX] Title of Architectural Decision

## Metadata
- **Status**: [ Proposed | Accepted | Rejected | Deprecated | Superseded by ADR-XXXX ]
- **Date**: YYYY-MM-DD
- **Deciders**: [Names / Handles]
- **Consulted**: [Risk / Trading Strategy Reviewers]
- **Informed**: [Team Members]

---

## Context & Problem Statement
<!-- 
Describe the problem statement, trading/execution dynamics, exchange API characteristics, or database constraints.
For Trading Bot: consider SQLite WAL concurrency, order state machines, slippage, reconciliation loops, broker abstraction, etc.
-->
[TODO: Explain the context and problem statement]

## Decision Drivers
<!-- 
Key drivers influencing this choice (e.g., zero double-fills, sub-100ms execution latency, kill-switch safety, crash-recovery guarantees).
-->
- [TODO: Driver 1, e.g., Guarantee zero duplicate orders on network timeout (idempotency)]
- [TODO: Driver 2, e.g., Execution latency under 150ms from signal to exchange order acknowledgment]
- [TODO: Driver 3, e.g., Resilient SQLite WAL concurrent reads during background live monitor runs]

## Considered Options
<!-- 
List alternative options evaluated.
-->
- **Option 1**: [Description, pros, cons]
- **Option 2**: [Description, pros, cons]
- **Option 3**: [Description, pros, cons]

## Decision Outcome
Chosen Option: **[Option X]**, because:
[TODO: Succinct rationale explaining the trade-offs, safety considerations, and risk constraints]

## Consequences
<!-- 
What becomes easier or harder as a result of this decision?
-->
### Positive Impacts
- [TODO: e.g., Strict state machine guarantees deterministic order settlement]
- [TODO: e.g., Reconciler automatically recovers state after process crashes]

### Negative Impacts / Trade-offs
- [TODO: e.g., Requires explicit WAL checkpointing before heavy analytics queries]

### Risk & Safety Safeguards
- [TODO: e.g., Kill-switch condition or maximum drawdown threshold enforced]

## Validation & Benchmarks
<!-- 
How is this validated? (e.g. backtests, k6 stress testing, simulated broker runs, live paper-trading)
-->
- [TODO: Validation criteria, e.g., 10,000 backtested cycles with zero unhandled exceptions]

## References & Related Artifacts
- [TODO: Links to commits, backtest runs, Kalshi API documentation, or reports]
