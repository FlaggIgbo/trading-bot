# Performance & Latency Bottlenecks

Use this document to track throughput, latency, database lock contention, and network timing bottlenecks discovered during bot operation, backtesting, or k6 stress testing, along with fixes and benchmark validations.

---

## Summary Log

| ID | Date | Subsystem / Component | Baseline (P95 / Timing) | Bottleneck Root Cause | Fix / Optimization | Post-Fix (P95 / Timing) | Status |
|----|------|-----------------------|-------------------------|-----------------------|--------------------|-------------------------|--------|
| *PB-01* | *YYYY-MM-DD* | *e.g., Market Scanner Upsert* | *e.g., 2,800ms for 200 markets* | *e.g., Autocommit per row in SQLite; no batch transaction* | *e.g., Wrapped in single executemany transaction + WAL mode* | *e.g., 45ms for 200 markets* | *Fixed* |
| *PB-02* | *YYYY-MM-DD* | *[Component / Routine]* | *[Baseline]* | *[Root Cause]* | *[Fix]* | *[Post-Fix]* | *Investigating* |

---

## Bottleneck Investigation Template

### [PB-XXXX] Title / Short Description

#### 1. Baseline Profile
- **Target Pipeline Stage / Routine**: `[e.g., Scanner fetch-and-upsert, TradeEngine order evaluation, or Settler poll]`
- **Test / Load Scenario**: `[e.g., 100 concurrent market ticker updates evaluated against risk rules via load-tests/k6-test.js]`
- **Baseline Duration / Throughput**: `[e.g., 1,450ms cycle time @ 10 updates/sec]`
- **Latency Percentiles**:
  - P50: `[e.g., 400ms]`
  - P95: `[e.g., 1,800ms]`
  - P99: `[e.g., 3,200ms]`
- **Database Lock Wait Time**: `[e.g., 850ms SQLite busy timeout]`

#### 2. Root Cause Analysis
- **Observed Behavior**: `[SQLite database locked contention during parallel report generation, synchronous network I/O in main loop, unindexed query]`
- **Diagnostic Tool Used**: `[e.g., cProfile, py-spy, SQLite PRAGMA compile_options / EXPLAIN, k6 metrics]`
- **Root Cause Details**:
  ```text
  [TODO: Insert traceback, profile flamegraph snippet, or query plan here]
  ```

#### 3. Optimization Applied
- **Changes Implemented**:
  - `[TODO: Description of WAL tuning, batching, async client pooling, indexing, or memory caching]`
- **PR / Commit**: `[TODO: Link to commit or PR]`

#### 4. Post-Optimization Verification
- **Post-Fix Cycle Time / Throughput**: `[e.g., 95ms cycle time (+1,400% throughput)]`
- **Post-Fix Latency**:
  - P50: `[e.g., 20ms]`
  - P95: `[e.g., 65ms]`
  - P99: `[e.g., 110ms]`
- **Database Lock Contention**: `[e.g., 0ms wait]`
- **Safety / Invariants Verified**: `[Yes/No]`
