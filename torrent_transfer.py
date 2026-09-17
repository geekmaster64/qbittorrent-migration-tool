#!/usr/bin/env python3
"""Migrate torrents, categories, tags, and RSS between qBittorrent instances.

This copies torrent *registrations* (the .torrent / magnet, category, tags,
save path, and related metadata) via the Web API. It does not copy the
downloaded files themselves. The destination client must already be able to
see those files at the mapped save path, typically via shared storage or an
offline copy.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__version__ = "0.3.0"

logger = logging.getLogger("qbt-migrate")

ENV_SOURCE_HOST = "QBT_SOURCE_HOST"
ENV_SOURCE_USER = "QBT_SOURCE_USERNAME"
ENV_SOURCE_PASS = "QBT_SOURCE_PASSWORD"
ENV_DEST_HOST = "QBT_DEST_HOST"
ENV_DEST_USER = "QBT_DEST_USERNAME"
ENV_DEST_PASS = "QBT_DEST_PASSWORD"

STOPPED_STATES = frozenset(
    {
        "pausedDL",
        "pausedUP",
        "stoppedDL",
        "stoppedUP",
    }
)

# torrents/add dropped skip_checking in Web API 2.16.0 in favor of seedMode.
SEED_MODE_API = (2, 16, 0)


class MigrationError(RuntimeError):
    """Raised when a migration cannot start."""


@dataclass
class Stats:
    categories_created: int = 0
    categories_updated: int = 0
    categories_skipped: int = 0
    categories_failed: int = 0
    tags_created: int = 0
    tags_skipped: int = 0
    rss_folders_created: int = 0
    rss_feeds_created: int = 0
    rss_rules_created: int = 0
    rss_skipped: int = 0
    rss_failed: int = 0
    plugins_installed: int = 0
    plugins_skipped: int = 0
    plugins_failed: int = 0
    torrents_added: int = 0
    torrents_skipped: int = 0
    torrents_failed: int = 0
    torrents_planned: int = 0
    failures: list[str] = field(default_factory=list)

    def record_failure(self, name: str, reason: str) -> None:
        self.torrents_failed += 1
        self.failures.append(f"{name}: {reason}")
        logger.error("Failed to add %s: %s", name, reason)


@dataclass
class MigrationState:
    """JSON resume file so a long run can continue after a failure."""

    completed: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None, *, reset: bool = False) -> MigrationState:
        if path is None:
            return cls()
        if reset or not path.exists():
            state = cls(path=path)
            if reset and path.exists():
                logger.info("Resetting state file %s", path)
            return state
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MigrationError(f"Could not read state file {path}: {exc}") from exc
        completed = {str(item).lower() for item in data.get("completed", [])}
        failed = {str(k).lower(): str(v) for k, v in dict(data.get("failed", {})).items()}
        logger.info("Loaded state file %s (%d completed)", path, len(completed))
        return cls(completed=completed, failed=failed, path=path)

    def mark_completed(self, torrent_hash: str) -> None:
        if not torrent_hash:
            return
        key = torrent_hash.lower()
        self.completed.add(key)
        self.failed.pop(key, None)
        self.save()

    def mark_failed(self, torrent_hash: str, reason: str) -> None:
        if torrent_hash:
            self.failed[torrent_hash.lower()] = reason
            self.save()

    def save(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": 1,
            "completed": sorted(self.completed),
            "failed": self.failed,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".qbt-migrate-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


@dataclass
class MigrateOptions:
    mappings: Sequence[tuple[str, str]]
    slash_style: str | None
    skip_checking: bool
    add_stopped: bool | None
    skip_existing: bool
    sync_trackers: bool
    sync_file_priorities: bool
    pause_source: bool
    dry_run: bool
    torrent_dir: Path | None
    state: MigrationState
    api_version: tuple[int, ...] = (2, 0, 0)


def parse_path_map(value: str) -> tuple[str, str]:
    """Parse a single OLD=NEW path mapping."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "Path map must be in OLD=NEW form, e.g. /data/torrents=/mnt/storage/torrents"
        )
    old, new = value.split("=", 1)
    if not old:
        raise argparse.ArgumentTypeError("Path map OLD side cannot be empty")
    return old, new


def rewrite_path(path: str | None, mappings: Sequence[tuple[str, str]]) -> str:
    """Rewrite a filesystem path using the longest matching prefix."""
    if not path:
        return path or ""
    if not mappings:
        return path
    for old, new in sorted(mappings, key=lambda item: len(item[0]), reverse=True):
        if path.startswith(old):
            return new + path[len(old) :]
    return path


def apply_slash_style(path: str, style: str | None) -> str:
    if not path or not style:
        return path
    if style == "posix":
        return path.replace("\\", "/")
    if style == "windows":
        return path.replace("/", "\\")
    return path


