"""The scraper's headless Chromium cannot reach internal addresses (phase 2 security).

Real Chromium and a real local HTTP server; no internet. ``localhost`` stands in for a public
career site (allowlisted the way the egress tests allowlist it); everything else the page
tries — direct private addresses, alternate IPv4 spellings, a name that resolves to loopback,
a popup, a redirect into private space, a WebSocket (dead proxy), a service worker, and a name whose DNS
answer changes between the URL check and the connect (rebinding) — must be refused.
"""

from __future__ import annotations

import http.server
import socket
import threading
import unittest

import importlib.util

PUBLIC_DECOY = "93.184.216.34"   # never contacted: the connect-time check refuses first


def page_html(port: int) -> str:
    return f"""<!doctype html><html><head><title>Careers</title>
<script src="http://127.0.0.1:{port}/direct.js"></script>
<script src="http://2130706433:{port}/integer.js"></script>
<script src="http://0x7f.0.0.1:{port}/hex.js"></script>
<script src="http://[::1]:{port}/ipv6.js"></script>
<script src="http://flip.example:{port}/rebind.js"></script>
<link rel="stylesheet" href="http://169.254.169.254/latest/meta-data/">
</head><body>
<ul id="jobs"><li><a href="/careers/1">Data Engineer</a></li></ul>
<iframe src="http://loopback.example:{port}/frame"></iframe>
<script>
  document.title = "loaded";
  fetch("http://10.1.2.3/admin").then(() => document.title += " fetch-ok").catch(() => document.title += " fetch-refused");
  fetch("/to-private").then(() => document.title += " redirect-ok").catch(() => document.title += " redirect-refused");
  window.open("http://192.168.1.1/router");
  try {{
    const ws = new WebSocket("ws://localhost:{port}/ws");
    ws.onopen = () => document.title += " ws-open";
    ws.onclose = () => document.title += " ws-closed";
  }} catch (e) {{ document.title += " ws-error"; }}
  if (navigator.serviceWorker) {{
    navigator.serviceWorker.register("/sw.js").then(() => document.title += " sw-ok")
      .catch(() => document.title += " sw-refused");
  }} else {{ document.title += " sw-unavailable"; }}
</script></body></html>"""


class Site(http.server.BaseHTTPRequestHandler):
    hits: list = []

    def do_GET(self):  # noqa: N802
        Site.hits.append(self.path)
        port = self.server.server_address[1]
        if self.path == "/to-jobs":
            self.send_response(301)
            self.send_header("Location", "/jobs")
            self.end_headers()
            return
        if self.path == "/to-private":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
            self.end_headers()
            return
        body = page_html(port).encode() if self.path in ("/", "/jobs") else b"// ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path in ("/", "/jobs") else "text/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FlipResolver:
    """flip.example is public when the URL is checked and loopback at connect time."""

    def __init__(self) -> None:
        self.calls = {}

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        mapping = {"loopback.example": "127.0.0.1"}
        if host == "flip.example":
            n = self.calls[host] = self.calls.get(host, 0) + 1
            mapping[host] = PUBLIC_DECOY if n % 2 else "127.0.0.1"
        if host in mapping:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (mapping[host], port))]
        return socket.getaddrinfo(host, port, family, type, proto, flags)


@unittest.skipIf(importlib.util.find_spec("playwright") is None, "Playwright is not installed")
class BrowserContainmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_every_internal_destination_is_refused_and_the_page_still_renders(self) -> None:
        from cloud.intel.scraper.fetcher import PlaywrightRenderer

        Site.hits.clear()
        renderer = PlaywrightRenderer(resolver=FlipResolver(), allow_hosts=("localhost",), timeout_ms=20000)
        result = renderer.render(f"http://localhost:{self.port}/jobs")
        self.assertIsNone(result.error)
        self.assertIn("Data Engineer", result.html)            # the legitimate page works
        blocked = "\n".join(renderer.blocked)
        # Chromium itself rewrites 2130706433 and 0x7f.0.0.1 to 127.0.0.1 before requesting
        for needle in (f"127.0.0.1:{self.port}/direct.js", f"127.0.0.1:{self.port}/integer.js",
                       f"127.0.0.1:{self.port}/hex.js", "[::1]",
                       "169.254.169.254", "10.1.2.3", "192.168.1.1", "loopback.example",
                       f"127.0.0.1:{self.port}/secret", "flip.example"):
            with self.subTest(blocked=needle):
                self.assertIn(needle, blocked)
        title = result.html.split("<title>", 1)[1].split("</title>", 1)[0]
        self.assertIn("fetch-refused", title)
        self.assertIn("redirect-refused", title)
        self.assertNotIn("ws-open", title)
        # A service-worker registration can report success, but its script is never fetched
        # (the dead proxy): the hit list below shows the server never saw /sw.js.
        # the server only ever saw the page and the redirect hop, never an internal fetch
        self.assertEqual(sorted(set(Site.hits)), ["/jobs", "/to-private"])
        self.assertNotIn("/sw.js", Site.hits)

    def test_a_public_redirect_is_followed_and_each_hop_is_checked(self) -> None:
        from cloud.intel.scraper.fetcher import PlaywrightRenderer

        Site.hits.clear()
        renderer = PlaywrightRenderer(resolver=FlipResolver(), allow_hosts=("localhost",), timeout_ms=20000)
        result = renderer.render(f"http://localhost:{self.port}/to-jobs")
        self.assertIsNone(result.error)
        self.assertIn("Data Engineer", result.html)
        self.assertEqual(result.final_url, f"http://localhost:{self.port}/jobs")
        self.assertIn(f'<base href="http://localhost:{self.port}/jobs">', result.html)
        # a redirect into private space is refused at the hop, not followed
        renderer = PlaywrightRenderer(resolver=FlipResolver(), allow_hosts=("localhost",), timeout_ms=20000)
        result = renderer.render(f"http://localhost:{self.port}/to-private")
        self.assertIn(f"http://127.0.0.1:{self.port}/secret", renderer.blocked)
        self.assertNotIn("/secret", Site.hits)


if __name__ == "__main__":
    unittest.main()
