# VaultCord

VaultCord is a local-first Python application that archives and anonymizes your own Discord messages.

It encrypts message data locally, replaces the original Discord content with a vault reference (`vault://<id>`), and lets you recover originals on demand.

## Features

- Textual TUI dashboard with live progress, logs, and worker controls
- TUI-side vault retrieval by `vault_id` without leaving the dashboard
- Modular backend components (`scraper`, `worker`, `security`, `editor`)
- BYOT authentication (`vault login`) with encrypted token storage
- Per-message encryption (no plaintext message storage)
- Guild-wide queue processing with retry handling and resume support
- Guild scan includes text channels plus active/archived threads
- Dry-run mode for count previews without making API edits
- Local-only architecture (no telemetry, no external VaultCord servers)

## Security Disclaimer

VaultCord handles sensitive credentials and message content.

- You are solely responsible for token safety.
- Store and run VaultCord only on trusted systems.
- Never share your token or vault password.
- Use of Discord user tokens and API behavior must comply with Discord's terms and policies.

## Token Responsibility Warning

VaultCord never extracts tokens automatically. You must provide your token manually.

The token is encrypted at rest with a password-derived key and is never stored in plaintext.

## Requirements

- Python 3.11+
- Local filesystem access
- Your Discord token (manually provided)

## Setup

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1

pip install -e .
```

## Usage

### 1) Login and Store Encrypted Token

```bash
vault login
```

Prompts for:
- Discord token (hidden)
- Vault password

VaultCord validates token with `GET /users/@me`.

### 2) Dry Run (Preview Only)

```bash
vault scrub --guild-id <guild_id> --dry-run
```

Shows counts by type (`all`, `text`, `links`, `media`) and makes no edits.

### 3) Scrub and Replace Messages

```bash
vault scrub --guild-id <guild_id> --mode all --order newest
```

Supported modes:
- `all`
- `text`
- `links`
- `media`

Supported order:
- `newest`
- `oldest`

### 4) Retry Failed Jobs

```bash
vault retry-failed --guild-id <guild_id> --mode all
```

### 5) Retrieve Original Message

```bash
vault get <vault_id>
```

### 6) Run Interactive Dashboard

```bash
vault tui
```

TUI start flow:

- Use the top **Command Strip** for `Guild ID`, mode, order, and run controls
- Use **Command Deck** (left) for `vault_id` retrieval
- Monitor **Telemetry** (right) and **Event Console** (bottom) live during execution

When work finishes, TUI now shows an explicit completion message with processed/failed/elapsed stats.

## Worker Behavior

- Random edit delay: 15-25 seconds
- Session run window: 1.5-3 hours
- Session pause window: 0.5-2 hours
- Max retries per job: 3
- Queue states: `pending`, `done`, `failed`
- Graceful shutdown on `Ctrl+C` with resumable queue state

## Config

Default config path:

- `~/.vaultcord/config.json`

Default data files:

- `~/.vaultcord/vaultcord.db`
- `~/.vaultcord/vaultcord.log`

Scheduler windows and retry settings are configurable in `config.json`.

## Development

Run tests:

```bash
pytest -q
```

CI runs on GitHub Actions for Python 3.11/3.12/3.13 on Linux and Windows.

## Operations

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for backup/restore, interruption recovery, and upgrade guidance.

## Security Reporting

See [SECURITY.md](SECURITY.md).

## Project Structure

```text
vaultcord/
  cli.py
  tui.py
  service.py
  worker.py
  scraper.py
  editor.py
  security.py
  discord_api.py
  storage.py
  config.py
```

## License

MIT