def mapped_path(
    path: str | None,
    mappings: Sequence[tuple[str, str]],
    slash_style: str | None,
) -> str:
    return apply_slash_style(rewrite_path(path, mappings), slash_style)


def split_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def torrent_is_stopped(torrent: Any) -> bool:
    state = str(getattr(torrent, "state", "") or "")
    if state in STOPPED_STATES:
        return True
    state_enum = getattr(torrent, "state_enum", None)
    if state_enum is not None:
        return bool(getattr(state_enum, "is_stopped", False) or getattr(state_enum, "is_paused", False))
    return False


def torrent_progress(torrent: Any) -> float:
    try:
        return float(getattr(torrent, "progress", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_api_version(raw: Any) -> tuple[int, ...]:
    text = str(raw or "0").lstrip("vV")
    parts: list[int] = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def client_api_version(client: Any) -> tuple[int, ...]:
    try:
        return parse_api_version(client.app.web_api_version)
    except Exception:
        return (2, 0, 0)


def filter_torrents(
    torrents: Sequence[Any],
    *,
    categories: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    hashes: Sequence[str] | None = None,
    completed_only: bool = False,
    limit: int | None = None,
    already_done: Sequence[str] | None = None,
) -> list[Any]:
    category_filter = {item.lower() for item in categories or () if item}
    tag_filter = {item.lower() for item in tags or () if item}
    hash_filter = {item.lower() for item in hashes or () if item}
    done = {item.lower() for item in already_done or () if item}

    selected: list[Any] = []
    for torrent in torrents:
        torrent_hash = str(getattr(torrent, "hash", "") or "").lower()
        if torrent_hash and torrent_hash in done:
            continue
        if hash_filter and torrent_hash not in hash_filter:
            continue
        if category_filter:
            category = str(getattr(torrent, "category", "") or "").lower()
            if category not in category_filter:
                continue
        if tag_filter:
            torrent_tags = {tag.lower() for tag in split_tags(getattr(torrent, "tags", None))}
            if torrent_tags.isdisjoint(tag_filter):
                continue
        if completed_only and torrent_progress(torrent) < 1.0:
            continue
        selected.append(torrent)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def _bencode_skip(data: bytes, index: int) -> int:
    if index >= len(data):
        raise ValueError("truncated bencode")
    marker = data[index : index + 1]
    if marker == b"i":
        end = data.find(b"e", index)
        if end < 0:
            raise ValueError("truncated integer")
        return end + 1
    if marker in (b"l", b"d"):
        index += 1
        while index < len(data) and data[index : index + 1] != b"e":
            index = _bencode_skip(data, index)
        if index >= len(data):
            raise ValueError("truncated list/dict")
        return index + 1
    colon = data.find(b":", index)
    if colon < 0:
        raise ValueError("truncated string")
    length = int(data[index:colon])
    return colon + 1 + length


def infohash_of_torrent_bytes(data: bytes) -> str | None:
    """SHA-1 infohash from a .torrent payload (v1)."""
    marker = data.find(b"4:info")
    if marker < 0:
        return None
    start = marker + 6
    try:
        end = _bencode_skip(data, start)
    except (ValueError, IndexError):
        return None
    return hashlib.sha1(data[start:end]).hexdigest()


def index_torrent_dir(torrent_dir: Path) -> dict[str, Path]:
    """Map infohash / filename stem -> local .torrent path."""
    index: dict[str, Path] = {}
    if not torrent_dir.is_dir():
        raise MigrationError(f"Torrent directory does not exist: {torrent_dir}")
    for path in torrent_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".torrent", ""} and path.suffix:
            continue
        stem = path.stem.lower()
        index.setdefault(stem, path)
        if path.suffix.lower() != ".torrent" and path.suffix:
            continue
        try:
            payload = path.read_bytes()
        except OSError:
            continue
        digest = infohash_of_torrent_bytes(payload)
        if digest:
            index.setdefault(digest.lower(), path)
    return index


def find_local_torrent(index: Mapping[str, Path], torrent: Any) -> bytes | None:
    for attr in ("hash", "infohash_v1", "infohash_v2"):
        value = getattr(torrent, attr, None)
        if not value:
            continue
        path = index.get(str(value).lower())
        if path is not None and path.is_file():
            logger.debug("Using local torrent file %s", path)
            return path.read_bytes()
    return None


def _qbittorrentapi():
    try:
        import qbittorrentapi
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'qbittorrent-api'.\n"
            "Install with:  pip install -r requirements.txt"
        ) from exc
    return qbittorrentapi


