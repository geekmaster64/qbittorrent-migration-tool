# Live qBittorrent tests

Spins up two linuxserver qBittorrent containers (admin / adminadmin) and copies a tiny torrent from one to the other.

```bash
docker compose -f docker-compose.test.yml up -d
QBT_LIVE=1 python3 tests/integration/test_live.py
# or, from this repo after `pip install -e .`:
# QBT_LIVE=1 .venv/bin/python tests/integration/test_live.py
docker compose -f docker-compose.test.yml down -v
```

Source Web UI: `http://127.0.0.1:18080`  
Dest Web UI: `http://127.0.0.1:18081`

The default `python -m unittest discover -s tests` run **skips** this file unless `QBT_LIVE=1`.
