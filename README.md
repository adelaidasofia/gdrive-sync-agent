# gdrive-sync-agent

A lightweight Python agent that classifies Google Drive files and files them into an Obsidian vault with YAML frontmatter. Works with n8n (or any tool that drops JSON events into a queue directory) to create a fully automated Google Drive -> Obsidian sync pipeline.

No Google API credentials required in this agent. Your n8n workflow handles Drive polling and deposits JSON event files into the queue directory.

## What it does

- Reads JSON event files from a queue directory (dropped by n8n or similar)
- Classifies files by extension and filename keywords (recording, deck, contract, invoice, etc.)
- Writes companion Markdown notes with YAML frontmatter into your vault
- Copies binary files (PDFs, audio, video) alongside the note
- Moves processed events to `processed/` and failed ones to `flagged/`
- Optional watcher (`watcher.py`) polls the queue on a configurable interval

## Install

```bash
pip install pyyaml python-frontmatter watchdog
```

## Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/adelaidasofia/gdrive-sync-agent.git
   cd gdrive-sync-agent
   ```

2. Edit `config.yaml` to set your vault root and classification rules:
   ```yaml
   vault_root: ~/vault/
   queue_dir: ~/.config/gdrive-sync/queue/
   ```

3. Configure your n8n workflow to drop JSON events into `queue_dir`. Each event should have:
   ```json
   {
     "filename": "Q1 Invoice.pdf",
     "mime_type": "application/pdf",
     "folder_path": "My Drive/Finance/2026",
     "local_tmp_path": "/tmp/gdrive-download/Q1 Invoice.pdf"
   }
   ```

4. Run a self-test:
   ```bash
   python3 agent.py
   ```

5. Start the queue watcher:
   ```bash
   python3 watcher.py
   ```

## Configuration

All settings live in `config.yaml`. Classification rules are evaluated top-to-bottom; first match wins.

```yaml
vault_root: ~/vault/

classification_rules:
  - extensions: [.mp3, .mp4, .wav]
    destination: "Meeting Notes/"
    category: recording

  - extensions: [.pdf]
    filename_contains: invoice
    destination: "Finance/"
    category: invoice
```

Add as many rules as you need. Unmatched files go to `Needs Review/`.

## Tools

| File | Purpose |
|------|---------|
| `agent.py` | Core classify + process + queue scan logic |
| `watcher.py` | Polling loop that calls `scan_queue()` on an interval |
| `config.yaml` | Your classification rules and paths |

## License

MIT
