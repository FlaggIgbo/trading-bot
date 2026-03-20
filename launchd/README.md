# LaunchAgents: Trading Engine + Daily Performance Report

This setup includes:
- `com.user.compare_historical.plist`: runs `scripts/compare_historical.py` every 5 minutes.
- `com.user.daily_performance_report.plist`: runs `scripts/daily_performance_report.py` once daily and writes a dated `.txt` summary.
- `com.user.ollama_codex_loop.plist`: runs `ollama_codex_loop.sh` every 30 minutes to update `docs/ollama_runs.html` from metrics and optionally apply Ollama-suggested patches.

Both jobs use your `.venv` Python so dependencies are available.
`compare_historical.py` now imports `execution/engine.py` (via backward-compat shim), runs fill reconciliation diagnostics (`--reconcile-diagnostics`) in launchd mode, includes the volatility hardening stack (defensive mode, vol-ratio scaling, cross-exchange sanity gate), applies inter-arb preflight before BUY execution (`arb_inter_allow_single_leg_fallback`; repo default **false** = paired legs only), uses balance-aware inter-arb route selection, applies DTE-confidence dip scaling for far-dated contracts, runs the anti-hallucination pipeline (Bayesian update, EWMA probability, vol Z-score dampening, adaptive edge, EMA regime filter), applies the alpha engine (Order Book Imbalance edge adjustment, markout/toxic flow detection, volatility-adjusted Kelly sizing, lead-lag cross-asset signal) to correct model overconfidence and optimize position sizing before recommendations, uses EV-optimal dynamic sell thresholds (`_ev_sell_threshold`) for position evaluation, applies adaptive learning from recent trades (recency-weighted win rates, loss streak detection, per-series cooldowns), corrects structural YES bias via probability shrinkage and overconfidence edge penalties, places passive dip-based limit orders via the fixed `layered` mode (below bid with staggered depth on re-buys), and modulates limit dip depth and signal model scoring using RSI-14 (`_compute_rsi`, `_rsi_dip_adjustment`, `rsi_norm` feature).

**Crypto underlying prices:** `signals/yfinance_client.py` prefers **Gemini Exchange** public REST (`api.gemini.com`, `v2/ticker` / `v2/candles`) for mapped USD pairs (BTC/ETH/SOL/XRP) when `YFINANCE_CRYPTO_GEMINI` is enabled (default **on**). S&P 500 still uses Yahoo/yfinance. Launchd runs the script from disk each tick—`git pull` on `main` picks this up on the **next** scheduled run (no plist edit). To force crypto back to Yahoo-only, add to the plist’s `EnvironmentVariables`: `YFINANCE_CRYPTO_GEMINI` = `0`. Optional: `GEMINI_SPOT_API_BASE` if you need a different API host.

**Repo `config.yaml`:** Uses the **defensive profitability preset** (e.g. higher `min_edge`, `enable_easy_win: false`, smaller absolute-winner balance slice). Launchd loads this from `WorkingDirectory` on every run—pull `main` and you pick up strategy changes without editing the plist.

## One-time setup

1. **Copy the plists to LaunchAgents** (paths must match your machine):

   ```bash
   cp launchd/com.user.compare_historical.plist launchd/com.user.daily_performance_report.plist launchd/com.user.ollama_codex_loop.plist ~/Library/LaunchAgents/
   ```

   If your project or username is different, edit the plist first: update `ProgramArguments` and `WorkingDirectory` to your absolute paths.

2. **Bootstrap both agents**:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.daily_performance_report.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ollama_codex_loop.plist
   ```

3. **Optional: Keep Mac awake** (otherwise macOS may sleep and pause the timer):
   - Run `caffeinate -di` in a Terminal (keeps awake while that window is open), or
   - Use [Amphetamine](https://apps.apple.com/app/amphetamine/id937984704) (free, Mac App Store).

## Commands

| Action | Command |
| ------------- | --------------------------------------------------------------------------- |
| Start compare_historical | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist` |
| Start daily_report | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.daily_performance_report.plist` |
| Reload latest scripts | `launchctl kickstart -k gui/$(id -u)/com.user.compare_historical && launchctl kickstart -k gui/$(id -u)/com.user.daily_performance_report && launchctl kickstart -k gui/$(id -u)/com.user.ollama_codex_loop` |
| Stop compare_historical | `launchctl unload ~/Library/LaunchAgents/com.user.compare_historical.plist` |
| Stop daily_report | `launchctl unload ~/Library/LaunchAgents/com.user.daily_performance_report.plist` |
| Stop ollama_codex_loop | `launchctl unload ~/Library/LaunchAgents/com.user.ollama_codex_loop.plist` |
| View compare stdout | `tail -f /tmp/compare_historical.out.log` |
| View compare stderr | `tail -f /tmp/compare_historical.err.log` |
| View report stdout | `tail -f /tmp/daily_performance_report.out.log` |
| View report stderr | `tail -f /tmp/daily_performance_report.err.log` |

## Picking up script changes

launchd agents run the scripts from disk on each invocation, so code changes (e.g. bug fixes or `yfinance_client` data-source updates pushed to `origin/main`) take effect on the **next** scheduled run automatically — no reload needed. After `git pull`, you do **not** need to re-copy the plist unless `ProgramArguments` or `WorkingDirectory` changed.

To force an immediate run after pulling updates:

```bash
launchctl kickstart -k gui/$(id -u)/com.user.compare_historical
launchctl kickstart -k gui/$(id -u)/com.user.daily_performance_report
```

**After changing `ProgramArguments` or `WorkingDirectory` in a plist**, reload the job so launchd picks up the file:

```bash
launchctl bootout gui/$(id -u)/com.user.compare_historical 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist
launchctl kickstart -k gui/$(id -u)/com.user.compare_historical
```

Code-only changes (no plist edit) still take effect on the next scheduled run; `kickstart -k` runs the current on-disk script immediately.

## Configuration

- **Compare interval:** 300 seconds (5 minutes). Change `<integer>300</integer>` in `com.user.compare_historical.plist`.
- **Reconciliation counters in logs:** launchd compare job includes `--reconcile-diagnostics` so each run logs `seen / inserted / dup / skipped` for Kalshi + Gemini fills. Gemini fills are fetched with **pagination** and parsed from multiple API response shapes so the DB stays aligned with the exchange.
- **Daily report time:** 23:55 local time (`StartCalendarInterval`) in `com.user.daily_performance_report.plist`.
- **Daily report file path:** controlled by `config.yaml` → `backtest.daily_report_dir` (default: `~/.trading-bot-data/reports`). The report also generates PNG performance graphs (per-trade P&L bars + cumulative line) for the short and long windows.
- **Run on login:** Set `<key>RunAtLoad</key><true/>` if you want it to run once immediately when loaded.
- **Paths:** `ProgramArguments[0]` = Python in your venv; `ProgramArguments[1]` = script path; `WorkingDirectory` = project root (for `config.yaml`).