def connect_client(
    host: str,
    username: str,
    password: str,
    *,
    verify_ssl: bool,
    timeout: int,
    label: str,
):
    qbittorrentapi = _qbittorrentapi()
    client = qbittorrentapi.Client(
        host=host,
        username=username or None,
        password=password or None,
        VERIFY_WEBUI_CERTIFICATE=verify_ssl,
        REQUESTS_ARGS={"timeout": timeout},
    )
    try:
        client.auth_log_in()
    except qbittorrentapi.LoginFailed as exc:
        raise MigrationError(f"Login failed for {label} ({host}): {exc}") from exc
    except qbittorrentapi.APIConnectionError as exc:
        raise MigrationError(f"Could not connect to {label} ({host}): {exc}") from exc
    version = client.app.version
    api_version = client.app.web_api_version
    logger.info("Connected to %s (%s)  qBittorrent %s  Web API %s", label, host, version, api_version)
    return client


def category_field(category: Any, *names: str) -> str:
    for name in names:
        if hasattr(category, name):
            value = getattr(category, name)
            if value:
                return str(value)
        if isinstance(category, dict) and category.get(name):
            return str(category[name])
    return ""


def migrate_categories(
    source,
    dest,
    *,
    mappings: Sequence[tuple[str, str]],
    slash_style: str | None,
    update_existing: bool,
    dry_run: bool,
    stats: Stats,
) -> None:
    try:
        source_categories = dict(source.torrent_categories.categories)
        dest_categories = dict(dest.torrent_categories.categories)
    except Exception as exc:
        logger.error("Could not read categories: %s", exc)
        return

    for name, data in source_categories.items():
        save_path = mapped_path(
            category_field(data, "save_path", "savePath"),
            mappings,
            slash_style,
        )
        download_path = mapped_path(
            category_field(data, "download_path", "downloadPath") or None,
            mappings,
            slash_style,
        )
        exists = name in dest_categories
        if exists and not update_existing:
            stats.categories_skipped += 1
            logger.debug("Category already exists, skipping: %s", name)
            continue
        action = "update" if exists else "create"
        logger.info(
            "%s category %s (save_path=%s%s)",
            "Would " + action if dry_run else action.capitalize(),
            name,
            save_path or "(default)",
            f", download_path={download_path}" if download_path else "",
        )
        if dry_run:
            if exists:
                stats.categories_updated += 1
            else:
                stats.categories_created += 1
            continue
        try:
            kwargs: dict[str, Any] = {"name": name, "save_path": save_path}
            if download_path:
                kwargs["download_path"] = download_path
            if exists:
                dest.torrent_categories.edit_category(**kwargs)
                stats.categories_updated += 1
            else:
                dest.torrent_categories.create_category(**kwargs)
                stats.categories_created += 1
        except Exception as exc:
            stats.categories_failed += 1
            logger.error("Failed to %s category %s: %s", action, name, exc)


def migrate_tags(source, dest, *, dry_run: bool, stats: Stats) -> None:
    try:
        source_tags = [str(tag) for tag in source.torrent_tags.tags]
        dest_tags = {str(tag) for tag in dest.torrent_tags.tags}
    except Exception as exc:
        logger.warning("Could not read tags (older qBittorrent?): %s", exc)
        return

    missing = [tag for tag in source_tags if tag not in dest_tags]
    stats.tags_skipped += len(source_tags) - len(missing)
    if not missing:
        logger.info("No new tags to copy.")
        return
    logger.info(
        "%s %d tag(s): %s",
        "Would create" if dry_run else "Creating",
        len(missing),
        ", ".join(missing),
    )
    if dry_run:
        stats.tags_created += len(missing)
        return
    try:
        dest.torrent_tags.create_tags(tags=missing)
        stats.tags_created += len(missing)
    except Exception as exc:
        logger.error("Failed to create tags: %s", exc)


def flatten_rss_items(items: Mapping[str, Any] | None, prefix: str = "") -> tuple[list[str], list[tuple[str, str]]]:
    """Return (folder_paths, [(feed_path, url), ...]) using qBittorrent's '\\' paths."""
    folders: list[str] = []
    feeds: list[tuple[str, str]] = []
    if not items:
        return folders, feeds
    for name, value in items.items():
        path = f"{prefix}\\{name}" if prefix else str(name)
        if isinstance(value, Mapping) and value.get("url"):
            feeds.append((path, str(value["url"])))
        elif isinstance(value, Mapping):
            folders.append(path)
            child_folders, child_feeds = flatten_rss_items(value, path)
            folders.extend(child_folders)
            feeds.extend(child_feeds)
    return folders, feeds


