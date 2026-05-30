#!/usr/bin/env python3
"""
Cross-platform auto-restart script for toolbox app.
Runs continuously and at 2:00 AM every day:
  1. Stops the running Flask app
  2. Pulls latest code from prod branch
  3. Restarts the Flask app

Usage:
    python auto_restart.py

Requirements:
    - Python 3.7+
    - Git installed and in PATH
    - SSH key configured for the repo (git@github-similecat:similecat/toolbox.git)
"""

import subprocess
import sys
import os
import time
import signal
import platform
import datetime
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────
REPO_DIR = Path(__file__).resolve().parent
BRANCH = "prod"
APP_PORT = 5000
RESTART_HOUR = 2
RESTART_MINUTE = 0
CHECK_INTERVAL = 60  # seconds between checks
LOG_PREFIX = "[AutoRestart]"

# ─── Helpers ─────────────────────────────────────────────────────
def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {LOG_PREFIX} {msg}", flush=True)


def run(cmd, cwd=None):
    """Run a shell command and return (returncode, stdout, stderr)."""
    log(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or REPO_DIR,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except Exception as e:
        return -1, "", str(e)


def find_app_pid():
    """Find the PID of the running Flask app (python app.py)."""
    system = platform.system()

    if system == "Windows":
        rc, out, _ = run(["wmic", "process", "where",
                          "name='python.exe'", "get", "ProcessId,CommandLine"])
        if rc == 0:
            for line in out.splitlines():
                if "app.py" in line.lower():
                    try:
                        pid = int(line.strip().split()[0])
                        return pid
                    except (ValueError, IndexError):
                        continue
    else:
        # Linux / macOS
        rc, out, _ = run(["pgrep", "-f", "python.*app.py"])
        if rc == 0 and out:
            return int(out.splitlines()[0])
    return None


def stop_app():
    """Gracefully stop the running Flask app."""
    pid = find_app_pid()
    if pid is None:
        log("No running app found, nothing to stop.")
        return True

    log(f"Stopping app (PID {pid})...")
    system = platform.system()

    if system == "Windows":
        run(["taskkill", "/F", "/PID", str(pid)])
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(3)
            # Force kill if still running
            if find_app_pid() == pid:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass  # Process already gone

    time.sleep(2)
    remaining = find_app_pid()
    if remaining:
        log(f"Warning: App still running (PID {remaining}), force-killing...")
        if system == "Windows":
            run(["taskkill", "/F", "/PID", str(remaining)])
        else:
            try:
                os.kill(remaining, signal.SIGKILL)
            except OSError:
                pass
    else:
        log("App stopped successfully.")
    return True


def pull_latest():
    """Pull latest code from the prod branch."""
    # Ensure we're on the prod branch
    rc, out, err = run(["git", "checkout", BRANCH])
    if rc != 0:
        log(f"Git checkout failed: {err}")
        return False

    # Pull latest changes
    rc, out, err = run(["git", "pull", "origin", BRANCH])
    if rc != 0:
        log(f"Git pull failed: {err}")
        return False

    log("Code pulled successfully.")
    return True


def start_app():
    """Start the Flask app in the background."""
    system = platform.system()
    python_exe = sys.executable or "python3"

    log(f"Starting app with {python_exe}...")

    if system == "Windows":
        # Windows: use start /B to run in background
        cmd = f'start /B "" {python_exe} app.py'
        subprocess.Popen(cmd, shell=True, cwd=REPO_DIR)
    else:
        # Linux/macOS: redirect output to a log file and run in background
        log_file = REPO_DIR / "app.log"
        with open(log_file, "a") as f:
            subprocess.Popen(
                [python_exe, "app.py"],
                cwd=REPO_DIR,
                stdout=f,
                stderr=f,
                start_new_session=True,
            )

    time.sleep(3)
    pid = find_app_pid()
    if pid:
        log(f"App started successfully (PID {pid}).")
        return True
    else:
        log("Warning: Could not confirm app started.")
        return False


def should_restart():
    """Check if it's time to restart (2:00 AM)."""
    now = datetime.datetime.now()
    return now.hour == RESTART_HOUR and now.minute == RESTART_MINUTE


def wait_until_target_time():
    """Block until the target restart time arrives."""
    now = datetime.datetime.now()
    target = now.replace(hour=RESTART_HOUR, minute=RESTART_MINUTE, second=0, microsecond=0)

    # If we've already passed 2 AM today, target is tomorrow
    if now >= target:
        target += datetime.timedelta(days=1)

    wait_seconds = (target - now).total_seconds()
    log(f"Next restart scheduled at {target.strftime('%Y-%m-%d %H:%M:%S')} "
        f"({wait_seconds / 3600:.1f} hours from now).")
    return wait_seconds


# ─── Main Loop ──────────────────────────────────────────────────
def main():
    log("=" * 60)
    log("Auto-restart monitor started")
    log(f"  Repo : {REPO_DIR}")
    log(f"  Branch: {BRANCH}")
    log(f"  Port : {APP_PORT}")
    log(f"  Schedule: Daily at {RESTART_HOUR:02d}:{RESTART_MINUTE:02d}")
    log(f"  Platform: {platform.system()} ({platform.release()})")
    log("=" * 60)

    # Initial: ensure app is running
    if not find_app_pid():
        log("No running app detected. Starting app...")
        pull_latest()
        start_app()
    else:
        log(f"App already running (PID {find_app_pid()}), keeping it alive.")

    while True:
        # Wait until close to the target time, then poll more frequently
        sleep_time = CHECK_INTERVAL

        if should_restart():
            log("-" * 40)
            log("Restart time reached! Performing daily update...")
            log("-" * 40)

            # Stop current app
            stop_app()

            # Pull latest code
            if pull_latest():
                # Restart app
                start_app()
                log("Daily restart completed successfully.")
            else:
                log("Pull failed, restarting app with current code anyway...")
                start_app()

            # After restart, wait until next day's target time
            remaining = wait_until_target_time()
            # Sleep in smaller chunks so we can still respond to interrupts
            while remaining > 0:
                chunk = min(CHECK_INTERVAL, remaining)
                time.sleep(chunk)
                remaining -= chunk

        else:
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nInterrupted by user. Stopping...")
        stop_app()
        sys.exit(0)
