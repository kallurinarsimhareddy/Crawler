"""The platform worker installs the egress guard (audit 2026-10-14 finding D2: the
``crawl`` task follows CareerCrawler redirects, but only the legacy worker guarded them).

Real sockets and real redirects: a local server plays an allowlisted "public" site and
redirects - directly or through a chain of hops - to internal destinations. Names in
``fake_resolver`` stand in for public-looking hosts that resolve to forbidden ranges.
"""

from __future__ import annotations

import http.server
import socket
import threading
import unittest

import requests

from cloud.intel.tasks import worker as worker_mod
from cloud.worker.egress import egress_guard_installed, uninstall_egress_guard

FORBIDDEN = {
    "loopback6.test": ("::1", socket.AF_INET6),
    "linklocal6.test": ("fe80::1", socket.AF_INET6),
    "ula6.test": ("fd00::1", socket.AF_INET6),
    "mapped6.test": ("::ffff:10.0.0.1", socket.AF_INET6),
    "cgnat.test": ("100.64.0.1", socket.AF_INET),
    "unspecified.test": ("0.0.0.0", socket.AF_INET),
    "multicast.test": ("224.0.0.1", socket.AF_INET),
    "metadata.test": ("169.254.169.254", socket.AF_INET),
    "private172.test": ("172.16.5.5", socket.AF_INET),
    "private192.test": ("192.168.1.1", socket.AF_INET),
}


def fake_resolver(host, port, family=0, type=0, proto=0, flags=0):
    if host in FORBIDDEN:
        address, fam = FORBIDDEN[host]
        sockaddr = (address, port, 0, 0) if fam == socket.AF_INET6 else (address, port)
        return [(fam, socket.SOCK_STREAM, 6, "", sockaddr)]
    return socket.getaddrinfo(host, port, family, type, proto, flags)


class Chain(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        port = self.server.server_address[1]
        hops = {
            "/hop1": f"http://localhost:{port}/hop2",
            "/hop2": f"http://localhost:{port}/hop3",
            "/hop3": "http://private192.test/admin",
            "/chain-ok": f"http://localhost:{port}/plain",
            "/to-ula6": "http://ula6.test/",
            "/to-cgnat": "http://cgnat.test/",
            "/to-relative": "/plain",
        }
        if self.path in hops:
            self.send_response(301)
            self.send_header("Location", hops[self.path])
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class PlatformWorkerEgress(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Chain)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def install(self, env=None) -> bool:
        from cloud.worker import egress

        self.addCleanup(uninstall_egress_guard)
        installed = worker_mod.install_platform_egress_guard(
            {"CAREERCLOUD_EGRESS_ALLOW_HOSTS": "localhost", **(env or {})})
        egress._resolver = fake_resolver  # noqa: SLF001 - stand-in DNS for the *.test names
        return installed

    def url(self, path: str) -> str:
        return f"http://localhost:{self.port}{path}"

    def assertRefused(self, url: str) -> None:
        with self.assertRaises(requests.exceptions.ConnectionError) as caught:
            requests.get(url, timeout=5)
        self.assertIn("not a public internet address", str(caught.exception))

    def test_main_installs_the_guard_by_default(self) -> None:
        self.assertTrue(self.install())
        self.assertTrue(egress_guard_installed())

    def test_redirect_chains_are_followed_until_they_turn_internal(self) -> None:
        self.install()
        self.assertEqual(requests.get(self.url("/chain-ok"), timeout=5).text, "ok")
        self.assertEqual(requests.get(self.url("/to-relative"), timeout=5).text, "ok")
        for path in ("/hop1", "/to-ula6", "/to-cgnat"):
            with self.subTest(path=path):
                self.assertRefused(self.url(path))

    def test_direct_requests_to_every_forbidden_range_are_refused(self) -> None:
        self.install()
        for host in FORBIDDEN:
            with self.subTest(host=host):
                self.assertRefused(f"http://{host}/")
        for literal in ("http://127.0.0.1:9/", "http://[::1]:9/", "http://169.254.169.254/",
                        "http://[fe80::1]:9/", "http://10.0.0.1/"):
            with self.subTest(url=literal):
                self.assertRefused(literal)

    def test_the_crawlers_own_sessions_are_guarded(self) -> None:
        from utils.http import build_session

        self.install()
        session = build_session(retries=1)
        try:
            self.assertEqual(session.get(self.url("/plain"), timeout=5).text, "ok")
            with self.assertRaises(requests.exceptions.ConnectionError):
                session.get(self.url("/hop1"), timeout=5)
        finally:
            session.close()

    def test_only_the_workers_own_infrastructure_is_exempt(self) -> None:
        hosts = worker_mod.egress_allow_hosts({
            "CAREERCLOUD_S3_ENDPOINT": "https://minio.internal:9000",
            "OPENAI_COMPATIBLE_BASE_URL": "http://localhost:11434/v1",
            "CAREERCLOUD_EGRESS_ALLOW_HOSTS": " Ops.Example. ,",
        })
        self.assertEqual(hosts, ["localhost", "minio.internal", "ops.example"])
        # a remote (https) model endpoint is a public host and needs no exemption
        self.assertEqual(worker_mod.egress_allow_hosts({"OPENAI_COMPATIBLE_BASE_URL": "https://llm.example.com/v1"}), [])

    def test_disabling_is_refused_where_it_matters(self) -> None:
        for environment in ("production", "staging"):
            with self.subTest(env=environment), self.assertRaises(SystemExit):
                worker_mod.install_platform_egress_guard(
                    {"CAREERCLOUD_EGRESS_GUARD": "false", "CAREERCLOUD_ENV": environment})
        self.assertFalse(worker_mod.install_platform_egress_guard(
            {"CAREERCLOUD_EGRESS_GUARD": "false", "CAREERCLOUD_ENV": "development"}))
        self.assertFalse(egress_guard_installed())


if __name__ == "__main__":
    unittest.main()
