# Changelog

## 0.3.0 — 2026-09-17

- Send `seedMode` on Web API 2.16+ (where `skip_checking` was removed).
- Optional RSS folder/feed/rule copy (`--copy-rss`) and search-plugin install (`--copy-search-plugins`).
- Copy per-file priorities after add.
- `--torrent-dir` matches local `.torrent` files by infohash or filename (private-tracker passkeys / BT_backup).
- `--state-file` JSON resume so a long run can continue after a failure.
- Packaged as `qbt-migrate-instance` (`pip install -e .`).
- Fake Web API integration test, plus optional Docker live test.

## 0.2.0 — 2026-09-17

Rewrite of the original 60-line proof of concept into a CLI that actually
talks to two qBittorrent Web UIs.

- Export torrents through `torrents/export` instead of looking for
  `{hash}.torrent` in the default download directory.
- Fall back to magnet URIs when export is unavailable.
- Copy tags as well as categories; optionally update existing category paths.
- Preserve save/download path, Auto TMM, tags, sequential download,
  first/last-piece priority, speed limits, share limits, and stopped state.
- Skip torrents whose infohash already exists on the destination.
- Skip hash checking by default (this is a migration, not a fresh download).
- CLI with `--dry-run`, filters, `--path-map`, `--slash-style`, confirmation
  prompt, and environment-variable credentials.
- Optional tracker sync and `--pause-source` to avoid double-seeding.
- Tests for path rewriting, filters, and argument parsing.

## 0.1.0 — 2025-09-22

Initial script. Logged into two clients and attempted to add torrents from
files on the source download path.
