"""
Google Drive Sync Agent — Queue Watcher
Polls the queue directory every N seconds and calls scan_queue().

Usage:
    python3 watcher.py

Stop with Ctrl+C.
"""

import logging
import signal
import sys
import time
from pathlib import Path

import yaml

# Agent lives in the same directory
sys.path.insert(0, str(Path(__file__).parent))
from agent import load_config, scan_queue, setup_logging


def _load_intervals() -> tuple[str, int]:
    """Returns (queue_dir, poll_interval_seconds) from config."""
    cfg = load_config()
    queue_dir = cfg.get("queue_dir", "~/.config/gdrive-sync/queue/")
    interval = int(cfg.get("poll_interval_seconds", 30))
    return queue_dir, interval


def _ensure_dirs():
    """Create queue/processed/flagged dirs at startup if they don't exist."""
    cfg = load_config()
    for key in ("queue_dir", "processed_dir", "flagged_dir"):
        path = Path(cfg.get(key, "~/.config/gdrive-sync/")).expanduser()
        path.mkdir(parents=True, exist_ok=True)


def main():
    setup_logging()
    log = logging.getLogger(__name__)

    _ensure_dirs()
    queue_dir, interval = _load_intervals()

    log.info("gdrive-sync-agent watcher starting.")
    log.info("Queue: %s | Poll interval: %ss", queue_dir, interval)
    log.info("Press Ctrl+C to stop.")

    # Graceful shutdown on SIGTERM (e.g. from systemd)
    def _handle_sigterm(signum, frame):
        log.info("Received SIGTERM. Shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while True:
            try:
                results = scan_queue(queue_dir)
                if results:
                    ok = sum(1 for r in results if r.get("status") == "ok")
                    errors = len(results) - ok
                    log.info("Processed %d event(s): %d ok, %d error(s).", len(results), ok, errors)
            except Exception as exc:
                log.error("Unexpected error in scan_queue: %s", exc, exc_info=True)

            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("Keyboard interrupt received. Watcher stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
