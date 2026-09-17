"""A tiny but valid .torrent used by live and fake-API tests."""

from __future__ import annotations

import hashlib


def bencode(obj: object) -> bytes:
    if isinstance(obj, bool):
        raise TypeError("bencode has no bool")
    if isinstance(obj, int):
        return f"i{obj}e".encode()
    if isinstance(obj, bytes):
        return f"{len(obj)}:".encode() + obj
    if isinstance(obj, str):
        return bencode(obj.encode("utf-8"))
    if isinstance(obj, list):
        return b"l" + b"".join(bencode(item) for item in obj) + b"e"
    if isinstance(obj, dict):
        items = b""
        keys = sorted(obj.keys(), key=lambda key: key if isinstance(key, bytes) else str(key).encode())
        for key in keys:
            raw_key = key if isinstance(key, bytes) else str(key).encode()
            items += bencode(raw_key) + bencode(obj[key])
        return b"d" + items + b"e"
    raise TypeError(f"cannot bencode {type(obj)}")


FILE_NAME = "tiny.txt"
FILE_BYTES = b"qbittorrent-migration-tool live test\n"
PIECE_LENGTH = 16384


def tiny_torrent_bytes() -> bytes:
    info = {
        b"name": FILE_NAME.encode(),
        b"piece length": PIECE_LENGTH,
        b"pieces": hashlib.sha1(FILE_BYTES).digest(),
        b"length": len(FILE_BYTES),
    }
    return bencode(
        {
            b"announce": b"http://127.0.0.1:9/announce",
            b"comment": b"fixture for qbittorrent-migration-tool",
            b"info": info,
        }
    )
