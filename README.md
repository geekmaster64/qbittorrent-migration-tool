# qBittorrent Migration Tool

Copy torrent **records** — the `.torrent` / magnet, category, tags, save path, and related metadata — from one [qBittorrent](https://www.qbittorrent.org/) instance to another using the Web API.

This is the tool you want when you stand up a new qBittorrent box (or container) and the files are already on disk, on a shared mount, or about to be. It does **not** copy the downloaded files themselves.

> **Not the same as [qbt-migrate](https://github.com/jslay88/qbt-migrate).** That project rewrites paths inside an existing client's `BT_backup` folder. This one talks to two running Web UIs and recreates torrents on the destination. The console script is named `qbt-migrate-instance` for that reason.

## What it does

```text
┌──────────────────────────┐         Web API          ┌──────────────────────────┐
│   Source qBittorrent     │  categories, tags, RSS,  │  Destination qBittorrent │
│                          │  exported .torrent files │                          │
│  BT_backup + metadata    │ ───────────────────────► │  same hashes, categories │
│  files already on disk   │                          │  pointed at dest paths   │
└──────────────────────────┘                          └──────────────────────────┘
        files stay put  ─── or are copied offline / NFS-mounted ───►  files already there
```

For each selected torrent the tool:

1. Creates missing **categories** (with save / download paths) and **tags** on the destination.
2. Optionally copies **RSS** folders/feeds/rules and **search plugins**.
3. Prefers a local `.torrent` from `--torrent-dir` (passkeys, BT_backup), then `torrents/export`, then the magnet URI.
4. Adds it on the destination with category, tags, save path, Auto TMM, sequential-download and first/last-piece flags, speed limits, share limits, and per-file priorities.
5. Skip-checks on dest (or `seedMode` on Web API 2.16+).
6. Skips torrents whose infohash is already present, and can resume from a JSON state file.
7. Optionally rewrites paths and slash style, syncs extra trackers, and pauses the torrent on the source so you do not double-seed.

## Requirements

- Python 3.9+
- Two qBittorrent instances with **Web UI enabled** (`Tools → Options → Web UI`)
- qBittorrent **4.5+** on the source recommended (`torrents/export` landed in v4.5.0 / Web API 2.8.14). Older sources can still be migrated via magnet links or `--torrent-dir`.
- The destination must be able to **see the files** at the (mapped) save path. Skip-checking is on by default; if the files are not there, qBittorrent will happily mark the torrent complete against empty disk. That is a foot-gun — use `--dry-run` first.

## Install

From a clone:

```bash
git clone https://github.com/geekmaster64/qbittorrent-migration-tool.git
cd qbittorrent-migration-tool
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

`pip install -e .` gives you both `python torrent_transfer.py` and the `qbt-migrate-instance` command. `pip install -r requirements.txt` is enough if you only want to run the script.

## Quick start

Always start with a dry run:

```bash
qbt-migrate-instance \
  --source-host 192.168.1.10:8080 \
  --source-user admin \
  --dest-host   192.168.1.20:8080 \
  --dest-user   admin \
  --dry-run
```

Passwords are prompted on a TTY. Prefer environment variables so they never land in your shell history:

```bash
export QBT_SOURCE_HOST=192.168.1.10:8080
export QBT_SOURCE_USERNAME=admin
export QBT_SOURCE_PASSWORD='...'
export QBT_DEST_HOST=192.168.1.20:8080
export QBT_DEST_USERNAME=admin
export QBT_DEST_PASSWORD='...'

qbt-migrate-instance --dry-run
qbt-migrate-instance --yes
```

See [`.env.example`](.env.example) for a copy-paste template. Do not commit a filled-in `.env`.

## Examples

Same storage, new client (typical NAS / *arr stack rebuild):

```bash
qbt-migrate-instance --yes
```

Files moved from `/data/torrents` to `/mnt/storage/torrents`:

```bash
qbt-migrate-instance \
  --path-map /data/torrents=/mnt/storage/torrents \
  --yes
```

Windows source, Linux destination:

```bash
qbt-migrate-instance \
  --path-map 'D:\Downloads=/data/torrents' \
  --slash-style posix \
  --yes
```

Private tracker (re-inject `.torrent` files that still have the passkey), resume a large run, pause source after each add:

```bash
qbt-migrate-instance \
  --torrent-dir /mnt/source/qBittorrent/BT_backup \
  --state-file .qbt-migrate-state.json \
  --pause-source \
  --yes
```

Copy RSS rules too, one category, stopped on dest:

```bash
qbt-migrate-instance \
  --category tv \
  --copy-rss \
  --add-stopped \
  --yes
```

First ten completed torrents only, useful as a smoke test:

```bash
qbt-migrate-instance --completed-only --limit 10 --dry-run
```

## Command-line options

| Flag | Purpose |
| --- | --- |
| `--source-host` / `--dest-host` | Web UI host (`host:port` or `https://host:port`) |
| `--source-user` / `--dest-user` | Web UI username |
| `--source-pass` / `--dest-pass` | Web UI password (env vars are safer) |
| `--torrent-dir` | Local folder of `.torrent` files, matched by infohash or filename |
| `--dry-run` | Log the plan; write nothing |
| `-y` / `--yes` | Skip the confirmation prompt |
| `--category`, `--tag`, `--hash` | Repeatable filters |
| `--completed-only` | Skip incomplete torrents |
| `--limit N` | Cap how many torrents are migrated after filters |
| `--path-map OLD=NEW` | Prefix rewrite for save/download/category/RSS paths; longest match wins |
| `--slash-style posix\|windows` | Normalize separators after path maps |
| `--copy-rss` | Copy RSS folders, feeds, and auto-download rules |
| `--copy-search-plugins` | Install search plugins from the source |
| `--no-sync-file-priorities` | Do not copy per-file priorities after add |
| `--no-skip-existing` | Try adding even if the hash is already on dest |
| `--no-skip-checking` | Force a hash check on dest instead of skip-checking / seedMode |
| `--add-stopped` | Add everything paused/stopped (default: match source) |
| `--update-categories` | Overwrite save paths on categories that already exist |
| `--no-sync-trackers` | Do not copy extra tracker URLs after add |
| `--pause-source` | Pause/stop the torrent on source after a successful add |
| `--state-file PATH` | JSON resume file; completed hashes are skipped next run |
| `--reset-state` | Ignore and overwrite `--state-file` |
| `--insecure` | Skip HTTPS certificate verification |
| `--timeout` | HTTP timeout in seconds (default 30) |
| `-v` / `-q` | Debug / warnings-only logging |

```bash
qbt-migrate-instance --help
```

## What is copied, and what is not

| Copied | Not copied |
| --- | --- |
| Categories (name, save path, download path) | Downloaded / seeding files |
| Tags | Application preferences (ports, Web UI, speed caps, encryption, …) |
| `.torrent` file (or magnet) | Cookie store / watched folders |
| Save path and download path (with optional rewrite) | Queue position |
| Category, tags, Auto TMM | Peer lists, stats, ratio *history* |
| Sequential download, first/last piece priority | Client identity / DHT node ID |
| Per-file priorities | |
| Upload / download limits | |
| Ratio and seeding-time limits | |
| Stopped vs running (and force-start) | |
| Extra tracker URLs | |
| RSS folders, feeds, and rules (`--copy-rss`) | |
| Search plugins (`--copy-search-plugins`) | |

The original script was named `transfer_torrents_and_settings` but it never copied application preferences. That is intentional now: blindly applying source `app/preferences` onto the destination would overwrite listen ports, Web UI settings, and other host-specific config.

## Safety notes

- **Files first, records second.** Copy or mount the data, *then* run this tool with skip-checking. If you skip-check against missing files, qBittorrent will report 100% complete until you force a recheck.
- **Do not seed the same torrent from two clients** on a private tracker. Verify the destination, then pause or remove the source (`--pause-source` helps). Running both is a good way to get banned.
- **Dry-run is cheap. Use it.** `--dry-run` still logs in and lists every category, tag, and torrent it would touch.
- **Credentials.** Prefer `QBT_*` environment variables over `--source-pass` / `--dest-pass`. Flags show up in `ps` and shell history.
- **HTTPS.** Default is to verify certificates. `--insecure` is for lab boxes with self-signed certs, not for the public internet.
- **RSS auto-download.** `--copy-rss` will recreate auto-download rules on dest. Run dest stopped or review rules before they fire.

## How this differs from the original script

The first commit was a 60-line proof of concept. It would not have worked on a typical setup:

- It looked for `{hash}.torrent` under the client's **default download directory**. qBittorrent stores those files in `BT_backup`, and the supported way to get them is `torrents/export`.
- Credentials were hardcoded in the source file, and importing the module started a transfer immediately (`if __name__` was missing).
- Categories were copied; tags, save paths, skip-checking, paused state, and duplicate detection were not.
- There was no CLI, dry-run, path mapping, or dependency pin.

This tree keeps the original filename so existing clones keep working, and also installs `qbt-migrate-instance`.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `Login failed` | Web UI enabled, username/password, and that the host includes the port. Some installs bind to `localhost` only. |
| `Could not connect` | Firewall, reverse proxy path, HTTP vs HTTPS. Try `http://host:8080` explicitly. |
| `Torrent file for … not found` (old script) | You are still on the original 60-line version. Use this rewrite; it exports via the API. |
| Dest torrent is 0% / missing files | Data is not at the mapped save path. Recheck the `--path-map` and the dest filesystem. |
| Dest torrent is 100% but files are wrong | Skip-checking assumed the files were already good. Recheck the torrent in the Web UI. |
| `Export failed` / magnet fallback | Source is older than 4.5, or the torrent has no metadata yet (`metaDL`). Use `--torrent-dir` or upgrade. |
| Private tracker "unregistered torrent" | Point `--torrent-dir` at `BT_backup` or a folder of `.torrent` files re-downloaded from the tracker (passkey intact). |
| Categories created but torrents land in the default folder | Auto TMM vs manual. The tool preserves the source `auto_tmm` flag. Check dest category save paths and `--path-map`. |
| HTTPS certificate errors | Use a real cert, or `--insecure` for a trusted LAN. |
| Long run died halfway | Re-run with the same `--state-file`. Completed hashes are skipped. |

qBittorrent 5.x renamed "paused" to "stopped". The client library maps both; `--add-stopped` covers either.

Web API 2.16+ replaced `skip_checking` with `seedMode`. The tool inspects the destination API version and sends the parameter that build expects.

## Development

```bash
pip install -e .
python -m unittest discover -s tests -v
python -m compileall torrent_transfer.py tests
qbt-migrate-instance --help
```

Unit tests and the fake Web API test do not need a live qBittorrent. To run two real clients in Docker:

```bash
docker compose -f docker-compose.test.yml up -d
QBT_LIVE=1 python tests/integration/test_live.py
docker compose -f docker-compose.test.yml down -v
```

See [tests/integration/README.md](tests/integration/README.md).

## Related tools

- [qbt-migrate](https://github.com/jslay88/qbt-migrate) — rewrite paths in `BT_backup` on a *single* instance (including Windows ↔ Linux slash conversion).
- [qbit_manage](https://github.com/StuffAnThings/qbit_manage) — ongoing category/tag/share-limit automation against one client.
- [qbittorrent-api](https://github.com/rmartin16/qbittorrent-api) — the Python Web API client this tool uses.

## License

[MIT](LICENSE)
