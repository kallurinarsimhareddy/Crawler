"""One origin for the self-hosted Windows install: the dashboard and the API in one process.

    uvicorn cloud.ops.webapp:create_app --factory --host 127.0.0.1 --port 8100

* ``/api/*`` goes, in-process, to the unchanged API (``cloud.api.main.app``): no proxy hop,
  uploads and exports stream straight through, and the API keeps its own security headers.
* everything else is the prebuilt dashboard (``SANAGTM_WEB_DIST``), with SPA routing, long
  caching for hashed assets and the browser security headers (CSP, HSTS, frame denial …).
* ``/runtime-config.js`` hands the dashboard its PUBLIC settings (auth URL + anon key), so one
  prebuilt dashboard serves every installation — no Node.js and no rebuild on the server.
* ``/healthz`` answers without touching the database (for the service manager).

Cloudflare Tunnel connects to this server on 127.0.0.1, so the API's loopback rule for
``CF-Connecting-IP`` applies unchanged.
"""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit

from starlette.responses import FileResponse, PlainTextResponse, Response

Scope = Dict[str, Any]
Receive = Callable[[], Awaitable[Dict[str, Any]]]
Send = Callable[[Dict[str, Any]], Awaitable[None]]

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _auth_origins(env: Dict[str, str]) -> str:
    url = (env.get("SANAGTM_PUBLIC_SUPABASE_URL") or env.get("CAREERCLOUD_SUPABASE_URL") or "").rstrip("/")
    if not url:
        return ""
    host = urlsplit(url).netloc
    return f"{url} wss://{host}"


def security_headers(env: Dict[str, str]) -> Dict[str, str]:
    csp = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
           f"connect-src 'self' {_auth_origins(env)}".rstrip() + "; frame-ancestors 'none'; base-uri 'self'; "
           "form-action 'self'; object-src 'none'")
    return {
        "Content-Security-Policy": csp,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Strict-Transport-Security": "max-age=31536000",
        "X-Robots-Tag": "noindex, nofollow",
    }


def runtime_config(env: Dict[str, str]) -> str:
    """Public values only. The anon/publishable key is designed to be visible to every browser."""
    key = env.get("SANAGTM_PUBLIC_SUPABASE_ANON_KEY") or env.get("CAREERCLOUD_SUPABASE_ANON_KEY") or ""
    if "service_role" in key or key.startswith("sb_secret_"):
        raise ValueError("refusing to serve a secret key to browsers; use the public anon/publishable key")
    config = {
        "VITE_AUTH_MODE": "supabase",
        "VITE_DEPLOY_ENV": "production",
        "VITE_API_URL": "",
        "VITE_SUPABASE_URL": (env.get("SANAGTM_PUBLIC_SUPABASE_URL") or env.get("CAREERCLOUD_SUPABASE_URL") or ""),
        "VITE_SUPABASE_ANON_KEY": key,
    }
    return "window.__SANA_CONFIG__ = " + json.dumps(config, sort_keys=True) + ";\n"


class WebApp:
    def __init__(self, dist: Path, api: Any, env: Optional[Dict[str, str]] = None) -> None:
        self.dist = dist.resolve()
        self.api = api
        env = dict(os.environ if env is None else env)
        self.headers = security_headers(env)
        self.config_js = runtime_config(env).encode()
        if not (self.dist / "index.html").is_file():
            raise RuntimeError(f"no dashboard build at {self.dist} (index.html missing)")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (scope["type"] in ("http", "websocket")
                                          and (scope["path"] == "/api" or scope["path"].startswith("/api/"))):
            await self.api(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        response = self.static(scope)
        await response(scope, receive, send)

    def static(self, scope: Scope) -> Response:
        path = scope["path"]
        if scope["method"] not in ("GET", "HEAD"):
            return self._with_headers(PlainTextResponse("method not allowed", status_code=405), cache="no-store")
        if path == "/healthz":
            return PlainTextResponse("ok\n", headers={"Cache-Control": "no-store"})
        if path == "/runtime-config.js":
            return self._with_headers(Response(self.config_js, media_type="application/javascript"), cache="no-store")
        relative = path.lstrip("/")
        target = (self.dist / relative).resolve() if relative else self.dist / "index.html"
        inside = target == self.dist or self.dist in target.parents
        if inside and target.is_file():
            cache = "public, max-age=31536000, immutable" if relative.startswith("assets/") else "no-cache"
            return self._with_headers(FileResponse(target), cache=cache)
        if not inside or "." in Path(relative).name:   # a missing file, or an escape attempt: no SPA page
            return self._with_headers(PlainTextResponse("not found", status_code=404), cache="no-store")
        return self._with_headers(FileResponse(self.dist / "index.html"), cache="no-cache")   # SPA route

    def _with_headers(self, response: Response, *, cache: str) -> Response:
        for name, value in self.headers.items():
            response.headers[name] = value
        response.headers["Cache-Control"] = cache
        return response


def create_app() -> WebApp:
    """uvicorn factory: ``uvicorn cloud.ops.webapp:create_app --factory``."""
    from cloud.api.main import app as api

    dist = Path(os.environ.get("SANAGTM_WEB_DIST") or Path(__file__).resolve().parents[1] / "web" / "dist")
    return WebApp(dist, api)
