# Resetting adaptive / calibration pressure (micro-balance recovery)

The bot does **not** use a `calibration_data.json` file. Learning signals come from:

1. **SQLite** (`storage` / `predictions` table) — resolved trades drive:
   - `get_loss_streak` → extra edge when `adaptive_learning_enabled: true`
   - `get_recent_performance` / `get_ewma_win_rate` → adaptive edge bump
   - `get_recent_performance_by_series` → per-series cooldown messaging
2. **`config.yaml`** — `adaptive_learning_enabled: false` (recommended during capital-starved recovery) disables most of that path without deleting data.

## Safer options (prefer these)

- Set **`strategy.adaptive_learning_enabled: false`** (already the defensive preset) so loss streak / EWMA bumps do not stack on `min_edge`.
- Use **`--show-config`** to confirm effective flags after edits.

## Nuclear option (loses trade history for analytics)

If you truly want a clean slate for **stored** outcomes:

1. Stop the bot / launchd jobs.
2. Locate the DB: default `~/.trading-bot-data/trading_bot.db` (or `TRADING_BOT_DATA_DIR`).
3. **Backup** the file, then either:
   - Delete only resolved rows (advanced SQL), or
   - Move/rename the whole DB and let the app recreate an empty one.

This removes calibration samples, fill history, and backtest inputs — only do it if you understand the tradeoff.

## Related scripts

- `python3 scripts/suggest_series_blacklist.py` — hints for `strategy.blacklist` from recent per-series stats.
- `python3 scripts/export_series_leaderboard.py` — HTML table of series PnL / win rate.

## Barbell (optional)

To temporarily allow more directional trades without deleting SQLite history, set **`strategy.barbell_enabled: false`** in `config.yaml` (disables the base/alpha-only filter). Restart or wait for the next launchd tick; use **`launchctl kickstart -k gui/$(id -u)/com.user.compare_historical`** for an immediate reload.
