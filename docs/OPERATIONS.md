# VaultCord Operations

## Backup

VaultCord state is local and lives under `~/.vaultcord` by default.

Back up at least these files:

- `config.json`
- `vaultcord.db`
- `vaultcord.log` (optional for diagnostics)

Example (PowerShell):

```powershell
$src = Join-Path $HOME ".vaultcord"
$dst = Join-Path $HOME ("vaultcord-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
Copy-Item -Recurse -Force $src $dst
```

## Restore

1. Stop any running VaultCord process.
2. Replace `~/.vaultcord` with your backup copy.
3. Run `vault get <vault_id>` to validate decrypt works with your vault password.

## Recovery Guide

### Interrupted run / terminal closed

- Re-run `vault scrub --guild-id <guild_id> --mode <mode>`.
- Queue state is persisted in SQLite and resumes from pending/retryable jobs.

### Retry failed items

- Use `vault retry-failed --guild-id <guild_id> --mode <mode>`.

### Token invalidated

- Run `vault login` again with a fresh token.

### Wrong password

- Decryption fails by design.
- There is no password recovery mechanism; restore from backup only if you have the correct password.

## Log Safety

VaultCord redacts obvious token-bearing logs, but avoid storing logs in shared systems.

## Upgrade Checklist

1. Back up `~/.vaultcord`.
2. Upgrade package and run `pytest -q` locally.
3. Validate with `vault --help` and a dry run:
   - `vault scrub --guild-id <guild_id> --dry-run`
