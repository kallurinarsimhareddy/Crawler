"""SANA GTM backend health for the Windows startup manager (sana-gtm.ps1).

Checks the database and the queue the API/worker are configured with (the
worker's git-ignored env file) and whether a platform worker has sent a
heartbeat recently. Prints one JSON object. Read-only: SELECT 1, PING and a
ZCOUNT. Never prints connection strings, passwords or tokens -- failures are
reported by exception class only.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HEARTBEAT_FRESH_S = 120  # the worker heartbeats every 30 s


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def main() -> int:
    env = read_env(Path(sys.argv[1]))
    out = {"database": False, "queue": False, "worker_heartbeat": False, "workers_online": 0,
           "last_heartbeat_age_s": None, "errors": {}}
    try:
        import psycopg

        with psycopg.connect(env["CAREERCLOUD_DATABASE_URL"], connect_timeout=10) as conn:
            out["database"] = conn.execute("select 1").fetchone()[0] == 1
    except Exception as error:  # noqa: BLE001 - reported by class name only
        out["errors"]["database"] = type(error).__name__
    try:
        import redis

        client = redis.Redis.from_url(env["CAREERCLOUD_REDIS_URL"], socket_timeout=10, socket_connect_timeout=10,
                                      decode_responses=True)
        out["queue"] = bool(client.ping())
        key = f"{env.get('CAREERCLOUD_QUEUE_PREFIX', 'careercloud')}:platform:workers"
        now = time.time()
        out["workers_online"] = int(client.zcount(key, now - HEARTBEAT_FRESH_S, "+inf"))
        newest = client.zrange(key, -1, -1, withscores=True)
        if newest:
            out["last_heartbeat_age_s"] = round(now - float(newest[0][1]), 1)
        out["worker_heartbeat"] = out["workers_online"] > 0
    except Exception as error:  # noqa: BLE001
        out["errors"]["queue"] = type(error).__name__
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
