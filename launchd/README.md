# LaunchAgent: Run compare_historical every 10 minutes

This LaunchAgent runs `scripts/compare_historical.py` every 10 minutes with `--use-volatility`, `--execute`, `--no-llm`, and `--verbose`. It uses your `.venv` Python so all dependencies are available.

## One-time setup

1. **Copy the plist to LaunchAgents** (paths in the plist must match your machine):

   ```bash
   cp launchd/com.user.compare_historical.plist ~/Library/LaunchAgents/
   ```

   If your project or username is different, edit the plist first: update `ProgramArguments` and `WorkingDirectory` to your absolute paths.

2. **Bootstrap the agent**:

   ```bash
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist
   ```

3. **Optional: Keep Mac awake** (otherwise macOS may sleep and pause the timer):
   - Run `caffeinate -di` in a Terminal (keeps awake while that window is open), or
   - Use [Amphetamine](https://apps.apple.com/app/amphetamine/id937984704) (free, Mac App Store).

## Commands

| Action        | Command                                                                     |
| ------------- | --------------------------------------------------------------------------- |
| Start (bootstrap)  | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.compare_historical.plist`   |
| Stop (unload) | `launchctl unload ~/Library/LaunchAgents/com.user.compare_historical.plist` |
| View stdout   | `tail -f /tmp/compare_historical.out.log`                                   |
| View stderr   | `tail -f /tmp/compare_historical.err.log`                                   |

## Configuration

- **Interval:** 600 seconds (10 minutes). Change `<integer>600</integer>` in the plist to adjust.
- **Run on login:** Set `<key>RunAtLoad</key><true/>` if you want it to run once immediately when loaded.
- **Paths:** `ProgramArguments[0]` = Python in your venv; `ProgramArguments[1]` = script path; `WorkingDirectory` = project root (for `config.yaml`).
