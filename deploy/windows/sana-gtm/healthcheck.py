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

HEARTBEAT_FRESH_S = 240  # the worker beats every 60 s (queue presence and its state file)


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return values


def _state_age(state, now: float):
    """Seconds since the worker last wrote its state file, or None."""
    try:
        from datetime import datetime

        return now - datetime.fromisoformat(state["updated_at"]).timestamp()
    except (TypeError, KeyError, ValueError):
        return None


def main() -> int:
    env = read_env(Path(sys.argv[1]))
    out = {"database": False, "queue": False, "queue_quota_exhausted": False, "worker_heartbeat": False,
           "workers_online": 0, "last_heartbeat_age_s": None, "worker_state": None, "errors": {}}
    # what the worker itself reports (written by cloud.intel.tasks.worker --state-file)
    state_file = Path(__file__).resolve().parents[3] / "logs" / "sana-gtm" / "worker-state.json"
    try:
        out["worker_state"] = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    try:
        import psycopg

        with psycopg.connect(env["CAREERCLOUD_DATABASE_URL"], connect_timeout=10) as conn:
            out["database"] = conn.execute("select 1").fetchone()[0] == 1
    except Exception as error:  # noqa: BLE001 - reported by class name only
        out["errors"]["database"] = type(error).__name__
    if env.get("CAREERCLOUD_QUEUE", "redis") != "redis":
        # PostgreSQL task queue: the queue is the database; the heartbeat is the worker's state file.
        out["queue"] = bool(out["database"])
        fresh = _state_age(out["worker_state"], time.time())
        if fresh is not None and fresh < HEARTBEAT_FRESH_S:
            out["workers_online"], out["last_heartbeat_age_s"] = 1, round(fresh, 1)
        out["worker_heartbeat"] = out["workers_online"] > 0
        print(json.dumps(out))
        return 0
    try:
        import redis

        client = redis.Redis.from_url(env["CAREERCLOUD_REDIS_URL"], socket_timeout=10, socket_connect_timeout=10,
                                      decode_responses=True)
        client.ping()                           # PING is not a billed command
        now = time.time()
        fresh = _state_age(out["worker_state"], now)
        if fresh is not None and fresh < HEARTBEAT_FRESH_S:
            # the worker's own state file says it is alive: no queue commands needed
            out["workers_online"], out["last_heartbeat_age_s"] = 1, round(fresh, 1)
        else:
            key = f"{env.get('CAREERCLOUD_QUEUE_PREFIX', 'careercloud')}:platform:workers"
            out["workers_online"] = int(client.zcount(key, now - HEARTBEAT_FRESH_S, "+inf"))
            newest = client.zrange(key, -1, -1, withscores=True)
            if newest:
                out["last_heartbeat_age_s"] = round(now - float(newest[0][1]), 1)
        out["worker_heartbeat"] = out["workers_online"] > 0
        out["queue"] = True                     # only when every queue command was served
    except Exception as error:  # noqa: BLE001
        out["errors"]["queue"] = type(error).__name__
        if "request limit" in str(error).lower() or "requests limit" in str(error).lower():
            out["queue_quota_exhausted"] = True
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
