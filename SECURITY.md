# Security Policy

## Scope

VaultCord is a local-first tool handling sensitive Discord tokens and encrypted local message archives.

## Supported Version

- `main` (latest) is the supported release line for security fixes.

## Reporting a Vulnerability

Open a private security report through GitHub Security Advisories if possible.

If private reporting is not available, open an issue with minimal reproduction details and **do not include tokens, decrypted messages, or sensitive logs**.

## Security Principles

- Token must never be stored plaintext.
- Message payloads must never be stored plaintext.
- Logs must not contain tokens or decrypted message content.
- Vault data remains local by default; no telemetry.
