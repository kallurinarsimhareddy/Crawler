"""An embedded PostgreSQL for local development, with no install and no Docker.

    python -m cloud.devtools.localpg start    # start (or find) it, apply migrations, print the URL
    python -m cloud.devtools.localpg url      # print the URL of the running server
    python -m cloud.devtools.localpg stop

Data lives in ``cloud/.localdev/postgres`` (git-ignored). The server listens on
127.0.0.1 only. With ``CAREERCLOUD_DATABASE_URL=localdev`` the API, the worker and
the migrator all find this same server.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from cloud.db.connection import LOCALDEV_PGDATA, localdev_database_url


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.devtools.localpg")
    parser.add_argument("command", choices=["start", "url", "stop"])
    args = parser.parse_args(argv)

    if args.command == "stop":
        import pgserver

        if not LOCALDEV_PGDATA.exists():
            print("not running")
            return 0
        pgserver.get_server(LOCALDEV_PGDATA, cleanup_mode="stop").cleanup()
        print("stopped")
        return 0

    url = localdev_database_url()
    if args.command == "start":
        from cloud.db.migrate import apply_migrations

        applied = apply_migrations(url)
        print("migrations applied: " + (", ".join(applied) if applied else "none pending"), file=sys.stderr)
    print(url)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