def migrate_rss(
    source,
    dest,
    *,
    mappings: Sequence[tuple[str, str]],
    slash_style: str | None,
    dry_run: bool,
    stats: Stats,
) -> None:
    try:
        source_items = source.rss_items(include_feed_data=False)
        dest_items = dest.rss_items(include_feed_data=False)
        source_rules = dict(source.rss_rules())
        dest_rules = dict(dest.rss_rules())
    except Exception as exc:
        logger.warning("Could not read RSS (disabled or old qBittorrent?): %s", exc)
        return

    src_folders, src_feeds = flatten_rss_items(source_items)
    dest_folders, dest_feeds = flatten_rss_items(dest_items)
    dest_folder_set = set(dest_folders)
    dest_feed_paths = {path for path, _url in dest_feeds}
    dest_feed_urls = {url for _path, url in dest_feeds}

    for folder in sorted(src_folders, key=lambda item: item.count("\\")):
        if folder in dest_folder_set:
            stats.rss_skipped += 1
            continue
        logger.info("%s RSS folder %s", "Would create" if dry_run else "Creating", folder)
        if dry_run:
            stats.rss_folders_created += 1
            dest_folder_set.add(folder)
            continue
        try:
            dest.rss_add_folder(folder_path=folder)
            stats.rss_folders_created += 1
            dest_folder_set.add(folder)
        except Exception as exc:
            stats.rss_failed += 1
            logger.error("Failed to create RSS folder %s: %s", folder, exc)

    for path, url in src_feeds:
        if path in dest_feed_paths or url in dest_feed_urls:
            stats.rss_skipped += 1
            continue
        logger.info("%s RSS feed %s (%s)", "Would add" if dry_run else "Adding", path, url)
        if dry_run:
            stats.rss_feeds_created += 1
            continue
        try:
            dest.rss_add_feed(url=url, item_path=path)
            stats.rss_feeds_created += 1
            dest_feed_paths.add(path)
            dest_feed_urls.add(url)
        except Exception as exc:
            stats.rss_failed += 1
            logger.error("Failed to add RSS feed %s: %s", path, exc)

    for name, rule_def in source_rules.items():
        if name in dest_rules:
            stats.rss_skipped += 1
            continue
        payload = dict(rule_def)
        if payload.get("savePath"):
            payload["savePath"] = mapped_path(str(payload["savePath"]), mappings, slash_style)
        logger.info("%s RSS rule %s", "Would create" if dry_run else "Creating", name)
        if dry_run:
            stats.rss_rules_created += 1
            continue
        try:
            dest.rss_set_rule(rule_name=name, rule_def=payload)
            stats.rss_rules_created += 1
        except Exception as exc:
            stats.rss_failed += 1
            logger.error("Failed to create RSS rule %s: %s", name, exc)


def migrate_search_plugins(source, dest, *, dry_run: bool, stats: Stats) -> None:
    try:
        source_plugins = list(source.search_plugins())
        dest_plugins = list(dest.search_plugins())
    except Exception as exc:
        logger.warning("Could not read search plugins: %s", exc)
        return

    dest_names = {str(getattr(plugin, "name", "")).lower() for plugin in dest_plugins}
    dest_urls = {str(getattr(plugin, "url", "")).lower() for plugin in dest_plugins if getattr(plugin, "url", None)}

    for plugin in source_plugins:
        name = str(getattr(plugin, "name", "") or "")
        url = str(getattr(plugin, "url", "") or "")
        if not url or url.lower() in dest_urls or name.lower() in dest_names:
            stats.plugins_skipped += 1
            continue
        logger.info("%s search plugin %s (%s)", "Would install" if dry_run else "Installing", name or url, url)
        if dry_run:
            stats.plugins_installed += 1
            continue
        try:
            dest.search_install_plugin(sources=url)
            stats.plugins_installed += 1
            dest_names.add(name.lower())
            dest_urls.add(url.lower())
        except Exception as exc:
            stats.plugins_failed += 1
            logger.error("Failed to install search plugin %s: %s", name or url, exc)


def export_torrent_payload(source, torrent: Any) -> tuple[bytes | None, str | None]:
    """Return (torrent_bytes, magnet_uri). Prefer a .torrent export."""
    torrent_hash = getattr(torrent, "hash", None)
    magnet = getattr(torrent, "magnet_uri", None) or None
    if torrent_hash:
        try:
            payload = source.torrents_export(torrent_hash=torrent_hash)
            if payload:
                return payload, magnet
        except Exception as exc:
            logger.debug("Export failed for %s (%s): %s", torrent.name, torrent_hash, exc)
    return None, magnet


def wait_for_torrent(client, torrent_hash: str, timeout: float = 20.0) -> Any | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            found = client.torrents_info(torrent_hashes=torrent_hash)
            if found:
                return found[0]
        except Exception:
            pass
        time.sleep(0.4)
    return None


