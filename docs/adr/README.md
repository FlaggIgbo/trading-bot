# Architecture Decision Records (ADRs)

This directory documents key architectural, risk-management, and algorithmic design decisions for the **Prediction Market Trading Bot**.

## Decision Log

| Number | Date | Title | Status |
|--------|------|-------|--------|
| [0001](./0001-record-architecture-decisions.md) | 2026-11-24 | Record Architecture Decisions | Accepted |
| *0002* | *YYYY-MM-DD* | *[Proposed Decision Title]* | *Proposed* |

## ADR Lifecycle

- **Proposed**: Under discussion, backtesting, or paper-trading validation.
- **Accepted**: Decision approved for live bot execution.
- **Rejected**: Evaluated through simulations or safety review and rejected.
- **Superseded**: Replaced by a subsequent decision (link to superseding ADR).
- **Deprecated**: Strategy, broker interface, or model retired.

## Creating a New ADR

1. Copy [`template.md`](./template.md) to `XXXX-<title-in-kebab-case>.md`.
2. Document the context, risk/latency trade-offs, options considered, and chosen solution.
3. Update the index table above.
