"""A Redis-compatible server for local development, with no install and no Docker.

    python -m cloud.devtools.fake_redis            # listens on 127.0.0.1:6390

Backed by ``fakeredis`` (with Lua), in memory, gone when stopped. It speaks
enough of the protocol for the CareerCloud queue, which is all it is for. Use a
real Redis for anything else — and never point production at this.
"""

from __future__ import annotations

import argparse
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.devtools.fake_redis")
    parser.add_argument("--port", type=int, default=6390)
    args = parser.parse_args(argv)

    from fakeredis import TcpFakeServer

    server = TcpFakeServer(("127.0.0.1", args.port), server_type="redis")
    print(f"DEVELOPMENT ONLY fake Redis on redis://127.0.0.1:{args.port}/0 (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
