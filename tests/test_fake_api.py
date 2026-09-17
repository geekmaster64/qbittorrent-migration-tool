#!/usr/bin/env python3
"""HTTP-level integration test against a fake qBittorrent Web API."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import qbittorrentapi  # noqa: F401
except ImportError:
    qbittorrentapi = None  # type: ignore[misc, assignment]

import torrent_transfer as tt


TINY_INFO = b"d4:name4:tiny12:piece lengthi16e6:pieces0:6:lengthi0ee"
TINY_TORRENT = b"d8:announce0:4:info" + TINY_INFO + b"e"
TINY_HASH = tt.infohash_of_torrent_bytes(TINY_TORRENT) or "abc123"


class FakeQbtState:
    def __init__(self) -> None:
        self.categories: dict[str, dict[str, str]] = {
            "tv": {"name": "tv", "save_path": "/data/tv"},
        }
        self.tags = ["hd"]
        self.torrents = [
            {
                "hash": TINY_HASH,
                "name": "tiny",
                "category": "tv",
                "tags": "hd",
                "save_path": "/data/tv",
                "download_path": "",
                "auto_tmm": False,
                "seq_dl": False,
                "f_l_piece_prio": False,
                "force_start": False,
                "up_limit": -1,
                "dl_limit": -1,
                "ratio_limit": -2,
                "seeding_time_limit": -2,
                "progress": 1.0,
                "state": "pausedUP",
                "magnet_uri": f"magnet:?xt=urn:btih:{TINY_HASH}",
            }
        ]
        self.added: list[str] = []
        self.rss_items: dict = {}
        self.rss_rules: dict = {}


def make_handler(state: FakeQbtState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            return

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(length) if length else b""

        def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, code: int, text: str) -> None:
            self._send(code, text.encode(), "text/plain")

        def _dispatch(self, path: str, form: dict[str, str]) -> None:
            if path.endswith("/auth/login"):
                self.send_response(200)
                self.send_header("Set-Cookie", "SID=test-session; Path=/")
                self.send_header("Content-Type", "text/plain")
                payload = b"Ok."
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path.endswith("/auth/logout"):
                self._send_text(200, "Ok.")
                return
            if path.endswith("/app/version"):
                self._send_text(200, "v4.6.7")
                return
            if path.endswith("/app/webapiVersion"):
                self._send_text(200, "2.8.19")
                return
            if path.endswith("/torrents/info"):
                self._send(200, json.dumps(state.torrents).encode())
                return
            if path.endswith("/torrents/categories"):
                self._send(200, json.dumps(state.categories).encode())
                return
            if path.endswith("/torrents/tags"):
                self._send(200, json.dumps(state.tags).encode())
                return
            if path.endswith("/torrents/export"):
                self._send(200, TINY_TORRENT, "application/x-bittorrent")
                return
            if path.endswith("/torrents/trackers"):
                self._send(200, json.dumps([]).encode())
                return
            if path.endswith("/torrents/files"):
                self._send(200, json.dumps([]).encode())
                return
            if path.endswith("/rss/items"):
                self._send(200, json.dumps(state.rss_items).encode())
                return
            if path.endswith("/rss/rules"):
                self._send(200, json.dumps(state.rss_rules).encode())
                return
            if path.endswith("/search/plugins"):
                self._send(200, json.dumps([]).encode())
                return
            if path.endswith("/torrents/createCategory"):
                name = form.get("category") or form.get("name") or ""
                save_path = form.get("savePath") or form.get("save_path") or ""
                state.categories[name] = {"name": name, "save_path": save_path}
                self._send_text(200, "Ok.")
                return
            if path.endswith("/torrents/createTags"):
                self._send_text(200, "Ok.")
                return
            if path.endswith("/torrents/add"):
                state.added.append("ok")
                if not any(item.get("hash") == TINY_HASH for item in state.torrents):
                    state.torrents.append(
                        {
                            "hash": TINY_HASH,
                            "name": "tiny",
                            "category": "tv",
                            "tags": "hd",
                            "save_path": "/data/tv",
                            "progress": 1.0,
                            "state": "pausedUP",
                        }
                    )
                self._send_text(200, "Ok.")
                return
            if path.endswith("/torrents/addTrackers"):
                self._send_text(200, "Ok.")
                return
            if path.endswith("/torrents/pause") or path.endswith("/torrents/stop"):
                self._send_text(200, "Ok.")
                return
            if path.endswith("/torrents/filePrio"):
                self._send_text(200, "Ok.")
                return
            self._send_text(404, f"missing {path}")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            body = self._read_body()
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            form.update({k: v[0] for k, v in parse_qs(parsed.query).items()})
            self._dispatch(parsed.path, form)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            form = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            self._dispatch(parsed.path, form)

    return Handler


@unittest.skipIf(qbittorrentapi is None, "qbittorrent-api is not installed")
class FakeApiMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_state = FakeQbtState()
        self.dest_state = FakeQbtState()
        self.dest_state.categories = {}
        self.dest_state.tags = []
        self.dest_state.torrents = []
        self.source_httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.source_state))
        self.dest_httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.dest_state))
        self.source_thread = threading.Thread(target=self.source_httpd.serve_forever, daemon=True)
        self.dest_thread = threading.Thread(target=self.dest_httpd.serve_forever, daemon=True)
        self.source_thread.start()
        self.dest_thread.start()

    def tearDown(self) -> None:
        self.source_httpd.shutdown()
        self.dest_httpd.shutdown()
        self.source_httpd.server_close()
        self.dest_httpd.server_close()

    def test_migrates_category_and_torrent(self) -> None:
        source_host = f"127.0.0.1:{self.source_httpd.server_address[1]}"
        dest_host = f"127.0.0.1:{self.dest_httpd.server_address[1]}"
        code = tt.main(
            [
                "--source-host",
                source_host,
                "--source-user",
                "admin",
                "--source-pass",
                "adminadmin",
                "--dest-host",
                dest_host,
                "--dest-user",
                "admin",
                "--dest-pass",
                "adminadmin",
                "--yes",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("tv", self.dest_state.categories)
        self.assertTrue(self.dest_state.added)


if __name__ == "__main__":
    unittest.main()