def real_tracker_urls(client, torrent_hash: str) -> list[str]:
    urls: list[str] = []
    try:
        for tracker in client.torrents_trackers(torrent_hash):
            tier = getattr(tracker, "tier", 0)
            try:
                if int(tier) < 0:
                    continue
            except (TypeError, ValueError):
                pass
            url = getattr(tracker, "url", None) or (tracker.get("url") if isinstance(tracker, dict) else None)
            if url:
                urls.append(str(url))
    except Exception as exc:
        logger.debug("Could not list trackers for %s: %s", torrent_hash, exc)
    return urls


def sync_trackers(source, dest, torrent_hash: str) -> None:
    source_urls = real_tracker_urls(source, torrent_hash)
    if not source_urls:
        return
    dest_torrent = wait_for_torrent(dest, torrent_hash)
    if dest_torrent is None:
        logger.debug("Torrent %s not visible on destination yet; skipping tracker sync", torrent_hash)
        return
    dest_urls = set(real_tracker_urls(dest, torrent_hash))
    missing = [url for url in source_urls if url not in dest_urls]
    if missing:
        dest.torrents_add_trackers(torrent_hash=torrent_hash, urls=missing)
        logger.debug("Added %d tracker(s) to %s", len(missing), torrent_hash)


def sync_file_priorities(source, dest, torrent_hash: str) -> None:
    try:
        files = list(source.torrents_files(torrent_hash=torrent_hash))
    except Exception as exc:
        logger.debug("Could not list files for %s: %s", torrent_hash, exc)
        return
    if not files:
        return
    if wait_for_torrent(dest, torrent_hash) is None:
        return
    by_priority: dict[int, list[int]] = {}
    for entry in files:
        try:
            index = int(getattr(entry, "index"))
            priority = int(getattr(entry, "priority"))
        except (TypeError, ValueError, AttributeError):
            continue
        by_priority.setdefault(priority, []).append(index)
    for priority, file_ids in by_priority.items():
        try:
            dest.torrents_file_priority(torrent_hash=torrent_hash, file_ids=file_ids, priority=priority)
        except Exception as exc:
            logger.debug("Could not set file priority %s on %s: %s", priority, torrent_hash, exc)


def add_kwargs_for_torrent(
    torrent: Any,
    *,
    save_path: str,
    download_path: str,
    skip_checking: bool,
    add_stopped: bool,
    api_version: tuple[int, ...] = (2, 8, 0),
) -> dict[str, Any]:
    tags = split_tags(getattr(torrent, "tags", None))
    category = getattr(torrent, "category", None) or None
    auto_tmm = bool(getattr(torrent, "auto_tmm", False))
    kwargs: dict[str, Any] = {
        "category": category,
        "tags": tags or None,
        "is_paused": add_stopped,
        "is_stopped": add_stopped,
        "use_auto_torrent_management": auto_tmm,
        "is_sequential_download": bool(getattr(torrent, "seq_dl", False)),
        "is_first_last_piece_priority": bool(getattr(torrent, "f_l_piece_prio", False)),
        "forced": bool(getattr(torrent, "force_start", False)) and not add_stopped,
    }
    if skip_checking:
        if api_version >= SEED_MODE_API:
            kwargs["seedMode"] = True
        else:
            kwargs["is_skip_checking"] = True
    else:
        kwargs["is_skip_checking"] = False
    if not auto_tmm:
        if save_path:
            kwargs["save_path"] = save_path
        if download_path:
            kwargs["download_path"] = download_path
            kwargs["use_download_path"] = True

    up_limit = getattr(torrent, "up_limit", None)
    dl_limit = getattr(torrent, "dl_limit", None)
    if isinstance(up_limit, int) and up_limit > 0:
        kwargs["upload_limit"] = up_limit
    if isinstance(dl_limit, int) and dl_limit > 0:
        kwargs["download_limit"] = dl_limit

    ratio_limit = getattr(torrent, "ratio_limit", None)
    if ratio_limit not in (None, -1, -2):
        kwargs["ratio_limit"] = ratio_limit
    seeding_time_limit = getattr(torrent, "seeding_time_limit", None)
    if seeding_time_limit not in (None, -1, -2):
        kwargs["seeding_time_limit"] = seeding_time_limit
    return kwargs


