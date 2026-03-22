# LaunchAgents: Trading Engine + Daily Performance Report

> **Picking up code/config changes:** `launchd` runs your repo’s Python and reads `config.yaml` from **`WorkingDirectory` on every scheduled run**. After `git pull origin main`, the next tick (or `launchctl kickstart -k …`) uses the new code — including top-level **`recovery:`** (can override `min_edge`, `calibration_extra_edge_overall`, `calibration_extra_edge_above_yes`, `parity_penalty_enabled`, `uncertainty_edge_mult`, `vol_disagree_extra_edge`, `yes_overconfidence_extra_edge`, `max_buy_limit_price` (0 = disable cap), `enable_vol_regime_scaling`, `dte_edge_scaling_enabled`, `ultra_short_min_edge_mult`, etc.), **`execution:`**, **`trading.dust_trade_*`**, **`strategy.barbell_*` / `enable_easy_win`**, **`strategy.min_payout_ratio` / `dte_edge_*` / `passive_bid_chase_*` / `position_flip_sell_*` / `hold_winner_*` / `ultra_short_*` (15m BTC)**, and the rest of the strategy block. **You do not need to re-copy plists** unless **`ProgramArguments`** or **`WorkingDirectory`** in the plist file changed.

**Barbell (live trading):** With **`strategy.barbell_enabled: true`** in repo `config.yaml`, each scheduled compare run only places directional BUY YES/NO trades that qualify as **base** (high-P consensus) or **alpha** (cheap + large net edge). See root **`README.md`** § *Barbell strategy*. Toggle in YAML only; no plist key required.

This setup includes:
- `com.user.compare_historical.plist`: runs `scripts/compare_historical.py` every 5 minutes.
- `com.user.daily_performance_report.plist`: runs `scripts/daily_performance_report.py` once daily and writes a dated `.txt` summary.
- `com.user.ollama_codex_loop.plist`: runs `ollama_codex_loop.sh` **once per day** (default **00:10** local) to update `docs/ollama_runs.html` from metrics and optionally apply Ollama-suggested patches — after the prior day’s session and shortly after the 23:55 daily report.

**Ollama loop (validated):** The shell script now (1) snapshots `scripts/backtest.py --json` as a baseline, (2) builds a richer prompt (`ollama_codex_prompt.tpl` + `config.yaml` strategy block + `models/strategies/*.py` excerpts + **compact log signals** via `scripts/extract_ollama_log_context.py` + `docs/ollama_journal.jsonl`), (3) calls **`scripts/ollama_codex_generate.py`** (`/api/generate` with **`OLLAMA_TEMPERATURE`**, default **0.2**), (4) **`git apply`** only if a valid diff is returned, (5) runs **`scripts/ollama_patch_validate.py`** (smoke `compare_historical` + backtest must succeed). Failed validation **`git apply -R`** reverts. **`OLLAMA_AUTO_COMMIT_DOCS`** (default **1** in the script) **`git commit`**s `docs/ollama_runs.html`, `docs/series_leaderboard.html`, and `docs/ollama_journal.jsonl` whenever they change — including **`no_patch`** and **`metrics_gate_skip`** — not only when a code patch validates. This repo’s plist sets **`OLLAMA_AUTO_PUSH=1`** so those commits (and validated patch commits) **`git push`** to **`origin/main`** (needs SSH/credentials). Set **`OLLAMA_AUTO_PUSH=0`** in the plist for local-only commits. Optional: `OLLAMA_REQUIRE_PNL_IMPROVE=1` to require post-patch backtest PnL ≥ baseline (only meaningful when baseline JSON is from an **earlier** DB state). Optional: **`OLLAMA_VALIDATE_MAX_BACKTEST_DROP_CENTS`** to reject patches that drop backtest PnL more than N¢ vs baseline. Env knobs: `OLLAMA_MODEL`, `OLLAMA_HOST` / `OLLAMA_PORT` (legacy typo alias `OLLMAPORT` still honored), `OLLAMA_BT_DAYS`, `OLLAMA_BT_MODE` (`real`|`synthetic`), `OLLAMA_REPO`, `OLLAMA_SMOKE_MARKETS`, `OLLAMA_LOG_CONTEXT_MAX_CHARS`.

**Default LLM:** `qwen2.5-coder:7b` (better unified diffs / config edits than general chat models). One-time on each Mac: `ollama pull qwen2.5-coder:7b`. To use another tag, set `OLLAMA_MODEL` in the environment or add `<key>OLLAMA_MODEL</key><string>mistral</string>` under `EnvironmentVariables` in `com.user.ollama_codex_loop.plist`.

**Ollama loop plist:** `OLLAMA_METRICS_GATE=0`, **`OLLAMA_TEMPERATURE=0.2`**, **`OLLAMA_AUTO_PUSH=1`**, and `PATH` includes **`/opt/homebrew/bin`**. The loop no longer shells to `ollama run` for generation (temperature is set via the API helper). If you change **EnvironmentVariables** in the plist, **re-copy** it to `~/Library/LaunchAgents/` and **reload** the agent (`launchctl bootout` + `launchctl bootstrap`, or `unload` + `load`) — `kickstart` alone does not always refresh env vars.

