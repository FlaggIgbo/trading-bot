# LaunchAgents: Trading Engine + Daily Performance Report

This setup includes:
- `com.user.compare_historical.plist`: runs `scripts/compare_historical.py` every 5 minutes.
- `com.user.daily_performance_report.plist`: runs `scripts/daily_performance_report.py` once daily and writes a dated `.txt` summary.

Both jobs use your `.venv` Python so dependencies are available.
`compare_historical.py` now imports `execution_engine.py`, runs fill reconciliation diagnostics (`--reconcile-diagnostics`) in launchd mode, includes the volatility hardening stack (defensive mode, vol-ratio scaling, cross-exchange sanity gate), applies inter-arb preflight before BUY execution (with optional single-leg fallback), and uses balance-aware inter-arb route selection.

## One-time setup

1. **Copy the plists to LaunchAgents** (paths must match your machine):

   ```bash
   cp launchd/com.user.compare_historical.plist ~/Library/LaunchAgents/
   cp launchd/com.user.daily_performance_report.plist ~/Library/LaunchAgents/
   ```

   If your project or username is different, edit the plist first: update `ProgramArguments` and `WorkingDirectory` to your absolute paths.

2. **Bootstrap both agents**:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.daily_performance_report.plist
   ```

3. **Optional: Keep Mac awake** (otherwise macOS may sleep and pause the timer):
   - Run `caffeinate -di` in a Terminal (keeps awake while that window is open), or
   - Use [Amphetamine](https://apps.apple.com/app/amphetamine/id937984704) (free, Mac App Store).

## Commands

| Action | Command |
| ------------- | --------------------------------------------------------------------------- |
| Start compare_historical | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist` |
| Start daily_report | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.daily_performance_report.plist` |
| Reload latest scripts | `launchctl kickstart -k gui/$(id -u)/com.user.compare_historical && launchctl kickstart -k gui/$(id -u)/com.user.daily_performance_report` |
| Stop compare_historical | `launchctl unload ~/Library/LaunchAgents/com.user.compare_historical.plist` |
| Stop daily_report | `launchctl unload ~/Library/LaunchAgents/com.user.daily_performance_report.plist` |
| View compare stdout | `tail -f /tmp/compare_historical.out.log` |
| View compare stderr | `tail -f /tmp/compare_historical.err.log` |
| View report stdout | `tail -f /tmp/daily_performance_report.out.log` |
| View report stderr | `tail -f /tmp/daily_performance_report.err.log` |

## Configuration

- **Compare interval:** 300 seconds (5 minutes). Change `<integer>300</integer>` in `com.user.compare_historical.plist`.
- **Reconciliation counters in logs:** launchd compare job includes `--reconcile-diagnostics` so each run logs `seen / inserted / dup / skipped` for Kalshi + Gemini fills.
- **Daily report time:** 23:55 local time (`StartCalendarInterval`) in `com.user.daily_performance_report.plist`.
- **Daily report file path:** controlled by `config.yaml` → `backtest.daily_report_dir` (default: `~/.trading-bot-data/reports`).
- **Run on login:** Set `<key>RunAtLoad</key><true/>` if you want it to run once immediately when loaded.
- **Paths:** `ProgramArguments[0]` = Python in your venv; `ProgramArguments[1]` = script path; `WorkingDirectory` = project root (for `config.yaml`).
