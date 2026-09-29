"""SANA GTM Supervisor -- runs hidden under pythonw.exe (started by the "SANA GTM"
scheduled task at sign-in, and re-fired every 2 minutes as a watchdog).

    API     runtime\\python.exe -m uvicorn cloud.api.main:app  (127.0.0.1:<port>)
    Worker  runtime\\python.exe -m cloud.intel.tasks.worker
    Tunnel  bin\\cloudflared.exe tunnel --url http://127.0.0.1:<port>   (or a named tunnel token)

Every child is started with CREATE_NO_WINDOW (no console, nothing on screen),
restarted with back-off when it dies or stops answering, and de-duplicated. One
supervisor per installation (named mutex). Once the tunnel answers publicly, its
URL is published to Upstash (<prefix>:frontend:api_origin, 5-minute expiry,
refreshed every minute); the https://sanagtm.pages.dev /api proxy reads it, so a
new tunnel URL needs no frontend rebuild. Status goes to state\\status.json for the
control panel. Secrets are never logged.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sanagtm_common as C  # noqa: E402

P = C.installed_paths()
LOG = logging.getLogger("supervisor")


def setup_logging() -> None:
    P.ensure()
    handler = logging.handlers.RotatingFileHandler(P.logs / "supervisor.log", maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
PASS_S = 10


def http_ok(url: str, timeout: float = 10) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sana-gtm-supervisor"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def rotate(path: Path) -> None:
    if path.exists():
        try:
            path.replace(path.with_name(path.name + ".prev"))
        except OSError:
            pass


class Service:
    def __init__(self, name: str):
        self.name = name
        self.started = 0.0
        self.restarts = 0
        self.next_try = 0.0
        self.fails = 0

    def backoff(self) -> None:
        self.restarts += 1
        self.next_try = time.time() + min(300, 10 * 2 ** min(5, self.restarts - 1))


class Supervisor:
    def __init__(self):
        self.settings = C.load_settings(P)
        self.secrets = C.load_secrets(P)
        missing = [k for k in C.REQUIRED_SECRETS if not self.secrets.get(k)]
        if missing:
            raise SystemExit("configuration incomplete: " + ", ".join(missing))
        self.port = int(self.settings.get("api_port", 8100))
        self.local_health = f"http://127.0.0.1:{self.port}/api/v1/health"
        self.env = C.service_env(P, self.settings, self.secrets)
        self.svc = {n: Service(n) for n in ("api", "worker", "tunnel")}
        self.public_url = ""
        self.public_ok = False
        self.db_ok = None
        self.queue_ok = None
        self.heartbeat_age = None
        self.workers_online = 0
        self.published = False
        self.last_deep = 0.0
        self.first = True
        self._redis = None

    # --- processes -------------------------------------------------------------------------
    def procs(self):
        found = {"api": [], "worker": [], "tunnel": []}
        for p in C.owned_processes(P):
            kind = C.classify(p)
            if kind in found:
                found[kind].append(p)
        return found

    def dedupe(self, name, plist):
        if len(plist) > 1:
            LOG.warning("%d %s processes; keeping the oldest", len(plist), name)
            for p in sorted(plist, key=lambda x: x.info.get("create_time") or 0)[1:]:
                C.kill_tree(p)
            return plist[:1]
        return plist

    def port_taken_by_other(self) -> bool:
        import psutil

        try:
            for c in psutil.net_connections("tcp"):
                if c.laddr and c.laddr.port == self.port and c.status == psutil.CONN_LISTEN and c.pid:
                    exe = (psutil.Process(c.pid).exe() or "").lower()
                    return not exe.startswith(str(P.root).lower())
        except Exception:  # noqa: BLE001
            return False
        return False

    def start_api(self):
        if self.port_taken_by_other():
            LOG.error("port %d is used by another program; the API cannot start (change api_port in "
                      "config\\settings.json or stop that program)", self.port)
            return
        rotate(P.logs / "api.log")
        p = C.spawn_hidden([str(P.python), "-m", "uvicorn", "cloud.api.main:app", "--host", "127.0.0.1",
                            "--port", str(self.port), "--proxy-headers"],
                           cwd=str(P.app), env=self.env, stdout=P.logs / "api.log", stderr=P.logs / "api.log")
        LOG.info("API started (pid %d)", p.pid)

    def start_worker(self):
        rotate(P.logs / "worker.log")
        p = C.spawn_hidden([str(P.python), "-m", "cloud.intel.tasks.worker"], cwd=str(P.app), env=self.env,
                           stdout=P.logs / "worker.log", stderr=P.logs / "worker.log")
        LOG.info("Worker started (pid %d)", p.pid)

    def start_tunnel(self):
        rotate(P.logs / "tunnel.log")
        env = {k: v for k, v in self.env.items() if not k.startswith("CAREERCLOUD_") and k != "GEMINI_API_KEY"}
        if self.settings.get("tunnel_mode") == "named" and self.secrets.get("TUNNEL_TOKEN"):
            env["TUNNEL_TOKEN"] = self.secrets["TUNNEL_TOKEN"]   # read by cloudflared; never on the command line
            args = [str(P.cloudflared), "tunnel", "--no-autoupdate", "run"]
        else:
            args = [str(P.cloudflared), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"]
        p = C.spawn_hidden(args, cwd=str(P.root), env=env, stdout=P.logs / "tunnel.log", stderr=P.logs / "tunnel.log")
        self.public_url = ""
        self.public_ok = False
        LOG.info("Tunnel started (pid %d)", p.pid)

    def find_public_url(self) -> str:
        if self.settings.get("tunnel_mode") == "named" and self.settings.get("tunnel_hostname"):
            return "https://" + self.settings["tunnel_hostname"].strip().rstrip("/").split("://")[-1]
        try:
            text = (P.logs / "tunnel.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        found = URL_RE.findall(text)
        return found[-1] if found else ""

    # --- redis ----------------------------------------------------------------------------
    def redis(self):
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(self.secrets["CAREERCLOUD_REDIS_URL"], socket_timeout=10,
                                               socket_connect_timeout=10, decode_responses=True)
        return self._redis

    def publish(self):
        key = C.origin_key(self.settings)
        try:
            self.redis().set(key, self.public_url, ex=300)
            if not self.published:
                LOG.info("Published API origin %s for the sanagtm.pages.dev proxy", self.public_url)
            self.published = True
        except Exception as e:  # noqa: BLE001
            self.published = False
            LOG.warning("Could not publish the API origin (%s)", type(e).__name__)

    def unpublish(self):
        try:
            key = C.origin_key(self.settings)
            if self.public_url and self.redis().get(key) == self.public_url:
                self.redis().delete(key)
                LOG.info("Unpublished API origin")
        except Exception:  # noqa: BLE001
            pass

    def deep_checks(self, worker_pids):
        worker_running = bool(worker_pids)
        try:
            import psycopg

            with psycopg.connect(self.secrets["CAREERCLOUD_DATABASE_URL"], connect_timeout=10) as conn:
                self.db_ok = conn.execute("select 1").fetchone()[0] == 1
        except Exception as e:  # noqa: BLE001
            if self.db_ok is not False:
                LOG.warning("Database (Supabase PostgreSQL) not reachable (%s)", type(e).__name__)
            self.db_ok = False
        try:
            r = self.redis()
            self.queue_ok = bool(r.ping())
            key = f"{self.settings.get('queue_prefix')}:platform:workers"
            now = time.time()
            # Other workers on the same queue (another PC, a dev worker here) share this
            # ZSET: only OUR worker process's own heartbeat counts.
            self.workers_online = int(r.zcount(key, now - 120, "+inf"))
            beats = r.zrangebyscore(key, now - 86400, "+inf", withscores=True)
            self.heartbeat_age = C.own_heartbeat_age(beats, socket.gethostname(), worker_pids, now)
        except Exception as e:  # noqa: BLE001
            if self.queue_ok is not False:
                LOG.warning("Queue (Upstash Redis) not reachable (%s)", type(e).__name__)
            self.queue_ok = False
            self._redis = None
        # A worker process that is alive but has not heart-beaten for minutes is restarted.
        w = self.svc["worker"]
        if worker_running and self.queue_ok and not self.worker_heartbeat_ok() and time.time() - w.started > 180:
            w.fails += 1
        else:
            w.fails = 0
        # Tunnel public health.
        url = self.find_public_url()
        if url and url != self.public_url:
            self.public_url = url
            P.tunnel_url.write_text(url, encoding="ascii")
            LOG.info("Tunnel URL: %s", url)
        if self.public_url:
            ok = http_ok(self.public_url + "/api/v1/health", 15)
            if ok and not self.public_ok:
                LOG.info("Tunnel answering publicly")
            self.public_ok = ok
            t = self.svc["tunnel"]
            t.fails = 0 if ok else t.fails + 1
            if ok:
                self.publish()
            elif t.fails in (1, 5):
                LOG.warning("Tunnel %s not answering (check %d)", self.public_url, t.fails)

    def worker_heartbeat_ok(self) -> bool:
        return self.heartbeat_age is not None and self.heartbeat_age < 120

    # --- one pass ---------------------------------------------------------------------------
    def tick(self):
        now = time.time()
        found = self.procs()
        api = self.dedupe("API", found["api"])
        worker = self.dedupe("worker", found["worker"])
        tunnel = self.dedupe("tunnel", found["tunnel"])

        s = self.svc["api"]
        api_ok = bool(api) and http_ok(self.local_health, 5)
        if not api:
            if now >= s.next_try:
                if not self.first:
                    LOG.warning("API not running; starting it")
                self.start_api()
                s.started = now
                s.backoff()
                for _ in range(30):
                    if http_ok(self.local_health, 3):
                        api_ok = True
                        LOG.info("API healthy (%s)", self.local_health)
                        break
                    time.sleep(2)
        elif now - s.started > 60:
            if api_ok:
                s.fails = 0
            else:
                s.fails += 1
                if s.fails >= 3:
                    LOG.warning("API not answering for 3 checks; restarting")
                    for p in api:
                        C.kill_tree(p)
                    s.fails, s.next_try = 0, now

        w = self.svc["worker"]
        if not worker and api_ok and now >= w.next_try:
            if not self.first:
                LOG.warning("Worker not running; starting it")
            self.start_worker()
            w.started = now
            w.backoff()
        elif worker and w.fails >= 3:
            LOG.warning("Worker alive but no heartbeat for minutes; restarting")
            for p in worker:
                C.kill_tree(p)
            w.fails = 0

        t = self.svc["tunnel"]
        if not tunnel and api_ok and now >= t.next_try:
            self.start_tunnel()
            t.started = now
            t.backoff()
            self.last_deep = 0  # look for the new URL soon
        elif tunnel and t.fails >= 10:
            LOG.warning("Tunnel not answering for 10 minutes; restarting it")
            for p in tunnel:
                C.kill_tree(p)
            t.fails = 0

        # Deep checks every minute (every 10 s while the tunnel URL is not known yet).
        interval = 60 if self.public_ok else 10
        if now - self.last_deep >= interval:
            self.last_deep = now
            self.deep_checks([p.pid for p in worker])

        for svc in self.svc.values():
            if now - svc.started > 600:
                svc.restarts = 0
        self.first = False
        self.write_status(api, worker, tunnel, api_ok)

    def write_status(self, api, worker, tunnel, api_ok):
        worker_ok = bool(worker) and self.worker_heartbeat_ok()
        tunnel_ok = bool(tunnel) and self.public_ok
        ready = api_ok and worker_ok and tunnel_ok and bool(self.db_ok) and self.published
        C.write_json_atomic(P.status, {
            "updated": time.time(),
            "supervisor_pid": __import__("os").getpid(),
            "api": {"online": api_ok, "running": bool(api), "pid": api[0].pid if api else None,
                    "url": self.local_health},
            "worker": {"online": worker_ok, "running": bool(worker), "pid": worker[0].pid if worker else None,
                       "heartbeat_age_s": self.heartbeat_age, "workers_online": self.workers_online},
            "tunnel": {"online": tunnel_ok, "running": bool(tunnel), "pid": tunnel[0].pid if tunnel else None,
                       "url": self.public_url, "published": self.published},
            "database": self.db_ok,
            "queue": self.queue_ok,
            "overall": "READY" if ready else "STARTING",
        })

    def shutdown(self):
        LOG.info("Stop requested; stopping services")
        self.unpublish()
        for plist in self.procs().values():
            for p in plist:
                C.kill_tree(p)
        C.write_json_atomic(P.status, {"updated": time.time(), "overall": "STOPPED", "supervisor_pid": None})
        LOG.info("Stopped")


def acquire_mutex():
    name = "SanaGtmSupervisor-" + C.instance_id(P.root)
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    for prefix in ("Global\\", "Local\\"):
        h = k32.CreateMutexW(None, True, prefix + name)
        if h:
            if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                return None
            return h
    return None


def main():
    if C.stop_requested(P):
        return 0  # the user pressed Stop; the watchdog trigger must not undo that
    mutex = acquire_mutex()
    if not mutex:
        return 0  # another supervisor for this installation is running
    setup_logging()
    LOG.info("Supervisor started (install %s)", P.root)
    try:
        sup = Supervisor()
    except SystemExit as e:
        LOG.error("Cannot start: %s. Run SANA-GTM-Setup.exe to complete the configuration.", e)
        C.write_json_atomic(P.status, {"updated": time.time(), "overall": "NOT CONFIGURED", "error": str(e)})
        return 1
    while not C.stop_requested(P):
        try:
            sup.tick()
        except Exception as e:  # noqa: BLE001 - one bad pass must not end the supervisor
            LOG.exception("Supervisor pass failed: %s", type(e).__name__)
        for _ in range(PASS_S):
            if C.stop_requested(P):
                break
            time.sleep(1)
    sup.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
