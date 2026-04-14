"""
Google Drive Structured Sync Agent
Classification and filing logic for Drive files into an Obsidian vault.
No Google API calls here — n8n handles Drive polling and drops JSON events
into the queue directory. This agent processes those events.

Run as self-test:
    python3 agent.py
"""

import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_DEFAULT_CONFIG = {
    "vault_root": "~/vault/",
    "queue_dir": "~/.config/gdrive-sync/queue/",
    "processed_dir": "~/.config/gdrive-sync/processed/",
    "flagged_dir": "~/.config/gdrive-sync/flagged/",
    "log_file": "~/.config/gdrive-sync/sync.log",
    "poll_interval_seconds": 30,
    "classification_rules": [
        {"extensions": [".mp3", ".mp4", ".wav", ".ogg", ".m4a"], "destination": "Meeting Notes/", "category": "recording"},
        {"extensions": [".pptx", ".ppt", ".key"], "destination": "Presentations/", "category": "deck"},
        {"extensions": [".pdf"], "filename_contains": "contract", "destination": "Legal/", "category": "contract"},
        {"extensions": [".pdf"], "filename_contains": "invoice", "destination": "Finance/", "category": "invoice"},
    ],
}


def load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        # Fill in any missing keys from defaults
        for k, v in _DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return dict(_DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Logging setup (called once; watcher.py also calls this)
# ---------------------------------------------------------------------------

def setup_logging(log_file: str | None = None):
    cfg = load_config()
    log_path = Path(log_file or cfg.get("log_file", "~/.config/gdrive-sync/sync.log")).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def classify_file(filename: str, mime_type: str, folder_path: str) -> dict:
    """
    Returns {category, vault_destination, confidence} for a given file.

    Rules are evaluated in order from config. The first match wins.
    Falls back to unknown / Needs Review / 0.3 if nothing matches.
    """
    cfg = load_config()
    rules = cfg.get("classification_rules", _DEFAULT_CONFIG["classification_rules"])

    stem = Path(filename).stem.lower()
    suffix = Path(filename).suffix.lower()
    mime = (mime_type or "").lower()

    # Audio/video via mime type (catches Drive-native audio/* etc.)
    if mime.startswith("audio/") or mime.startswith("video/"):
        return {
            "category": "recording",
            "vault_destination": "Meeting Notes/",
            "confidence": 0.95,
        }

    for rule in rules:
        extensions = [e.lower() for e in rule.get("extensions", [])]
        if suffix not in extensions:
            continue

        keyword = rule.get("filename_contains", "")
        if keyword and keyword.lower() not in stem:
            continue

        return {
            "category": rule["category"],
            "vault_destination": rule["destination"],
            "confidence": 0.9,
        }

    # Fallback
    return {
        "category": "unknown",
        "vault_destination": "Needs Review/",
        "confidence": 0.3,
    }


def inject_metadata(
    file_path: str,
    category: str,
    filename: str,
    drive_folder: str,
) -> str:
    """
    Returns a Markdown string with YAML frontmatter prepended.
    If file_path points to an existing text/markdown file, its content is appended
    after the frontmatter. Binary files (recordings, PDFs, decks) get a stub note
    linking to the original filename instead.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    stem = Path(filename).stem
    suffix = Path(filename).suffix.lower()

    binary_extensions = {".mp3", ".mp4", ".wav", ".ogg", ".m4a", ".pdf", ".pptx", ".ppt", ".key"}
    is_binary = suffix in binary_extensions

    frontmatter_lines = [
        "---",
        f"type: {category}",
        "source: google-drive",
        f"drive_folder: {drive_folder}",
        f"synced_at: {now_iso}",
        f"aliases:",
        f"  - {stem}",
        "---",
        "",
    ]

    if is_binary:
        body = (
            f"# {stem}\n\n"
            f"**Category:** {category}\n"
            f"**Original file:** `{filename}`\n"
            f"**Drive folder:** {drive_folder}\n"
            f"**Synced:** {now_iso}\n"
        )
    else:
        # Try to read existing text content
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                body = f.read()
        except (OSError, IOError):
            body = f"# {stem}\n\n_Content could not be read from `{file_path}`._\n"

    return "\n".join(frontmatter_lines) + body


def check_duplicates(filename: str, vault_root: str) -> str:
    """
    Returns a safe filename (with timestamp suffix) if a file with the same name
    already exists anywhere under vault_root/destination.
    The destination check happens in process_file; here we just ensure the
    proposed filename is unique within the destination folder passed implicitly
    by the caller using full path logic.

    For simpler use: pass the full target path as filename, and this returns
    a path that does not yet exist.
    """
    target = Path(filename)
    if not target.exists():
        return str(target)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    new_path = parent / f"{stem}_{ts}{suffix}"

    # Extremely unlikely, but guard against sub-second collisions
    counter = 1
    while new_path.exists():
        new_path = parent / f"{stem}_{ts}_{counter}{suffix}"
        counter += 1

    return str(new_path)


def process_file(drive_file_meta: dict, vault_root: str) -> dict:
    """
    Full pipeline for one Drive file event.

    drive_file_meta keys:
        filename      — original filename (e.g. "Q1 Invoice.pdf")
        mime_type     — MIME type string
        folder_path   — Drive folder path string (for metadata)
        local_tmp_path — local path where n8n downloaded the file

    Returns {status, destination, category, filename}
    """
    filename = drive_file_meta.get("filename", "unknown_file")
    mime_type = drive_file_meta.get("mime_type", "")
    folder_path = drive_file_meta.get("folder_path", "")
    local_tmp_path = drive_file_meta.get("local_tmp_path", "")

    log = logging.getLogger(__name__)

    try:
        classification = classify_file(filename, mime_type, folder_path)
        category = classification["category"]
        vault_destination = classification["vault_destination"]
        confidence = classification["confidence"]

        vault_root_path = Path(vault_root).expanduser()
        dest_dir = vault_root_path / vault_destination
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Determine the output filename:
        # Non-markdown binary files get a companion .md note.
        # Text / markdown files are written directly.
        suffix = Path(filename).suffix.lower()
        binary_extensions = {".mp3", ".mp4", ".wav", ".ogg", ".m4a", ".pdf", ".pptx", ".ppt", ".key"}

        if suffix in binary_extensions:
            # Write a .md companion note; also copy the binary if it exists locally
            md_filename = Path(filename).stem + ".md"
            raw_dest = str(dest_dir / md_filename)
            final_dest = check_duplicates(raw_dest, vault_root)

            content = inject_metadata(local_tmp_path, category, filename, folder_path)
            with open(final_dest, "w", encoding="utf-8") as f:
                f.write(content)

            # Copy the binary file alongside if it exists
            if local_tmp_path and Path(local_tmp_path).exists():
                raw_bin = str(dest_dir / filename)
                final_bin = check_duplicates(raw_bin, vault_root)
                shutil.copy2(local_tmp_path, final_bin)
                log.info("Copied binary: %s -> %s", local_tmp_path, final_bin)
        else:
            # Text-like file — write with injected frontmatter
            raw_dest = str(dest_dir / filename)
            final_dest = check_duplicates(raw_dest, vault_root)

            content = inject_metadata(local_tmp_path, category, filename, folder_path)
            with open(final_dest, "w", encoding="utf-8") as f:
                f.write(content)

        log.info(
            "Filed '%s' -> %s (category=%s, confidence=%.2f)",
            filename,
            final_dest,
            category,
            confidence,
        )

        return {
            "status": "ok",
            "destination": final_dest,
            "category": category,
            "filename": filename,
            "confidence": confidence,
        }

    except Exception as exc:
        log.error("Failed to process '%s': %s", filename, exc, exc_info=True)
        return {
            "status": "error",
            "destination": None,
            "category": "unknown",
            "filename": filename,
            "error": str(exc),
        }


def scan_queue(queue_dir: str) -> list[dict]:
    """
    Reads all .json files from queue_dir.
    For each:
      - Parses the JSON event
      - Calls process_file
      - On success: moves JSON to processed/
      - On error:   moves JSON to flagged/
    Returns list of result dicts from process_file.
    """
    cfg = load_config()
    vault_root = cfg.get("vault_root", "~/vault/")
    processed_dir = Path(cfg.get("processed_dir", "~/.config/gdrive-sync/processed/")).expanduser()
    flagged_dir = Path(cfg.get("flagged_dir", "~/.config/gdrive-sync/flagged/")).expanduser()

    processed_dir.mkdir(parents=True, exist_ok=True)
    flagged_dir.mkdir(parents=True, exist_ok=True)

    queue_path = Path(queue_dir).expanduser()
    queue_path.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger(__name__)
    results = []

    json_files = sorted(queue_path.glob("*.json"))
    if not json_files:
        log.debug("Queue is empty.")
        return results

    for json_file in json_files:
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                event = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Cannot parse queue file %s: %s", json_file.name, exc)
            shutil.move(str(json_file), str(flagged_dir / json_file.name))
            results.append({"status": "error", "filename": json_file.name, "error": str(exc)})
            continue

        result = process_file(event, vault_root)
        results.append(result)

        if result["status"] == "ok":
            shutil.move(str(json_file), str(processed_dir / json_file.name))
            log.info("Moved to processed/: %s", json_file.name)
        else:
            shutil.move(str(json_file), str(flagged_dir / json_file.name))
            log.warning("Moved to flagged/: %s", json_file.name)

    return results


# ---------------------------------------------------------------------------
# Self-test (python3 agent.py)
# ---------------------------------------------------------------------------

def _self_test():
    """Creates a sample queue JSON, processes it, prints result."""
    print("\n=== gdrive-sync-agent self-test ===\n")

    setup_logging()

    cfg = load_config()
    queue_dir = Path(cfg.get("queue_dir", "~/.config/gdrive-sync/queue/")).expanduser()
    queue_dir.mkdir(parents=True, exist_ok=True)

    # Write a sample event JSON
    sample_event = {
        "filename": "Q1 2026 Invoice - Client.pdf",
        "mime_type": "application/pdf",
        "folder_path": "My Drive/Finance/2026",
        "local_tmp_path": "",  # no actual file; inject_metadata will handle gracefully
    }
    sample_path = queue_dir / "test_event_selftest.json"
    with open(sample_path, "w") as f:
        json.dump(sample_event, f, indent=2)
    print(f"Wrote sample event to: {sample_path}")

    # Test classify_file directly
    print("\n--- classify_file tests ---")
    tests = [
        ("meeting_recording.mp3", "audio/mpeg", "Drive/Meetings"),
        ("Q1 Invoice.pdf", "application/pdf", "Drive/Finance"),
        ("Master Contract - ClientX.pdf", "application/pdf", "Drive/Legal"),
        ("Deck v3 Final.pptx", "application/vnd.ms-powerpoint", "Drive/Sales"),
        ("random_file.xlsx", "application/vnd.ms-excel", "Drive/Misc"),
        ("zoom_recording.mp4", "video/mp4", "Drive/Meetings"),
    ]
    for fname, mime, folder in tests:
        result = classify_file(fname, mime, folder)
        print(f"  {fname!r:45} -> category={result['category']!r:12} dest={result['vault_destination']!r:20} conf={result['confidence']}")

    # Process the queue
    print("\n--- scan_queue ---")
    results = scan_queue(str(queue_dir))
    for r in results:
        print(f"  {r}")

    print("\nSelf-test complete. Check ~/.config/gdrive-sync/ for logs and processed files.")
    print("Check vault Finance/ folder for the filed note.\n")


if __name__ == "__main__":
    _self_test()