Launchd **always** invokes the on-disk `ollama_codex_loop.sh` path from the plist (`ProgramArguments`). After `git pull`, the **next** timer fire uses the new script and new default model name; use `launchctl kickstart -k gui/$(id -u)/com.user.ollama_codex_loop` to run it immediately (no plist copy needed unless you change `ProgramArguments` / `WorkingDirectory`).

The loop also exports a **72h** leaderboard snippet into the Ollama prompt (`OLLAMA_PROMPT_LB_HOURS`), and when `OLLAMA_AUTO_BLACKLIST=1` (default) runs **`scripts/apply_series_blacklist.py`** to update **`~/.trading-bot-data/series_blacklist_auto.txt`** — ensure `config.yaml` → **`strategy.blacklist_extra_file`** points at that path so `compare_historical` merges bans without editing YAML.

Both jobs use your `.venv` Python so dependencies are available.
`compare_historical.py` now imports `execution/engine.py` (via backward-compat shim), runs fill reconciliation diagnostics (`--reconcile-diagnostics`) in launchd mode, includes the volatility hardening stack (defensive mode, vol-ratio scaling, cross-exchange sanity gate), applies inter-arb preflight before BUY execution (`arb_inter_allow_single_leg_fallback`; repo default **false** = paired legs only), uses balance-aware inter-arb route selection, applies DTE-confidence dip scaling for far-dated contracts, runs the anti-hallucination pipeline (Bayesian update, EWMA probability, vol Z-score dampening, adaptive edge, EMA regime filter), applies the alpha engine (Order Book Imbalance edge adjustment, markout/toxic flow detection, volatility-adjusted Kelly sizing, lead-lag cross-asset signal) to correct model overconfidence and optimize position sizing before recommendations, uses EV-optimal dynamic sell thresholds (`_ev_sell_threshold`) for position evaluation, applies adaptive learning from recent trades (recency-weighted win rates, loss streak detection, per-series cooldowns), corrects structural YES bias via probability shrinkage and overconfidence edge penalties, places passive dip-based limit orders via the fixed `layered` mode (below bid with staggered depth on re-buys), and modulates limit dip depth and signal model scoring using RSI-14 (`_compute_rsi`, `_rsi_dip_adjustment`, `rsi_norm` feature).

**Crypto underlying prices:** `signals/yfinance_client.py` prefers **Gemini Exchange** public REST (`api.gemini.com`, `v2/ticker` / `v2/candles`) for mapped USD pairs (BTC/ETH/SOL/XRP) when `YFINANCE_CRYPTO_GEMINI` is enabled (default **on**). S&P 500 still uses Yahoo/yfinance. Launchd runs the script from disk each tick—`git pull` on `main` picks this up on the **next** scheduled run (no plist edit). To force crypto back to Yahoo-only, add to the plist’s `EnvironmentVariables`: `YFINANCE_CRYPTO_GEMINI` = `0`. Optional: `GEMINI_SPOT_API_BASE` if you need a different API host.

**Repo `config.yaml`:** Strategy is tuned for profitability and risk (e.g. `min_edge`, **`barbell_enabled`**, **`enable_easy_win`** paired with barbell base layer, `min_payout_ratio`, recovery overrides). Launchd loads this from `WorkingDirectory` on every run—pull `main` and you pick up strategy changes without editing the plist.

**Gemini WTI (commodities):** `compare_historical` loads WTI prediction events from Gemini (`commodities` + `search=WTI`) and uses Yahoo **`CL=F`** for history and live blend—the same underlying mapping as Kalshi **`KXWTI`**. This is implemented in Python only; **no plist or `ProgramArguments` change**. After `git pull origin main`, the next `StartInterval` run uses it, or run `launchctl kickstart -k gui/$(id -u)/com.user.compare_historical` to pick it up immediately (same as any other code-only change).

## Pick up latest code and run compare now (after `git pull`)

launchd already points at your repo’s `scripts/compare_historical.py` and passes **`--execute`** (see `com.user.compare_historical.plist`). Each scheduled run loads **current** Python + `config.yaml` from disk—no plist edit needed for code/config-only changes.

On the Mac (Terminal.app, not plain SSH if you hit bootstrap EIO before):

```bash
cd ~/git/trading-bot   # or your clone path
git pull origin main
launchctl kickstart -k gui/$(id -u)/com.user.compare_historical
```

After pull, **launchd runs the updated `config.yaml` and scripts from disk** on the next timer tick — no plist reload needed for strategy-only changes (lottery penny-jump, dip %, execution offset, **`max_buy_limit_price`**, **`recovery:`** hurdle/min-contract, **`trading.micro_balance_*`**, **`min_balance_for_limit_fallback_cents`**, strike % filters).

Optional: `tail -f /tmp/compare_historical.out.log` and confirm the banner says **`LIVE — will place orders`** and `CLI flags: --execute=True`.

Re-copy the plist **only** when `ProgramArguments` or `WorkingDirectory` in the repo file changed:

