#!/usr/bin/env python3
"""Live integration test against two qBittorrent Web UIs.

Expects instances already running (see docker-compose.test.yml):

    QBT_SOURCE_HOST  default 127.0.0.1:18080
    QBT_DEST_HOST    default 127.0.0.1:18081
    QBT_SOURCE_USERNAME / QBT_DEST_USERNAME  default admin
    QBT_SOURCE_PASSWORD / QBT_DEST_PASSWORD  default adminadmin

Skip unless QBT_LIVE=1 so `python -m unittest discover` stays offline.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torrent_transfer as tt
from tests.torrent_fixture import FILE_NAME, tiny_torrent_bytes

TINY_TORRENT = tiny_torrent_bytes()

LIVE = os.environ.get("QBT_LIVE") == "1"


def _wait_for(host: str, user: str, password: str, timeout: float = 90.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return tt.connect_client(
                host,
                user,
                password,
                verify_ssl=True,
                timeout=5,
                label=host,
            )
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise AssertionError(f"Timed out waiting for {host}: {last}")


@unittest.skipUnless(LIVE, "set QBT_LIVE=1 to run against real qBittorrent")
class LiveMigrationTests(unittest.TestCase):
    def test_category_and_torrent_roundtrip(self) -> None:
        source_host = os.environ.get("QBT_SOURCE_HOST", "127.0.0.1:18080")
        dest_host = os.environ.get("QBT_DEST_HOST", "127.0.0.1:18081")
        user = os.environ.get("QBT_SOURCE_USERNAME", "admin")
        password = os.environ.get("QBT_SOURCE_PASSWORD", "adminadmin")
        dest_user = os.environ.get("QBT_DEST_USERNAME", user)
        dest_password = os.environ.get("QBT_DEST_PASSWORD", password)

        source = _wait_for(source_host, user, password)
        dest = _wait_for(dest_host, dest_user, dest_password)

        try:
            source.torrent_categories.create_category(name="live-test", save_path="/downloads/live-test")
        except Exception:
            pass
        source.torrents_add(
            torrent_files=TINY_TORRENT,
            category="live-test",
            is_paused=True,
            is_stopped=True,
            is_skip_checking=True,
            save_path="/downloads/live-test",
        )

        code = tt.main(
            [
                "--source-host",
                source_host,
                "--source-user",
                user,
                "--source-pass",
                password,
                "--dest-host",
                dest_host,
                "--dest-user",
                dest_user,
                "--dest-pass",
                dest_password,
                "--category",
                "live-test",
                "--yes",
            ]
        )
        self.assertEqual(code, 0)
        dest_categories = dest.torrent_categories.categories
        self.assertIn("live-test", dest_categories)
        dest_names = {torrent.name for torrent in dest.torrents_info()}
        self.assertIn(FILE_NAME, dest_names)


if __name__ == "__main__":
    LIVE = True
    os.environ["QBT_LIVE"] = "1"
    unittest.main()
