"""Check SANA GTM configuration before it is saved (run by SANA-GTM-Setup.exe with the
installed runtime). Reads {"secrets": {...}, "settings": {...}} as JSON on stdin and
prints one JSON object: {"database": ok, "queue": ok, "supabase": ok, "errors": {...}}.
Read-only (SELECT 1, PING, one HTTPS request). Errors are reported by class name
and a short hint -- never with connection strings, passwords or tokens.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    data = json.loads(sys.stdin.read())
    secrets, settings = data.get("secrets", {}), data.get("settings", {})
    out = {"database": False, "queue": False, "supabase": False, "errors": {}}
    try:
        import psycopg

        with psycopg.connect(secrets["CAREERCLOUD_DATABASE_URL"], connect_timeout=15) as conn:
            out["database"] = conn.execute("select 1").fetchone()[0] == 1
    except Exception as e:  # noqa: BLE001
        hint = "password authentication failed" if "password" in str(e).lower() else ""
        out["errors"]["database"] = (type(e).__name__ + (": " + hint if hint else ""))
    try:
        import redis

        r = redis.Redis.from_url(secrets["CAREERCLOUD_REDIS_URL"], socket_timeout=15, socket_connect_timeout=15)
        out["queue"] = bool(r.ping())
    except Exception as e:  # noqa: BLE001
        out["errors"]["queue"] = type(e).__name__
    url = (settings.get("supabase_url") or "").rstrip("/")
    try:
        req = urllib.request.Request(url + "/auth/v1/.well-known/jwks.json", headers={"User-Agent": "sana-gtm-setup"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            out["supabase"] = resp.status == 200
    except urllib.error.HTTPError as e:
        out["supabase"] = e.code in (401, 403)  # reachable; the project answers
        if not out["supabase"]:
            out["errors"]["supabase"] = f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        out["errors"]["supabase"] = type(e).__name__
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
