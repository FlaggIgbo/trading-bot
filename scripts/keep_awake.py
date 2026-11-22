"""
Chromebox Keep-Awake Daemon
===========================
Periodically resets X11/Sommelier screen idle timer and ensures display
and container stay active throughout long-running parallel trading bot executions.
"""

import os
import subprocess
import time

def main():
    display = os.getenv("DISPLAY", ":0")
    env = {**os.environ, "DISPLAY": display}

    # Initial setup: disable blanking and screensaver timeout
    try:
        subprocess.run(["xset", "s", "off", "s", "noblank"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    while True:
        try:
            subprocess.run(["xset", "s", "reset"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        time.sleep(25)

if __name__ == "__main__":
    main()
