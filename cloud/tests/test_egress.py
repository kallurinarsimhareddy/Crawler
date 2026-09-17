"""The worker's egress guard, with real sockets and real HTTP redirects.

A local HTTP server plays a "public" website (its hostname is allowlisted so
the first hop is permitted). It redirects to private and metadata addresses, and
the tests prove the redirect hop is refused. That is a request made with
``requests`` exactly as the crawler makes it, including a session built by the
crawler's own ``utils.http.build_session``.
"""

from __future__ import annotations

import http.server
import socket
import threading
import unittest

import requests

from cloud.worker.egress import (
    EgressBlockedError,
    egress_guard_installed,
    install_egress_guard,
    uninstall_egress_guard,
)


class Redirector(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        targets = {
            "/to-metadata": "http://169.254.169.254/latest/meta-data/",
            "/to-loopback": "http://127.0.0.1:9/",
            "/to-private": "http://10.1.2.3/admin",
            "/to-rebind": "http://rebind.test:%d/ok" % self.server.server_address[1],
            "/to-ipv6-mapped": "http://[::ffff:127.0.0.1]:9/",
        }
        if self.path in targets:
            self.send_response(302)
            self.send_header("Location", targets[self.path])
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def fake_resolver(port):
    def resolve(host, port_, family=0, type=0, proto=0, flags=0):
        mapping = {
            "public.test": "127.0.0.1",   # allowlisted, stands in for a public site
            "rebind.test": "127.0.0.1",   # a public-looking name that resolves to loopback
            "mixed.test": ["93.184.216.34", "10.0.0.1"],
        }
        if host in mapping:
            addresses = mapping[host] if isinstance(mapping[host], list) else [mapping[host]]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port_)) for a in addresses]
        return socket.getaddrinfo(host, port_, family, type, proto, flags)

    return resolve


class TestEgressGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        # "localhost" stands in for an allowlisted public site: allowlisted hosts use
        # normal DNS, everything else goes through the guard's own resolution.
        install_egress_guard(allow_hosts=["localhost"], resolver=fake_resolver(self.port))
        self.addCleanup(uninstall_egress_guard)

    def get(self, path: str, session=None):
        return (session or requests).get(f"http://localhost:{self.port}{path}", timeout=5)

    def assertBlocked(self, path: str, session=None) -> None:
        with self.assertRaises(requests.exceptions.ConnectionError) as caught:
            self.get(path, session)
        self.assertIn("not a public internet address", str(caught.exception))

    def test_the_allowlisted_first_hop_works(self) -> None:
        self.assertTrue(egress_guard_installed())
        self.assertEqual(self.get("/plain").text, "ok")

    def test_redirects_to_metadata_loopback_and_private_ranges_are_refused(self) -> None:
        for path in ("/to-loopback", "/to-private", "/to-rebind", "/to-ipv6-mapped"):
            with self.subTest(path=path):
                self.assertBlocked(path)

    def test_metadata_redirect_is_refused_without_ever_connecting(self) -> None:
        from cloud.worker import egress

        before = egress.blocked_count
        with self.assertRaises(requests.exceptions.ConnectionError):
            self.get("/to-metadata")
        self.assertGreater(egress.blocked_count, before)

    def test_the_crawlers_own_session_is_guarded_too(self) -> None:
        from utils.http import build_session

        session = build_session(retries=1)
        try:
            self.assertEqual(self.get("/plain", session).text, "ok")
            self.assertBlocked("/to-private", session)
        finally:
            session.close()

    def test_direct_private_targets_and_mixed_answers_are_refused(self) -> None:
        for url in (
            f"http://127.0.0.1:{self.port}/plain",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:9/",
            "http://mixed.test/",
        ):
            with self.subTest(url=url), self.assertRaises(requests.exceptions.ConnectionError):
                requests.get(url, timeout=5)

    def test_the_guard_is_idempotent_and_removable(self) -> None:
        install_egress_guard(allow_hosts=["localhost"], resolver=fake_resolver(self.port))
        self.assertTrue(egress_guard_installed())
        uninstall_egress_guard()
        self.assertFalse(egress_guard_installed())
        self.assertIsInstance(EgressBlockedError("x"), ConnectionError)


if __name__ == "__main__":
    unittest.main()