```bash
cp launchd/com.user.compare_historical.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.user.compare_historical 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist
launchctl kickstart -k gui/$(id -u)/com.user.compare_historical
```

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
| View ollama loop stdout | `tail -f /tmp/ollama_codex_loop.out.log` |
| View ollama loop stderr | `tail -f /tmp/ollama_codex_loop.err.log` |

## Live orders vs preview (troubleshooting “silent” bot)

Each run prints an explicit **Mode** and **CLI flags** line:

| Log says | Meaning |
| -------- | ------- |
| `LIVE — will place orders` | `--execute` is set and `trading.dry_run` is **false** — Kalshi/Gemini order APIs are used. |
| `PREVIEW — shows proposed orders only` | You passed **`--preview`** — no live order placement. Remove `--preview` from cron/launchd to trade. |
| `EVALUATE ONLY` | Neither `--preview` nor `--execute` — evaluation only. Add **`--execute`** for live trading. |
| `EXECUTE requested but trading.dry_run is true` | You passed `--execute` but **`config.yaml` → `trading.dry_run: true`** blocks placement. Set `dry_run: false` when you intend to trade. |

The repo’s `launchd/com.user.compare_historical.plist` includes **`--execute`** by default. If your **installed** plist omits it, or you run `python3 scripts/compare_historical.py` by hand without flags, you will see PREVIEW or EVALUATE ONLY.

**Optional two-phase gate:** `scripts/compare_live_guarded.sh` with `COMPARE_TWO_PHASE=1` runs a fast `--preview --no-llm` pass first; only if the log shows proposed trades / lottery lines does it run your full command (e.g. with `--execute`). Without that env var, the script forwards all arguments unchanged.

## Picking up script changes

launchd agents run the scripts from disk on each invocation, so code changes (e.g. bug fixes or `yfinance_client` data-source updates pushed to `origin/main`) take effect on the **next** scheduled run automatically — no reload needed. After `git pull`, you do **not** need to re-copy the plist unless `ProgramArguments` or `WorkingDirectory` changed.

**Per-trade Ollama blurbs** are built in `signals/ollama_analysis.py` (`analyze_single_trade`). Each launchd run starts a **new** Python process, so after you pull, the **next** timer fire loads the updated module. To verify immediately (without waiting for the interval), run:

`launchctl kickstart -k gui/$(id -u)/com.user.compare_historical`

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

### `Bootstrap failed: 5: Input/output error`

That usually means launchd **refused to load the job into your GUI domain** — often **not** a broken plist.

| Cause | What to do |
| ----- | ---------- |
| **SSH / remote session** | `gui/$(id -u)` is tied to a logged-in graphical session. Run `bootout` / `bootstrap` from **Terminal on the Mac itself** (or Screen Sharing), not plain SSH. |
| **Job already loaded** | `bootout` first, then `bootstrap`. If `bootout` was skipped or failed silently, bootstrap can EIO. Run: `launchctl print gui/$(id -u)/com.user.compare_historical` — if it prints a job, `launchctl bootout gui/$(id -u)/com.user.compare_historical` then bootstrap again **in Terminal.app**. |
| **Plist not readable** | `plutil -lint ~/Library/LaunchAgents/com.user.compare_historical.plist` — must say OK. Fix permissions: `chmod 644` on the plist. |

**If the agent was already working** and you only changed **code or `config.yaml`**, you do **not** need `bootstrap` at all:

```bash
cp /path/to/trading-bot/launchd/com.user.compare_historical.plist ~/Library/LaunchAgents/
launchctl kickstart -k gui/$(id -u)/com.user.compare_historical
```

The next timer fire still uses the plist on disk; copying updates LaunchAgents for the **next** login/bootstrap.

## Configuration

- **Compare interval:** 300 seconds (5 minutes). Change `<integer>300</integer>` in `com.user.compare_historical.plist`.
- **Ollama codex loop:** Daily at **00:10** local (`StartCalendarInterval` in `com.user.ollama_codex_loop.plist`). Edit `Hour` / `Minute` to shift the run; after any plist schedule change, **bootout + bootstrap** so launchd reloads the job definition.
- **Reconciliation counters in logs:** launchd compare job includes `--reconcile-diagnostics` so each run logs `seen / inserted / dup / skipped` for Kalshi + Gemini fills. Gemini fills are fetched with **pagination** and parsed from multiple API response shapes so the DB stays aligned with the exchange.
- **Daily report time:** 23:55 local time (`StartCalendarInterval`) in `com.user.daily_performance_report.plist`.
- **Daily report file path:** controlled by `config.yaml` → `backtest.daily_report_dir` (default: `~/.trading-bot-data/reports`). The report also generates PNG performance graphs (per-trade P&L bars + cumulative line) for the short and long windows.
- **Run on login:** Set `<key>RunAtLoad</key><true/>` if you want it to run once immediately when loaded.
- **Paths:** `ProgramArguments[0]` = Python in your venv; `ProgramArguments[1]` = script path; `WorkingDirectory` = project root (for `config.yaml`).
