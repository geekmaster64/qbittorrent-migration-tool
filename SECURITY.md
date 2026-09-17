# Security

## Credentials

This tool needs Web UI usernames and passwords for both qBittorrent instances.

- Prefer `QBT_SOURCE_PASSWORD` / `QBT_DEST_PASSWORD` (or a prompted TTY) over
  `--source-pass` / `--dest-pass`. Command-line flags are visible in process
  lists and shell history.
- Never commit a filled-in `.env`, a patched `torrent_transfer.py` with
  passwords, or CI secrets in workflow files.
- Give the Web UI user only what it needs. A read/write Web UI login can add
  and pause torrents; it can also delete them.

## What this tool will not do

It will not copy application preferences (listen ports, Web UI config,
encryption, DynDNS, etc.) from source to destination. That is host-specific
and would be a surprising overwrite.

## Reporting a vulnerability

Open a private report on the GitHub repository, or contact the author via
the email on their GitHub profile. Please do not file a public issue that
includes credentials or tracker passkeys.