def migrate_torrents(
    source,
    dest,
    torrents: Sequence[Any],
    options: MigrateOptions,
    stats: Stats,
) -> None:
    dest_hashes = {str(t.hash).lower() for t in dest.torrents_info()}
    qbittorrentapi = _qbittorrentapi()
    local_index: dict[str, Path] = {}
    if options.torrent_dir is not None:
        local_index = index_torrent_dir(options.torrent_dir)
        logger.info("Indexed %d local torrent file(s) from %s", len(local_index), options.torrent_dir)

    for index, torrent in enumerate(torrents, start=1):
        name = getattr(torrent, "name", "(unnamed)")
        torrent_hash = str(getattr(torrent, "hash", "") or "")
        prefix = f"[{index}/{len(torrents)}]"
        if torrent_hash.lower() in options.state.completed:
            stats.torrents_skipped += 1
            logger.info("%s Skipping (state file): %s", prefix, name)
            continue
        if options.skip_existing and torrent_hash.lower() in dest_hashes:
            stats.torrents_skipped += 1
            options.state.mark_completed(torrent_hash)
            logger.info("%s Skipping existing torrent: %s", prefix, name)
            continue

        save_path = mapped_path(getattr(torrent, "save_path", None), options.mappings, options.slash_style)
        download_path = mapped_path(getattr(torrent, "download_path", None) or "", options.mappings, options.slash_style)
        stopped = torrent_is_stopped(torrent) if options.add_stopped is None else options.add_stopped
        kwargs = add_kwargs_for_torrent(
            torrent,
            save_path=save_path,
            download_path=download_path,
            skip_checking=options.skip_checking,
            add_stopped=stopped,
            api_version=options.api_version,
        )

        logger.info(
            "%s %s %s  hash=%s  category=%s  save_path=%s  stopped=%s",
            prefix,
            "Would add" if options.dry_run else "Adding",
            name,
            torrent_hash[:8] or "?",
            kwargs.get("category") or "(none)",
            save_path or "(default)",
            stopped,
        )
        if options.dry_run:
            stats.torrents_planned += 1
            continue

        payload = find_local_torrent(local_index, torrent)
        magnet = None
        if payload is None:
            payload, magnet = export_torrent_payload(source, torrent)
        try:
            if payload:
                result = dest.torrents_add(torrent_files=payload, **kwargs)
            elif magnet:
                logger.warning("No .torrent file for %s; adding via magnet", name)
                result = dest.torrents_add(urls=magnet, **kwargs)
            else:
                stats.record_failure(name, "no .torrent export, local file, or magnet URI")
                options.state.mark_failed(torrent_hash, "no torrent payload")
                continue
        except qbittorrentapi.Conflict409Error:
            stats.torrents_skipped += 1
            options.state.mark_completed(torrent_hash)
            logger.info("%s Already present on destination: %s", prefix, name)
            dest_hashes.add(torrent_hash.lower())
            continue
        except Exception as exc:
            stats.record_failure(name, str(exc))
            options.state.mark_failed(torrent_hash, str(exc))
            continue

        if isinstance(result, str) and result.strip().lower().startswith("fail"):
            stats.record_failure(name, result.strip())
            options.state.mark_failed(torrent_hash, result.strip())
            continue

        stats.torrents_added += 1
        dest_hashes.add(torrent_hash.lower())
        options.state.mark_completed(torrent_hash)

        if options.sync_trackers and torrent_hash:
            try:
                sync_trackers(source, dest, torrent_hash)
            except Exception as exc:
                logger.warning("Tracker sync failed for %s: %s", name, exc)

        if options.sync_file_priorities and torrent_hash:
            try:
                sync_file_priorities(source, dest, torrent_hash)
            except Exception as exc:
                logger.warning("File priority sync failed for %s: %s", name, exc)

        if options.pause_source and torrent_hash:
            try:
                if hasattr(source.torrents, "stop"):
                    source.torrents_stop(torrent_hashes=torrent_hash)
                else:
                    source.torrents_pause(torrent_hashes=torrent_hash)
                logger.info("%s Paused/stopped on source: %s", prefix, name)
            except Exception as exc:
                logger.warning("Could not pause %s on source: %s", name, exc)


def print_summary(stats: Stats, dry_run: bool) -> None:
    title = "Dry run summary" if dry_run else "Migration summary"
    logger.info("%s", title)
    logger.info("  Categories created: %d", stats.categories_created)
    logger.info("  Categories updated: %d", stats.categories_updated)
    logger.info("  Categories skipped: %d", stats.categories_skipped)
    logger.info("  Categories failed:  %d", stats.categories_failed)
    logger.info("  Tags created:       %d", stats.tags_created)
    logger.info("  Tags skipped:       %d", stats.tags_skipped)
    logger.info("  RSS folders:        %d", stats.rss_folders_created)
    logger.info("  RSS feeds:          %d", stats.rss_feeds_created)
    logger.info("  RSS rules:          %d", stats.rss_rules_created)
    logger.info("  RSS skipped:        %d", stats.rss_skipped)
    logger.info("  RSS failed:         %d", stats.rss_failed)
    logger.info("  Search plugins:     %d", stats.plugins_installed)
    if dry_run:
        logger.info("  Torrents planned:   %d", stats.torrents_planned)
    else:
        logger.info("  Torrents added:     %d", stats.torrents_added)
    logger.info("  Torrents skipped:   %d", stats.torrents_skipped)
    logger.info("  Torrents failed:    %d", stats.torrents_failed)
    if stats.failures:
        logger.info("Failures:")
        for item in stats.failures:
            logger.info("  - %s", item)


