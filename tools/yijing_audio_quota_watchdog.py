#!/usr/bin/env python3
"""Auto-restart audio batch after NotebookLM quota cooldown.

Behavior:
  1. Sleep 12h initially (let NotebookLM daily quota fully reset).
  2. Loop forever, every 3h:
     - If batch is alive (manual restart or our previous restart held), skip.
     - Else, start the batch. The batch's own quota detection will break out
       again if quota still pressed; we'll retry on the next 3h tick.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKLM_PY = os.environ.get("NOTEBOOKLM_PY", sys.executable)
SCRIPT_REL = "tools/yijing_audio_md_plus_slides_release.py"
LOG = REPO_ROOT / "workspace/reports/yijing_audio_2026-05-08.log"
WATCHDOG_LOG = REPO_ROOT / "workspace/reports/yijing_audio_watchdog.log"

INITIAL_COOLDOWN_S = 12 * 3600
POLL_INTERVAL_S = 3 * 3600
WARMUP_S = 60


def log(msg: str) -> None:
    line = f"[watchdog {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def batch_alive() -> bool:
    r = subprocess.run(
        ["pgrep", "-f", SCRIPT_REL],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def start_batch() -> None:
    cmd = (
        f"cd {REPO_ROOT} && "
        f"nohup {NOTEBOOKLM_PY} {SCRIPT_REL} --skip-video --pause-between 5 "
        f">> {LOG} 2>&1 &"
    )
    log(f"starting batch: {cmd}")
    subprocess.run(["bash", "-c", cmd], check=False)


def main() -> int:
    log(
        f"watchdog start; initial sleep {INITIAL_COOLDOWN_S}s "
        f"(~{INITIAL_COOLDOWN_S/3600:.0f}h), poll every {POLL_INTERVAL_S/3600:.0f}h"
    )
    time.sleep(INITIAL_COOLDOWN_S)
    while True:
        if batch_alive():
            log("batch alive — skipping this tick")
        else:
            log("batch dead — restarting")
            start_batch()
            time.sleep(WARMUP_S)
            if batch_alive():
                log("batch resumed successfully")
            else:
                log("batch did not start (still quota? will retry next tick)")
        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    sys.exit(main())
