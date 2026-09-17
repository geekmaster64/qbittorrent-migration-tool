#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import torrent_transfer as tt  # noqa: E402


class FakeTorrent(SimpleNamespace):
    pass


class PathRewriteTests(unittest.TestCase):
    def test_longest_prefix_wins(self) -> None:
        mappings = [
            ("/data", "/mnt/data"),
            ("/data/torrents", "/mnt/storage/torrents"),
        ]
        self.assertEqual(
            tt.rewrite_path("/data/torrents/tv/show", mappings),
            "/mnt/storage/torrents/tv/show",
        )
        self.assertEqual(tt.rewrite_path("/data/other", mappings), "/mnt/data/other")

    def test_no_match_returns_original(self) -> None:
        self.assertEqual(tt.rewrite_path("/keep/me", [("/data", "/mnt")]), "/keep/me")

    def test_empty_path(self) -> None:
        self.assertEqual(tt.rewrite_path("", [("/data", "/mnt")]), "")
        self.assertEqual(tt.rewrite_path(None, [("/data", "/mnt")]), "")

    def test_windows_to_posix(self) -> None:
        mapped = tt.mapped_path(
            r"D:\Downloads\Movies",
            [(r"D:\Downloads", "/data/torrents")],
            "posix",
        )
        self.assertEqual(mapped, "/data/torrents/Movies")

    def test_slash_style_windows(self) -> None:
        self.assertEqual(tt.apply_slash_style("/data/tv", "windows"), r"\data\tv")

    def test_parse_path_map(self) -> None:
        self.assertEqual(tt.parse_path_map("/old=/new"), ("/old", "/new"))
        self.assertEqual(tt.parse_path_map("a=b=c"), ("a", "b=c"))
        with self.assertRaises(argparse.ArgumentTypeError):
            tt.parse_path_map("no-separator")
        with self.assertRaises(argparse.ArgumentTypeError):
            tt.parse_path_map("=/new")


class TagAndStateTests(unittest.TestCase):
    def test_split_tags(self) -> None:
        self.assertEqual(tt.split_tags("tv, 1080p, hdr"), ["tv", "1080p", "hdr"])
        self.assertEqual(tt.split_tags(""), [])
        self.assertEqual(tt.split_tags(None), [])

    def test_stopped_states(self) -> None:
        self.assertTrue(tt.torrent_is_stopped(FakeTorrent(state="pausedUP")))
        self.assertTrue(tt.torrent_is_stopped(FakeTorrent(state="stoppedDL")))
        self.assertFalse(tt.torrent_is_stopped(FakeTorrent(state="uploading")))

    def test_stopped_via_enum(self) -> None:
        enum = SimpleNamespace(is_stopped=True, is_paused=False)
        self.assertTrue(tt.torrent_is_stopped(FakeTorrent(state="weird", state_enum=enum)))


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.torrents = [
            FakeTorrent(hash="aaa", category="tv", tags="hd,watched", progress=1.0, name="show"),
            FakeTorrent(hash="bbb", category="movies", tags="4k", progress=0.4, name="film"),
            FakeTorrent(hash="ccc", category="tv", tags="", progress=1.0, name="other"),
        ]

    def test_category_filter(self) -> None:
        selected = tt.filter_torrents(self.torrents, categories=["TV"])
        self.assertEqual([t.hash for t in selected], ["aaa", "ccc"])

    def test_tag_filter(self) -> None:
        selected = tt.filter_torrents(self.torrents, tags=["4k"])
        self.assertEqual([t.hash for t in selected], ["bbb"])

    def test_hash_filter(self) -> None:
        selected = tt.filter_torrents(self.torrents, hashes=["BBB"])
        self.assertEqual([t.hash for t in selected], ["bbb"])

    def test_completed_only(self) -> None:
        selected = tt.filter_torrents(self.torrents, completed_only=True)
        self.assertEqual([t.hash for t in selected], ["aaa", "ccc"])

    def test_limit(self) -> None:
        selected = tt.filter_torrents(self.torrents, limit=1)
        self.assertEqual([t.hash for t in selected], ["aaa"])

    def test_already_done_skipped(self) -> None:
        selected = tt.filter_torrents(self.torrents, already_done=["AAA"])
        self.assertEqual([t.hash for t in selected], ["bbb", "ccc"])


class AddKwargsTests(unittest.TestCase):
    def test_manual_tmm_includes_save_path(self) -> None:
        torrent = FakeTorrent(
            tags="tv, hd",
            category="tv",
            auto_tmm=False,
            seq_dl=True,
            f_l_piece_prio=False,
            force_start=False,
            up_limit=1024,
            dl_limit=0,
            ratio_limit=-2,
            seeding_time_limit=-1,
        )
        kwargs = tt.add_kwargs_for_torrent(
            torrent,
            save_path="/data/tv",
            download_path="/incomplete",
            skip_checking=True,
            add_stopped=True,
        )
        self.assertEqual(kwargs["save_path"], "/data/tv")
        self.assertEqual(kwargs["download_path"], "/incomplete")
        self.assertTrue(kwargs["use_download_path"])
        self.assertEqual(kwargs["tags"], ["tv", "hd"])
        self.assertTrue(kwargs["is_skip_checking"])
        self.assertNotIn("seedMode", kwargs)
        self.assertTrue(kwargs["is_paused"])
        self.assertTrue(kwargs["is_stopped"])
        self.assertTrue(kwargs["is_sequential_download"])
        self.assertEqual(kwargs["upload_limit"], 1024)
        self.assertNotIn("download_limit", kwargs)

    def test_auto_tmm_omits_save_path(self) -> None:
        torrent = FakeTorrent(
            tags="",
            category="movies",
            auto_tmm=True,
            seq_dl=False,
            f_l_piece_prio=False,
            force_start=True,
            up_limit=-1,
            dl_limit=-1,
            ratio_limit=-2,
            seeding_time_limit=-2,
        )
        kwargs = tt.add_kwargs_for_torrent(
            torrent,
            save_path="/ignored",
            download_path="",
            skip_checking=True,
            add_stopped=False,
        )
        self.assertNotIn("save_path", kwargs)
        self.assertTrue(kwargs["use_auto_torrent_management"])
        self.assertTrue(kwargs["forced"])

    def test_seed_mode_on_new_api(self) -> None:
        torrent = FakeTorrent(
            tags="",
            category="",
            auto_tmm=True,
            seq_dl=False,
            f_l_piece_prio=False,
            force_start=False,
            up_limit=-1,
            dl_limit=-1,
            ratio_limit=-2,
            seeding_time_limit=-2,
        )
        kwargs = tt.add_kwargs_for_torrent(
            torrent,
            save_path="",
            download_path="",
            skip_checking=True,
            add_stopped=True,
            api_version=(2, 16, 0),
        )
        self.assertTrue(kwargs["seedMode"])
        self.assertNotIn("is_skip_checking", kwargs)