def env_or_none(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def prompt_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return ""


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Hide uninformative defaults such as None or empty lists."""

    def _get_help_string(self, action: argparse.Action) -> str:
        if action.default in (None, [], "") or action.default is argparse.SUPPRESS:
            return action.help or ""
        return super()._get_help_string(action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qbt-migrate-instance",
        description="Migrate torrents, categories, tags, and RSS from one qBittorrent instance to another.",
        epilog=(
            "Credentials may also be supplied via QBT_SOURCE_HOST, QBT_SOURCE_USERNAME, "
            "QBT_SOURCE_PASSWORD, QBT_DEST_HOST, QBT_DEST_USERNAME, and QBT_DEST_PASSWORD. "
            "This tool copies torrent records, not the downloaded files."
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    source = parser.add_argument_group("source")
    source.add_argument("--source-host", default=env_or_none(ENV_SOURCE_HOST), help="Source Web UI host, e.g. 192.168.1.10:8080")
    source.add_argument("--source-user", default=env_or_none(ENV_SOURCE_USER) or "", help="Source Web UI username")
    source.add_argument("--source-pass", default=env_or_none(ENV_SOURCE_PASS), help="Source Web UI password (prefer the env var)")
    source.add_argument(
        "--torrent-dir",
        type=Path,
        default=None,
        help="Local folder of .torrent files (BT_backup or tracker re-downloads). Matched by infohash or filename.",
    )

    dest = parser.add_argument_group("destination")
    dest.add_argument("--dest-host", default=env_or_none(ENV_DEST_HOST), help="Destination Web UI host, e.g. 192.168.1.20:8080")
    dest.add_argument("--dest-user", default=env_or_none(ENV_DEST_USER) or "", help="Destination Web UI username")
    dest.add_argument("--dest-pass", default=env_or_none(ENV_DEST_PASS), help="Destination Web UI password (prefer the env var)")

    select = parser.add_argument_group("selection")
    select.add_argument("--category", action="append", default=[], help="Only migrate this category (repeatable)")
    select.add_argument("--tag", action="append", default=[], help="Only migrate torrents that have this tag (repeatable)")
    select.add_argument(
        "--hash",
        action="append",
        default=[],
        dest="hashes",
        metavar="HASH",
        help="Only migrate this infohash (repeatable)",
    )
    select.add_argument("--completed-only", action="store_true", help="Skip torrents that are not 100%% complete")
    select.add_argument("--limit", type=int, default=None, help="Migrate at most N torrents (after filters)")

    paths = parser.add_argument_group("paths")
    paths.add_argument(
        "--path-map",
        action="append",
        default=[],
        type=parse_path_map,
        metavar="OLD=NEW",
        help="Rewrite save/download paths that start with OLD to NEW (repeatable; longest prefix wins)",
    )
    paths.add_argument(
        "--slash-style",
        choices=("posix", "windows"),
        default=None,
        help="Normalize path separators after applying --path-map",
    )

    extras = parser.add_argument_group("extras")
    extras.add_argument("--copy-rss", action="store_true", help="Copy RSS folders, feeds, and auto-download rules")
    extras.add_argument("--copy-search-plugins", action="store_true", help="Install search plugins from the source onto dest")
    extras.add_argument("--no-sync-file-priorities", action="store_true", help="Do not copy per-file priorities after add")

    behavior = parser.add_argument_group("behavior")
    behavior.add_argument("--dry-run", action="store_true", help="Log what would happen without writing to the destination")
    behavior.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation")
    behavior.add_argument("--no-skip-existing", action="store_true", help="Try to add torrents even if the hash already exists on dest")
    behavior.add_argument("--no-skip-checking", action="store_true", help="Hash-check files on dest instead of skipping verification")
    behavior.add_argument(
        "--add-stopped",
        action="store_true",
        default=None,
        help="Add every torrent stopped/paused (if omitted, match the source state)",
    )
    behavior.add_argument("--update-categories", action="store_true", help="Update save paths on categories that already exist")
    behavior.add_argument("--no-sync-trackers", action="store_true", help="Do not copy extra tracker URLs after add")
    behavior.add_argument(
        "--pause-source",
        action="store_true",
        help="After a successful add, pause/stop the torrent on the source (avoids double-seeding)",
    )
    behavior.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="JSON resume file; completed hashes are skipped on the next run",
    )
    behavior.add_argument("--reset-state", action="store_true", help="Ignore and overwrite --state-file")
    behavior.add_argument("--insecure", action="store_true", help="Do not verify HTTPS certificates")
    behavior.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")

    logging_group = parser.add_argument_group("logging")
    logging_group.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    logging_group.add_argument("-q", "--quiet", action="store_true", help="Warnings and errors only")
    return parser


def configure_logging(verbose: bool, quiet: bool) -> None:
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")


def resolve_password(value: str | None, *, host: str, role: str) -> str:
    if value is not None:
        return value
    return prompt_secret(f"{role} password for {host}: ")


def confirm(message: str) -> bool:
    if not sys.stdin.isatty():
        logger.error("Refusing to run without --yes when stdin is not a TTY")
        return False
    answer = input(f"{message} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def run(args: argparse.Namespace) -> int:
    configure_logging(args.verbose, args.quiet)

    if not args.source_host:
        logger.error("Missing source host. Pass --source-host or set %s.", ENV_SOURCE_HOST)
        return 2
    if not args.dest_host:
        logger.error("Missing destination host. Pass --dest-host or set %s.", ENV_DEST_HOST)
        return 2
    if args.source_host == args.dest_host and args.source_user == args.dest_user:
        logger.error("Source and destination look identical. Refusing to continue.")
        return 2

    try:
        state = MigrationState.load(args.state_file, reset=args.reset_state)
    except MigrationError as exc:
        logger.error("%s", exc)
        return 2

    source_pass = resolve_password(args.source_pass, host=args.source_host, role="Source")
    dest_pass = resolve_password(args.dest_pass, host=args.dest_host, role="Destination")

    try:
        source = connect_client(
            args.source_host,
            args.source_user,
            source_pass,
            verify_ssl=not args.insecure,
            timeout=args.timeout,
            label="source",
        )
        dest = connect_client(
            args.dest_host,
            args.dest_user,
            dest_pass,
            verify_ssl=not args.insecure,
            timeout=args.timeout,
            label="destination",
        )
    except MigrationError as exc:
        logger.error("%s", exc)
        return 1

    stats = Stats()
    mappings = list(args.path_map)

    migrate_categories(
        source,
        dest,
        mappings=mappings,
        slash_style=args.slash_style,
        update_existing=args.update_categories,
        dry_run=args.dry_run,
        stats=stats,
    )
    migrate_tags(source, dest, dry_run=args.dry_run, stats=stats)
    if args.copy_rss:
        migrate_rss(
            source,
            dest,
            mappings=mappings,
            slash_style=args.slash_style,
            dry_run=args.dry_run,
            stats=stats,
        )
    if args.copy_search_plugins:
        migrate_search_plugins(source, dest, dry_run=args.dry_run, stats=stats)

    source_torrents = source.torrents_info()
    selected = filter_torrents(
        source_torrents,
        categories=args.category,
        tags=args.tag,
        hashes=args.hashes,
        completed_only=args.completed_only,
        limit=args.limit,
        already_done=state.completed,
    )
    logger.info(
        "Source torrents: %d  selected: %d  already in state file: %d",
        len(source_torrents),
        len(selected),
        len(state.completed),
    )

    if not args.dry_run and not args.yes:
        extra = []
        if args.copy_rss:
            extra.append("RSS")
        if args.copy_search_plugins:
            extra.append("search plugins")
        suffix = f" plus {', '.join(extra)}" if extra else ""
        if not confirm(f"Migrate {len(selected)} torrent(s){suffix} from {args.source_host} to {args.dest_host}?"):
            logger.info("Aborted.")
            return 0

    options = MigrateOptions(
        mappings=mappings,
        slash_style=args.slash_style,
        skip_checking=not args.no_skip_checking,
        add_stopped=True if args.add_stopped else None,
        skip_existing=not args.no_skip_existing,
        sync_trackers=not args.no_sync_trackers,
        sync_file_priorities=not args.no_sync_file_priorities,
        pause_source=args.pause_source,
        dry_run=args.dry_run,
        torrent_dir=args.torrent_dir,
        state=state,
        api_version=client_api_version(dest),
    )
    migrate_torrents(source, dest, selected, options, stats)
    print_summary(stats, args.dry_run)

    try:
        source.auth_log_out()
        dest.auth_log_out()
    except Exception:
        pass

    failed = stats.torrents_failed or stats.categories_failed or stats.rss_failed or stats.plugins_failed
    return 1 if failed else 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
