"""Offline tests for the SANA GTM Windows installer, launcher, health checks and startup logic.

    python -m unittest discover -s deploy\\windows\\installer\\tests -t deploy\\windows\\installer\\tests

No network, no services, no scheduled task is registered: schtasks / Popen are
mocked except where a real hidden child process is the point of the test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent
DEV = INSTALLER.parent / "sana-gtm"
sys.path.insert(0, str(INSTALLER / "manager"))
sys.path.insert(0, str(INSTALLER))

import sanagtm_common as C  # noqa: E402

SECRET = "pw-DO-NOT-LEAK-7f3a9c"


class TempInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "SANA GTM"
        self.paths = C.Paths(self.root)
        self.paths.ensure()

    def tearDown(self):
        self._tmp.cleanup()


class OwnHeartbeat(unittest.TestCase):
    NOW = 1_000_000.0

    def test_dev_worker_on_same_pc_does_not_count(self):
        beats = [("PC1-5000-abcdef", self.NOW - 5)]          # a dev worker, fresh
        self.assertIsNone(C.own_heartbeat_age(beats, "PC1", [7000], self.NOW))

    def test_own_worker_counts(self):
        beats = [("PC1-5000-abcdef", self.NOW - 5), ("PC1-7000-0a1b2c", self.NOW - 40)]
        self.assertEqual(C.own_heartbeat_age(beats, "PC1", [7000], self.NOW), 40.0)

    def test_other_pc_with_same_pid_does_not_count(self):
        beats = [("OTHERPC-7000-abcdef", self.NOW - 1)]
        self.assertIsNone(C.own_heartbeat_age(beats, "PC1", [7000], self.NOW))

    def test_pid_prefix_is_not_a_match(self):
        beats = [("PC1-70000-abcdef", self.NOW - 1)]
        self.assertIsNone(C.own_heartbeat_age(beats, "PC1", [7000], self.NOW))

    def test_hostname_case_and_regex_chars(self):
        beats = [("my.pc-7000-abcdef", self.NOW - 3)]
        self.assertEqual(C.own_heartbeat_age(beats, "MY.PC", [7000], self.NOW), 3.0)
        self.assertIsNone(C.own_heartbeat_age([("myXpc-7000-abcdef", self.NOW)], "my.pc", [7000], self.NOW))

    def test_newest_of_several_restarts(self):
        beats = [("PC1-7000-aaaaaa", self.NOW - 90), ("PC1-7000-bbbbbb", self.NOW - 10)]
        self.assertEqual(C.own_heartbeat_age(beats, "PC1", [7000], self.NOW), 10.0)

    def test_no_worker_process_means_offline(self):
        beats = [("PC1-7000-abcdef", self.NOW - 1)]
        self.assertIsNone(C.own_heartbeat_age(beats, "PC1", [], self.NOW))


class SupervisorWorkerStatus(unittest.TestCase):
    def test_dead_test_worker_is_not_online_even_with_fresh_dev_heartbeat(self):
        import supervisor

        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.settings = {"queue_prefix": "p"}
        sup.secrets = {"CAREERCLOUD_DATABASE_URL": "postgresql://x", "CAREERCLOUD_REDIS_URL": "rediss://x"}
        sup.svc = {n: supervisor.Service(n) for n in ("api", "worker", "tunnel")}
        sup.public_url, sup.public_ok, sup.db_ok, sup.queue_ok = "", False, None, None
        sup.heartbeat_age, sup.workers_online, sup.published = None, 0, False
        now = time.time()
        fake = mock.Mock()
        fake.ping.return_value = True
        fake.zcount.return_value = 1
        fake.zrangebyscore.return_value = [(f"{supervisor.socket.gethostname()}-4242-abcdef", now - 2)]
        sup._redis = fake
        with mock.patch.dict(sys.modules, {"psycopg": mock.Mock()}), \
                mock.patch.object(sup, "find_public_url", return_value=""):
            sup.deep_checks([])                      # our worker is dead
            self.assertFalse(sup.worker_heartbeat_ok())
            sup.deep_checks([9999])                  # our worker runs, but it is not pid 4242
            self.assertFalse(sup.worker_heartbeat_ok())
            sup.deep_checks([4242])
            self.assertTrue(sup.worker_heartbeat_ok())


class Secrets(TempInstall):
    def test_roundtrip_and_encrypted_at_rest(self):
        C.save_secrets(self.paths, {"CAREERCLOUD_DATABASE_URL": f"postgresql://u:{SECRET}@h/db",
                                    "CAREERCLOUD_REDIS_URL": "rediss://x", "NOT_A_SECRET_KEY": "dropped",
                                    "GEMINI_API_KEY": ""})
        raw = self.paths.secrets.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(b"postgresql", raw)
        back = C.load_secrets(self.paths)
        self.assertEqual(back["CAREERCLOUD_DATABASE_URL"], f"postgresql://u:{SECRET}@h/db")
        self.assertNotIn("NOT_A_SECRET_KEY", back)
        self.assertNotIn("GEMINI_API_KEY", back)   # blanks are not stored

    def test_tampered_file_is_rejected(self):
        C.save_secrets(self.paths, {"CAREERCLOUD_REDIS_URL": "rediss://x"})
        data = bytearray(self.paths.secrets.read_bytes())
        data[-5] ^= 0xFF
        self.paths.secrets.write_bytes(bytes(data))
        with self.assertRaises(OSError):
            C.load_secrets(self.paths)

    def test_settings_json_never_holds_secrets(self):
        import setup_wizard as S

        inst = S.Installer(self.root, {"supabase_url": "https://ref.supabase.co"},
                           {"CAREERCLOUD_DATABASE_URL": f"postgresql://u:{SECRET}@h/db",
                            "CAREERCLOUD_REDIS_URL": f"rediss://default:{SECRET}@h:6379",
                            "CAREERCLOUD_PLATFORM_SECRETS_KEY": SECRET * 2}, lambda *a: None)
        inst.save_config()
        self.assertNotIn(SECRET, self.paths.settings.read_text(encoding="utf-8"))


class ServiceEnv(TempInstall):
    def test_env_for_api_and_worker(self):
        settings = C.load_settings(self.paths)
        settings["supabase_url"] = "https://ref.supabase.co"
        secrets = {"CAREERCLOUD_DATABASE_URL": "postgresql://a", "CAREERCLOUD_REDIS_URL": "rediss://b",
                   "CAREERCLOUD_PLATFORM_SECRETS_KEY": "k" * 32, "TUNNEL_TOKEN": "tok"}
        with mock.patch.dict(os.environ, {"CAREERCLOUD_ENV": "production", "GEMINI_API_KEY": "stray"}):
            env = C.service_env(self.paths, settings, secrets)
        self.assertEqual(env["CAREERCLOUD_ENV"], "development")      # never inherited
        self.assertNotIn("GEMINI_API_KEY", env)                       # not configured -> not inherited
        self.assertNotIn("TUNNEL_TOKEN", env)                         # only cloudflared gets it
        self.assertEqual(env["CAREERCLOUD_DATABASE_URL"], "postgresql://a")
        self.assertEqual(env["PYTHONPATH"], str(self.paths.app))
        self.assertEqual(env["CAREERCLOUD_PLATFORM_FILES_DIR"], str(self.paths.data / "files"))


class UpdateKeepsConfiguration(TempInstall):
    def _payload(self, version):
        z = Path(self._tmp.name) / f"payload-{version}.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("version.json", json.dumps({"version": version}))
            f.writestr("manager/marker.txt", version)
            f.writestr("app/cloud/__init__.py", "")
        return z

    def test_update_replaces_program_and_keeps_config_data_logs(self):
        import setup_wizard as S

        report = lambda *a: None  # noqa: E731
        with mock.patch.object(S, "PAYLOAD", self._payload("1.0.0")):
            first = S.Installer(self.root, {"supabase_url": "https://ref.supabase.co"},
                                {"CAREERCLOUD_DATABASE_URL": "postgresql://one", "CAREERCLOUD_REDIS_URL": "rediss://one",
                                 "CAREERCLOUD_PLATFORM_SECRETS_KEY": "s" * 32}, report)
            first.extract()
            first.save_config()
        settings = C.load_settings(self.paths)
        settings["env"]["CAREERCLOUD_LOG_LEVEL"] = "DEBUG"             # a local customisation
        del settings["env"]["CAREERCLOUD_JOB_CREATE_PER_HOUR"]          # a key a newer version adds
        C.save_settings(self.paths, settings)
        (self.paths.logs / "api.log").write_text("old log")
        (self.paths.data / "files" / "keep.txt").write_text("user data")
        (self.root / "manager" / "stale.py").write_text("removed by update")

        with mock.patch.object(S, "PAYLOAD", self._payload("1.1.0")):
            upd = S.Installer(self.root, {}, {}, report)               # update: no secrets entered
            self.assertTrue(upd.update)
            upd.extract()
            upd.save_config()
        self.assertEqual((self.root / "manager" / "marker.txt").read_text(), "1.1.0")
        self.assertFalse((self.root / "manager" / "stale.py").exists())
        self.assertEqual(C.load_secrets(self.paths)["CAREERCLOUD_DATABASE_URL"], "postgresql://one")
        s = C.load_settings(self.paths)
        self.assertEqual(s["env"]["CAREERCLOUD_LOG_LEVEL"], "DEBUG")
        self.assertEqual(s["env"]["CAREERCLOUD_JOB_CREATE_PER_HOUR"], "20")
        self.assertEqual(s["supabase_url"], "https://ref.supabase.co")
        self.assertEqual((self.paths.logs / "api.log").read_text(), "old log")
        self.assertTrue((self.paths.data / "files" / "keep.txt").exists())

    def test_blank_secret_on_change_keeps_saved_one(self):
        import setup_wizard as S

        base = {"CAREERCLOUD_DATABASE_URL": "postgresql://one", "CAREERCLOUD_REDIS_URL": "rediss://one",
                "CAREERCLOUD_PLATFORM_SECRETS_KEY": "s" * 32}
        S.Installer(self.root, {}, base, lambda *a: None).save_config()
        S.Installer(self.root, {}, {"CAREERCLOUD_REDIS_URL": "rediss://two", "CAREERCLOUD_DATABASE_URL": ""},
                    lambda *a: None).save_config()
        back = C.load_secrets(self.paths)
        self.assertEqual(back["CAREERCLOUD_DATABASE_URL"], "postgresql://one")
        self.assertEqual(back["CAREERCLOUD_REDIS_URL"], "rediss://two")

    def test_first_install_without_required_secrets_fails(self):
        import setup_wizard as S

        with self.assertRaises(RuntimeError):
            S.Installer(self.root, {}, {"CAREERCLOUD_REDIS_URL": "rediss://x"}, lambda *a: None).save_config()


class SetupConfigInput(unittest.TestCase):
    def test_env_file_import_and_checks(self):
        import setup_wizard as S

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env.sana-cloud"
            f.write_text("# comment\nCAREERCLOUD_DATABASE_URL=postgresql://u:p@h/db\n"
                         "CAREERCLOUD_REDIS_URL=\"rediss://default:x@h:6379\"\nCAREERCLOUD_PLATFORM_SECRETS_KEY=" + "k" * 40 +
                         "\nCAREERCLOUD_SUPABASE_URL=https://ref.supabase.co/\nCAREERCLOUD_QUEUE_PREFIX=sanagtm:staging\n"
                         "CAREERCLOUD_ENV=production\n", encoding="utf-8")
            settings, secrets = S.config_from_env(S.read_env_file(f))
        self.assertEqual(settings, {"supabase_url": "https://ref.supabase.co", "queue_prefix": "sanagtm:staging"})
        self.assertEqual(set(secrets), {"CAREERCLOUD_DATABASE_URL", "CAREERCLOUD_REDIS_URL",
                                        "CAREERCLOUD_PLATFORM_SECRETS_KEY"})
        self.assertEqual(S.check_values(settings, secrets), [])
        self.assertTrue(S.check_values({"supabase_url": "http://x"}, {}))
        self.assertTrue(any("named" in p for p in S.check_values(
            {"supabase_url": "https://x", "tunnel_mode": "named"}, secrets)))


class StopFlag(TempInstall):
    def test_stop_holds_this_boot_only(self):
        self.assertFalse(C.stop_requested(self.paths))
        C.request_stop(self.paths)
        self.assertTrue(C.stop_requested(self.paths))
        self.paths.stop_flag.write_text(str(int(C.boot_id()) - 500), encoding="ascii")   # an earlier boot
        self.assertFalse(C.stop_requested(self.paths))
        self.paths.stop_flag.write_text("garbage", encoding="ascii")
        self.assertFalse(C.stop_requested(self.paths))
        C.request_stop(self.paths)
        C.clear_stop(self.paths)
        self.assertFalse(C.stop_requested(self.paths))

    def test_boot_id_is_stable(self):
        self.assertLessEqual(abs(int(C.boot_id()) - int(C.boot_id())), 1)

    def test_supervisor_exits_at_once_when_stopped(self):
        import supervisor

        with mock.patch.object(supervisor.C, "stop_requested", return_value=True), \
                mock.patch.object(supervisor, "acquire_mutex") as mutex:
            self.assertEqual(supervisor.main(), 0)
            mutex.assert_not_called()   # the watchdog trigger must not undo a Stop


class StartupTask(TempInstall):
    def test_task_xml(self):
        captured = {}

        def fake_run(args, **kw):
            if "/XML" in args:
                captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(C, "run_hidden", side_effect=fake_run) as run:
            C.register_task(self.paths)
        args = run.call_args[0][0]
        self.assertEqual(args[:4], ["schtasks.exe", "/Create", "/TN", "SANA GTM"])
        self.assertFalse((self.paths.state / "task.xml").exists())      # temp file removed
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(captured["xml"].split("?>", 1)[1])
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", ns).text, str(self.paths.pythonw))
        self.assertIn("supervisor.py", root.find("t:Actions/t:Exec/t:Arguments", ns).text)
        self.assertIsNotNone(root.find("t:Triggers/t:LogonTrigger", ns))
        self.assertEqual(root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text, "PT2M")
        s = root.find("t:Settings", ns)
        self.assertEqual(s.find("t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
        self.assertEqual(s.find("t:Hidden", ns).text, "true")
        self.assertEqual(s.find("t:ExecutionTimeLimit", ns).text, "PT0S")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", ns).text, "LeastPrivilege")
        text = captured["xml"].lower()
        for bad in ("powershell", "cmd.exe", "python.exe\""):
            self.assertNotIn(bad, text)

    def test_register_failure_is_reported(self):
        with mock.patch.object(C, "run_hidden",
                               return_value=subprocess.CompletedProcess([], 1, "", "ERROR: Access is denied.")):
            with self.assertRaisesRegex(RuntimeError, "Access is denied"):
                C.register_task(self.paths)

    def test_start_uses_task_then_falls_back_to_hidden_spawn(self):
        with mock.patch.object(C, "task_exists", return_value=True), mock.patch.object(C, "run_task", return_value=True), \
                mock.patch.object(C, "spawn_hidden") as spawn:
            C.start_supervisor(self.paths)
            spawn.assert_not_called()
        with mock.patch.object(C, "task_exists", return_value=False), mock.patch.object(C, "spawn_hidden") as spawn:
            C.start_supervisor(self.paths)
            self.assertEqual(spawn.call_args[0][0][0], str(self.paths.pythonw))


class NoWindows(TempInstall):
    def test_spawn_hidden_uses_create_no_window(self):
        with mock.patch.object(C.subprocess, "Popen") as popen:
            C.spawn_hidden(["x.exe"], stdout=self.paths.logs / "a.log", stderr=self.paths.logs / "a.log")
        kw = popen.call_args.kwargs
        self.assertTrue(kw["creationflags"] & C.CREATE_NO_WINDOW)
        self.assertEqual(kw["startupinfo"].wShowWindow, 0)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_run_hidden_uses_create_no_window(self):
        with mock.patch.object(C.subprocess, "run") as run:
            C.run_hidden(["schtasks.exe", "/Query"])
        self.assertTrue(run.call_args.kwargs["creationflags"] & C.CREATE_NO_WINDOW)

    def test_real_child_has_no_console_window(self):
        """A process started like the API/worker has a console but no window (console_window.py)."""
        log = self.paths.logs / "child.log"
        proc = C.spawn_hidden([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(20)"],
                              stdout=log, stderr=log)
        try:
            for _ in range(50):
                if log.exists() and "up" in log.read_text():
                    break
                time.sleep(0.1)
            out = subprocess.run([sys.executable, str(DEV / "console_window.py"), str(proc.pid)],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=C.CREATE_NO_WINDOW).stdout
            result = json.loads(out.strip().splitlines()[-1])
            self.assertTrue(result["console"])
            self.assertFalse(result["visible"])
        finally:
            proc.kill()
            proc.wait(10)

    def test_dev_launch_hidden(self):
        """deploy\\windows\\sana-gtm\\launch_hidden.py (the dev supervisor's launcher)."""
        log = self.paths.logs / "lh.log"
        r = subprocess.run([sys.executable, str(DEV / "launch_hidden.py"), "--stdout", str(log), "--wait", "--",
                            sys.executable, "-c", "print('hello from hidden')"],
                           capture_output=True, text=True, timeout=60, creationflags=C.CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello from hidden", log.read_text())
        r = subprocess.run([sys.executable, str(DEV / "launch_hidden.py"), "--", sys.executable, "-c", "pass"],
                           capture_output=True, text=True, timeout=60, creationflags=C.CREATE_NO_WINDOW)
        self.assertTrue(r.stdout.strip().isdigit())    # prints the PID without --wait


class Classify(unittest.TestCase):
    def _p(self, exe, cmd):
        return mock.Mock(info={"exe": exe, "cmdline": cmd})

    def test_kinds(self):
        rt = r"C:\SANA GTM\runtime\python.exe"
        cases = {
            "api": self._p(rt, [rt, "-m", "uvicorn", "cloud.api.main:app", "--port", "8100"]),
            "worker": self._p(rt, [rt, "-m", "cloud.intel.tasks.worker"]),
            "tunnel": self._p(r"C:\SANA GTM\bin\cloudflared.exe", ["cloudflared", "tunnel"]),
            "supervisor": self._p(r"C:\SANA GTM\runtime\pythonw.exe", ["pythonw", r"C:\SANA GTM\manager\supervisor.py"]),
            "panel": self._p(r"C:\SANA GTM\runtime\pythonw.exe", ["pythonw", "control_panel.pyw"]),
            "other": self._p(rt, [rt, "-c", "pass"]),
        }
        for kind, proc in cases.items():
            self.assertEqual(C.classify(proc), kind)

    def test_owned_processes_ignores_other_installs(self):
        with tempfile.TemporaryDirectory() as d:
            paths = C.Paths(Path(d) / "SANA GTM")
            self.assertEqual(C.owned_processes(paths), [])    # nothing runs from a fresh folder


class HealthChecksNeverLeakSecrets(unittest.TestCase):
    def _run(self, args, stdin=None):
        return subprocess.run([sys.executable, *args], input=stdin, capture_output=True, text=True, timeout=120,
                              creationflags=C.CREATE_NO_WINDOW)

    def test_validate_config_with_unreachable_services(self):
        payload = json.dumps({"secrets": {"CAREERCLOUD_DATABASE_URL": f"postgresql://u:{SECRET}@127.0.0.1:1/db",
                                          "CAREERCLOUD_REDIS_URL": f"redis://default:{SECRET}@127.0.0.1:1"},
                              "settings": {"supabase_url": "https://127.0.0.1:1"}})
        r = self._run([str(INSTALLER / "manager" / "validate_config.py")], payload)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertFalse(out["database"] or out["queue"] or out["supabase"])
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_dev_healthcheck_with_unreachable_services(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "worker.env"
            env.write_text(f"CAREERCLOUD_DATABASE_URL=postgresql://u:{SECRET}@127.0.0.1:1/db\n"
                           f"CAREERCLOUD_REDIS_URL=redis://default:{SECRET}@127.0.0.1:1\n", encoding="utf-8")
            r = self._run([str(DEV / "healthcheck.py"), str(env)])
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertFalse(out["database"] or out["queue"] or out["worker_heartbeat"])
        self.assertNotIn(SECRET, r.stdout + r.stderr)


class Sources(unittest.TestCase):
    def test_everything_compiles(self):
        import py_compile

        for f in list((INSTALLER / "manager").glob("*.py*")) + [INSTALLER / "setup_wizard.py",
                                                                INSTALLER / "build_installer.py"]:
            py_compile.compile(str(f), doraise=True)

    def test_manager_never_uses_visible_launch_styles(self):
        for f in (INSTALLER / "manager").glob("*.py*"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("-WindowStyle", text, f.name)
            self.assertNotIn("os.system(", text, f.name)
            self.assertNotIn("shell=True", text, f.name)


if __name__ == "__main__":
    unittest.main()