class CliTests(unittest.TestCase):
    def test_help_and_version(self) -> None:
        parser = tt.build_parser()
        with self.assertRaises(SystemExit) as cm:
            parser.parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_path_map_and_dry_run(self) -> None:
        parser = tt.build_parser()
        args = parser.parse_args(
            [
                "--source-host",
                "10.0.0.1:8080",
                "--dest-host",
                "10.0.0.2:8080",
                "--path-map",
                "/data=/mnt/data",
                "--dry-run",
                "--completed-only",
                "--limit",
                "5",
            ]
        )
        self.assertEqual(args.path_map, [("/data", "/mnt/data")])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.completed_only)
        self.assertEqual(args.limit, 5)

    def test_identical_hosts_refused(self) -> None:
        parser = tt.build_parser()
        args = parser.parse_args(
            [
                "--source-host",
                "localhost:8080",
                "--dest-host",
                "localhost:8080",
                "--source-user",
                "admin",
                "--dest-user",
                "admin",
                "--source-pass",
                "x",
                "--dest-pass",
                "x",
                "--yes",
            ]
        )
        self.assertEqual(tt.run(args), 2)

    def test_new_flags(self) -> None:
        parser = tt.build_parser()
        args = parser.parse_args(
            [
                "--source-host",
                "a:1",
                "--dest-host",
                "b:1",
                "--copy-rss",
                "--copy-search-plugins",
                "--state-file",
                "state.json",
                "--torrent-dir",
                "/tmp/torrents",
            ]
        )
        self.assertTrue(args.copy_rss)
        self.assertTrue(args.copy_search_plugins)
        self.assertEqual(args.state_file, Path("state.json"))
        self.assertEqual(args.torrent_dir, Path("/tmp/torrents"))


class ApiVersionTests(unittest.TestCase):
    def test_parse_api_version(self) -> None:
        self.assertEqual(tt.parse_api_version("2.16.0"), (2, 16, 0))
        self.assertEqual(tt.parse_api_version("v2.8.14"), (2, 8, 14))
        self.assertEqual(tt.parse_api_version("2"), (2, 0, 0))


class RssFlattenTests(unittest.TestCase):
    def test_nested_feeds_and_folders(self) -> None:
        items = {
            "Linux": {
                "ISOs": {
                    "Ubuntu": {"uid": "1", "url": "https://example.com/ubuntu.xml"},
                }
            },
            "Top": {"uid": "2", "url": "https://example.com/top.xml"},
        }
        folders, feeds = tt.flatten_rss_items(items)
        self.assertEqual(folders, ["Linux", "Linux\\ISOs"])
        self.assertEqual(
            feeds,
            [
                ("Linux\\ISOs\\Ubuntu", "https://example.com/ubuntu.xml"),
                ("Top", "https://example.com/top.xml"),
            ],
        )


class TorrentBytesTests(unittest.TestCase):
    def test_infohash_roundtrip(self) -> None:
        info = b"d4:name4:tiny12:piece lengthi16e6:pieces0:6:lengthi0ee"
        payload = b"d8:announce0:4:info" + info + b"e"
        digest = tt.infohash_of_torrent_bytes(payload)
        self.assertIsNotNone(digest)
        self.assertEqual(len(digest), 40)

    def test_index_and_lookup(self) -> None:
        info = b"d4:name4:tiny12:piece lengthi16e6:pieces0:6:lengthi0ee"
        payload = b"d8:announce0:4:info" + info + b"e"
        digest = tt.infohash_of_torrent_bytes(payload)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            named = folder / f"{digest}.torrent"
            named.write_bytes(payload)
            index = tt.index_torrent_dir(folder)
            torrent = FakeTorrent(hash=digest, infohash_v1=digest, infohash_v2=None)
            found = tt.find_local_torrent(index, torrent)
            self.assertEqual(found, payload)


class StateFileTests(unittest.TestCase):
    def test_roundtrip_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = tt.MigrationState.load(path)
            state.mark_completed("ABC")
            state.mark_failed("def", "boom")
            loaded = tt.MigrationState.load(path)
            self.assertIn("abc", loaded.completed)
            self.assertEqual(loaded.failed["def"], "boom")
            data = json.loads(path.read_text())
            self.assertEqual(data["version"], 1)
            reset = tt.MigrationState.load(path, reset=True)
            self.assertEqual(reset.completed, set())


if __name__ == "__main__":
    unittest.main()
