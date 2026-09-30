"""Offline tests for the finished installer pieces: wizard rules, DPAPI-backed config
(through a fake DPAPI, so they run on any OS), staged update + rollback, the startup
task definition, the headless control panel, uninstall plans/dry-run and verify.py.

Nothing here registers a task, starts SANA GTM, touches the network or builds an EXE.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
INSTALLER = HERE.parent
sys.path.insert(0, str(INSTALLER / "manager"))
sys.path.insert(0, str(INSTALLER))

import config_rules as R  # noqa: E402
import panel_logic as L  # noqa: E402
import sanagtm_common as C  # noqa: E402
from updater import Updater, UpdateError  # noqa: E402

SECRET = "pw-DO-NOT-LEAK-7f3a9c"
GOOD_SETTINGS = {"supabase_url": "https://zqbbcaehvpstunxphxxj.supabase.co", "api_port": 8100,
                 "queue_prefix": "sanagtm:staging"}
GOOD_SECRETS = {"CAREERCLOUD_DATABASE_URL": f"postgresql://postgres:{SECRET}@db.example.supabase.co:5432/postgres",
                "CAREERCLOUD_REDIS_URL": f"rediss://default:{SECRET}@eu1-sharp-cat-1.upstash.io:6379",
                "CAREERCLOUD_PLATFORM_SECRETS_KEY": "k" * 44}


class FakeDpapi:
    """Stand-in for CryptProtectData: keyed per 'user', authenticated, tamper-evident."""

    def __init__(self, user_key: bytes = b"user-A"):
        self.key = user_key

    def _stream(self, n: int) -> bytes:
        out, counter = b"", 0
        while len(out) < n:
            out += hashlib.sha256(self.key + counter.to_bytes(4, "big")).digest()
            counter += 1
        return out[:n]

    def protect(self, data: bytes) -> bytes:
        body = bytes(a ^ b for a, b in zip(data, self._stream(len(data))))
        return hmac.new(self.key, body, hashlib.sha256).digest() + body

    def unprotect(self, blob: bytes) -> bytes:
        mac, body = blob[:32], blob[32:]
        if not hmac.compare_digest(mac, hmac.new(self.key, body, hashlib.sha256).digest()):
            raise OSError("CryptUnprotectData failed: 13 (secrets.dat belongs to another Windows user or PC)")
        return bytes(a ^ b for a, b in zip(body, self._stream(len(body))))


class TempRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "SANA GTM"
        self.paths = C.Paths(self.root)
        self.paths.ensure()
        self._old_dpapi = C.set_dpapi_backend(FakeDpapi())

    def tearDown(self):
        C.set_dpapi_backend(self._old_dpapi)
        self._tmp.cleanup()

    def payload(self, version, extra=None):
        z = Path(self._tmp.name) / f"payload-{version}.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("version.json", json.dumps({"version": version}))
            f.writestr("manager/marker.txt", version)
            f.writestr("app/cloud/__init__.py", "")
            f.writestr("runtime/python.exe", "")
            for name, text in (extra or {}).items():
                f.writestr(name, text)
        return z


# --- wizard ------------------------------------------------------------------------------------

class WizardSteps(unittest.TestCase):
    def test_first_install_update_and_change(self):
        self.assertEqual(R.wizard_steps(False), ["welcome", "folder", "configuration", "review", "install", "finish"])
        self.assertEqual(R.wizard_steps(True), ["welcome", "install", "finish"])
        self.assertEqual(R.wizard_steps(True, True), ["welcome", "configuration", "review", "install", "finish"])

    def test_review_masks_secrets(self):
        self.assertNotIn(SECRET, R.mask(SECRET))
        self.assertEqual(R.mask(""), "not set")


class WizardValidation(unittest.TestCase):
    def fields(self, settings=None, secrets=None, **kw):
        s = {**GOOD_SETTINGS, **(settings or {})}
        k = {**GOOD_SECRETS, **(secrets or {})}
        return {p["field"] for p in R.check_config(s, k, **kw)}

    def test_good_configuration_passes(self):
        self.assertEqual(R.check_config(GOOD_SETTINGS, GOOD_SECRETS), [])

    def test_supabase_url(self):
        for bad in ("", "http://zqbbcaehvpstunxphxxj.supabase.co", "https://ref.supabase.co/rest/v1",
                    "https://UPPER_case!.supabase.co", "https://user:pw@x.supabase.co", "https://localhost"):
            self.assertIn("supabase_url", self.fields({"supabase_url": bad}), bad)

    def test_database_url(self):
        for bad in ("", "mysql://u:p@h/db", "postgresql://u@db.x.co/postgres", "postgresql://u:p@localhost/db"):
            self.assertIn("CAREERCLOUD_DATABASE_URL", self.fields(secrets={"CAREERCLOUD_DATABASE_URL": bad}), bad)

    def test_upstash_urls_and_token(self):
        self.assertIn("CAREERCLOUD_REDIS_URL", self.fields(secrets={
            "CAREERCLOUD_REDIS_URL": "redis://default:x@eu1-cat.upstash.io:6379"}))       # Upstash needs TLS
        self.assertIn("CAREERCLOUD_REDIS_URL", self.fields(secrets={
            "CAREERCLOUD_REDIS_URL": "rediss://eu1-cat.upstash.io:6379"}))                # no password
        self.assertIn("CAREERCLOUD_REDIS_URL", self.fields(secrets={"CAREERCLOUD_REDIS_URL": "https://x"}))
        self.assertIn("upstash_rest_url", self.fields({"upstash_rest_url": "http://x.upstash.io"},
                                                      {"UPSTASH_REST_TOKEN": "A" * 40}))
        self.assertIn("upstash_rest_url", self.fields({"upstash_rest_url": "https://x.upstash.io"}))  # token missing
        self.assertIn("UPSTASH_REST_TOKEN", self.fields({"upstash_rest_url": "https://x.upstash.io"},
                                                        {"UPSTASH_REST_TOKEN": "short token!"}))
        self.assertEqual(self.fields({"upstash_rest_url": "https://x.upstash.io"}, {"UPSTASH_REST_TOKEN": "A" * 40}),
                         set())

    def test_supabase_key_format(self):
        self.assertIn("supabase_anon_key", self.fields({"supabase_anon_key": "not-a-key"}))
        self.assertEqual(self.fields({"supabase_anon_key": "eyJhbGciOiJI.eyJpc3MiOiJzdXBh.c2lnbmF0dXJlLXg"}), set())
        self.assertEqual(self.fields({"supabase_anon_key": "sb_publishable_abcdefghijklmnop123"}), set())

    def test_api_port(self):
        for bad in ("abc", 80, 70000, None):
            self.assertIn("api_port", self.fields({"api_port": bad}), bad)
        self.assertEqual(self.fields({"api_port": "8100"}), set())

    def test_platform_secret_queue_prefix_and_named_tunnel(self):
        self.assertIn("CAREERCLOUD_PLATFORM_SECRETS_KEY",
                      self.fields(secrets={"CAREERCLOUD_PLATFORM_SECRETS_KEY": "short"}))
        self.assertIn("queue_prefix", self.fields({"queue_prefix": "Bad Prefix"}))
        self.assertIn("TUNNEL_TOKEN", self.fields({"tunnel_mode": "named"}))

    def test_install_dir(self):
        good = r"C:\Users\me\AppData\Local\Programs\SANA GTM"
        self.assertIsNone(R.check_install_dir(good))
        for bad in ("", "relative\\dir", "C:\\", r"C:\Windows\SANA", r"C:\Program Files\SANA GTM",
                    r"\\server\share\SANA", r"C:\Users\me\CON\x", r"C:\Users\me\bad|name", "C:\\" + "x" * 200):
            self.assertIsNotNone(R.check_install_dir(bad), bad)
        self.assertIn("install_dir", self.fields(install_dir=r"C:\Program Files\SANA GTM"))

    def test_messages_never_echo_values(self):
        leaky_settings = {"supabase_url": f"http://{SECRET}.example", "api_port": SECRET,
                          "queue_prefix": f"Bad {SECRET}", "upstash_rest_url": f"http://{SECRET}",
                          "supabase_anon_key": SECRET}
        leaky_secrets = {"CAREERCLOUD_DATABASE_URL": f"mysql://u:{SECRET}@h/db",
                         "CAREERCLOUD_REDIS_URL": f"redis://default:{SECRET}@x.upstash.io:6379",
                         "CAREERCLOUD_PLATFORM_SECRETS_KEY": SECRET[:5], "UPSTASH_REST_TOKEN": f"{SECRET} !"}
        problems = R.check_config(leaky_settings, leaky_secrets, install_dir=f"C:\\Windows\\{SECRET}")
        self.assertGreaterEqual(len(problems), 7)
        self.assertNotIn(SECRET, json.dumps(problems))
        self.assertNotIn(SECRET[:5], json.dumps(problems))

    def test_setup_wizard_check_values_delegates(self):
        import setup_wizard as S

        self.assertEqual(S.check_values(GOOD_SETTINGS, GOOD_SECRETS), [])
        self.assertTrue(any("Program Files" in p for p in S.check_values(
            GOOD_SETTINGS, GOOD_SECRETS, r"C:\Program Files\SANA GTM")))


# --- encrypted configuration ---------------------------------------------------------------------

class EncryptedConfig(TempRoot):
    def test_roundtrip_through_mockable_dpapi(self):
        C.save_secrets(self.paths, {**GOOD_SECRETS, "UNKNOWN": "dropped", "GEMINI_API_KEY": ""})
        raw = self.paths.secrets.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(b"postgresql", raw)
        back = C.load_secrets(self.paths)
        self.assertEqual(back, GOOD_SECRETS)

    def test_tamper_is_rejected(self):
        C.save_secrets(self.paths, GOOD_SECRETS)
        data = bytearray(self.paths.secrets.read_bytes())
        data[40] ^= 0x01
        self.paths.secrets.write_bytes(bytes(data))
        with self.assertRaises(OSError):
            C.load_secrets(self.paths)

    def test_another_windows_user_cannot_decrypt(self):
        C.save_secrets(self.paths, GOOD_SECRETS)
        C.set_dpapi_backend(FakeDpapi(b"user-B"))
        with self.assertRaises(OSError):
            C.load_secrets(self.paths)
        status = C.secrets_status(self.paths)
        self.assertFalse(status["decryptable"])
        self.assertEqual(status["present"], [])

    def test_decrypted_garbage_is_an_error_not_partial_data(self):
        self.paths.secrets.write_bytes(C.dpapi_protect(b"\xff\xfe not json"))
        with self.assertRaises(OSError):
            C.load_secrets(self.paths)
        self.paths.secrets.write_bytes(C.dpapi_protect(b"[1, 2]"))
        with self.assertRaises(OSError):
            C.load_secrets(self.paths)

    def test_status_lists_names_never_values(self):
        C.save_secrets(self.paths, {"CAREERCLOUD_REDIS_URL": GOOD_SECRETS["CAREERCLOUD_REDIS_URL"]})
        status = C.secrets_status(self.paths)
        self.assertTrue(status["decryptable"])
        self.assertEqual(status["present"], ["CAREERCLOUD_REDIS_URL"])
        self.assertIn("CAREERCLOUD_DATABASE_URL", status["missing"])
        self.assertNotIn(SECRET, json.dumps(status))

    def test_real_backend_is_the_default(self):
        C.set_dpapi_backend(self._old_dpapi)
        self.assertIsInstance(C._DPAPI, C.DpapiBackend)
        C.set_dpapi_backend(FakeDpapi())


# --- staged update + rollback --------------------------------------------------------------------

class StagedUpdate(TempRoot):
    def install(self, version, **kw):
        return Updater(self.root, retry_delay=0, **kw).apply(self.payload(version))

    def test_first_install_has_no_backup(self):
        result = self.install("1.0.0")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["backup"])
        self.assertEqual(Updater(self.root).current_version(), "1.0.0")
        self.assertFalse((self.root / "staging" / "1.0.0").exists())

    def test_update_keeps_previous_release_config_data_logs(self):
        self.install("1.0.0")
        C.save_secrets(self.paths, GOOD_SECRETS)
        (self.paths.logs / "api.log").write_text("old log")
        (self.paths.data / "files" / "keep.txt").write_text("user data")
        (self.root / "manager" / "stale.py").write_text("gone after update")
        result = self.install("1.1.0")
        self.assertTrue(result["ok"])
        self.assertEqual((self.root / "manager" / "marker.txt").read_text(), "1.1.0")
        self.assertFalse((self.root / "manager" / "stale.py").exists())
        self.assertEqual((self.root / "versions" / "1.0.0" / "manager" / "marker.txt").read_text(), "1.0.0")
        self.assertEqual(C.load_secrets(self.paths), GOOD_SECRETS)
        self.assertEqual((self.paths.logs / "api.log").read_text(), "old log")
        self.assertTrue((self.paths.data / "files" / "keep.txt").exists())

    def test_failed_health_check_rolls_back(self):
        self.install("1.0.0")
        C.save_secrets(self.paths, GOOD_SECRETS)
        result = Updater(self.root, retry_delay=0).apply(self.payload("2.0.0", {"bin/new-only.exe": "x"}),
                                                         health_check=lambda: False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(Updater(self.root).current_version(), "1.0.0")
        self.assertEqual((self.root / "manager" / "marker.txt").read_text(), "1.0.0")
        self.assertFalse((self.root / "bin").exists())                 # did not exist in 1.0.0
        self.assertEqual(C.load_secrets(self.paths), GOOD_SECRETS)
        self.assertFalse((self.root / "versions" / "1.0.0").exists())  # restored, not duplicated

    def test_health_check_exception_rolls_back(self):
        self.install("1.0.0")

        def boom():
            raise ConnectionError("api did not answer")

        result = Updater(self.root, retry_delay=0).apply(self.payload("2.0.0"), health_check=boom)
        self.assertFalse(result["ok"])
        self.assertIn("ConnectionError", result["error"])
        self.assertEqual(Updater(self.root).current_version(), "1.0.0")

    def test_bad_package_leaves_live_release_untouched(self):
        self.install("1.0.0")
        evil = self.payload("9.9.9", {"config/settings.json": "{}"})           # would overwrite config
        with self.assertRaises(UpdateError):
            Updater(self.root).apply(evil)
        z = Path(self._tmp.name) / "noversion.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("app/x.py", "")
        with self.assertRaises(UpdateError):
            Updater(self.root).apply(z)
        self.assertEqual(Updater(self.root).current_version(), "1.0.0")
        self.assertEqual((self.root / "manager" / "marker.txt").read_text(), "1.0.0")

    def test_prune_keeps_two_previous_releases(self):
        for i, v in enumerate(("1.0.0", "1.1.0", "1.2.0", "1.3.0")):
            self.install(v)
            if (self.root / "versions").exists():
                for j, d in enumerate(sorted((self.root / "versions").iterdir())):
                    os.utime(d, (1_000_000 + j, 1_000_000 + j))
        self.assertEqual(sorted(Updater(self.root).installed_versions()), ["1.1.0", "1.2.0"])

    def test_installer_rolls_back_an_update_that_fails_later(self):
        import setup_wizard as S

        report = lambda *a: None  # noqa: E731
        with mock.patch.object(S, "PAYLOAD", self.payload("1.0.0")):
            first = S.Installer(self.root, dict(GOOD_SETTINGS), dict(GOOD_SECRETS), report)
            first.extract()
            first.save_config()
        with mock.patch.object(S, "PAYLOAD", self.payload("1.1.0")), \
                mock.patch.object(S.Installer, "check_pc", return_value="ok"), \
                mock.patch.object(S.Installer, "stop_existing", return_value="ok"), \
                mock.patch.object(S.Installer, "validate", side_effect=RuntimeError("Could not connect")), \
                mock.patch.object(S.C, "stop_everything") as stop, mock.patch.object(S.C, "start_supervisor") as start:
            upd = S.Installer(self.root, {}, {}, report, start=True)
            self.assertTrue(upd.update)
            with self.assertRaisesRegex(RuntimeError, "Could not connect"):
                upd.run()
        self.assertTrue(upd.rolled_back)
        stop.assert_called_once()
        start.assert_called_once()                                    # the previous version restarted
        self.assertEqual((self.root / "manager" / "marker.txt").read_text(), "1.0.0")
        self.assertEqual(C.load_secrets(self.paths)["CAREERCLOUD_DATABASE_URL"],
                         GOOD_SECRETS["CAREERCLOUD_DATABASE_URL"])

    def test_first_install_failure_does_not_pretend_to_roll_back(self):
        import setup_wizard as S

        with mock.patch.object(S, "PAYLOAD", self.payload("1.0.0")), \
                mock.patch.object(S.Installer, "check_pc", return_value="ok"), \
                mock.patch.object(S.Installer, "stop_existing", return_value="ok"), \
                mock.patch.object(S.Installer, "validate", side_effect=RuntimeError("Could not connect")):
            inst = S.Installer(self.root, dict(GOOD_SETTINGS), dict(GOOD_SECRETS), lambda *a: None, start=False)
            with self.assertRaises(RuntimeError):
                inst.run()
        self.assertFalse(inst.rolled_back)


# --- background startup ----------------------------------------------------------------------------

class StartupDefinition(TempRoot):
    NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def test_definition_is_windowless_per_user(self):
        d = C.task_definition(self.paths)
        self.assertEqual(d["command"], str(self.paths.pythonw))
        self.assertTrue(d["command"].lower().endswith("pythonw.exe"))
        self.assertIn("supervisor.py", d["arguments"])
        self.assertEqual(d["run_level"], "LeastPrivilege")

    def test_xml_is_generated_without_registering(self):
        with mock.patch.object(C, "run_hidden") as run:
            xml = C.task_xml(self.paths, user="PC\\alice", start="2026-01-01T09:00:00")
            run.assert_not_called()
        root = ET.fromstring(xml.split("?>", 1)[1])
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", self.NS).text, "PC\\alice")
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:Delay", self.NS).text, "PT20S")
        self.assertEqual(root.find("t:Triggers/t:TimeTrigger/t:StartBoundary", self.NS).text, "2026-01-01T09:00:00")
        self.assertEqual(root.find("t:Principals/t:Principal/t:LogonType", self.NS).text, "InteractiveToken")
        self.assertEqual(root.find("t:Settings/t:Hidden", self.NS).text, "true")
        self.assertEqual(root.find("t:Actions/t:Exec/t:WorkingDirectory", self.NS).text, str(self.root))

    def test_paths_with_xml_characters_are_escaped(self):
        paths = C.Paths(Path(self._tmp.name) / "R&D <tools>")
        xml = C.task_xml(paths, user="PC\\bob", start="2026-01-01T09:00:00")
        root = ET.fromstring(xml.split("?>", 1)[1])                  # still well-formed
        self.assertIn("R&D <tools>", root.find("t:Actions/t:Exec/t:Command", self.NS).text)


# --- control panel (headless) -------------------------------------------------------------------------

class FakeCommon:
    FRONTEND_URL = C.FRONTEND_URL

    def __init__(self, paths, status=None, alive=True):
        self.calls = []
        self._status = status or {}
        self._alive = alive
        self._paths = paths

    def classify(self, p):
        return p

    def owned_processes(self, paths):
        return ["supervisor"] if self._alive else []

    def read_status(self, paths):
        return self._status

    def stop_requested(self, paths):
        return False

    def start_supervisor(self, paths):
        self.calls.append("start")

    def stop_everything(self, paths):
        self.calls.append("stop")

    def load_settings(self, paths):
        return C.load_settings(paths)

    def spawn_hidden(self, args, cwd=None):
        self.calls.append(("spawn", args))


class PanelView(unittest.TestCase):
    NOW = 2_000_000.0

    def test_not_running_stopped_and_not_configured(self):
        v = L.compute_view({}, False, False, self.NOW)
        self.assertEqual(v["rows"]["Overall"], ("NOT RUNNING", L.RED))
        self.assertEqual(L.compute_view({}, False, True, self.NOW)["rows"]["Overall"][0], "STOPPED")
        v = L.compute_view({"overall": "NOT CONFIGURED", "error": "secrets missing"}, False, False, self.NOW)
        self.assertEqual(v["rows"]["Overall"][0], "NOT CONFIGURED")
        self.assertEqual(v["detail"], "secrets missing")

    def test_starting_when_status_is_stale(self):
        v = L.compute_view({"updated": self.NOW - 120, "api": {"online": True}}, True, False, self.NOW)
        self.assertEqual({t for t, _ in v["rows"].values()}, {"STARTING"})

    def test_ready_and_partial(self):
        st = {"updated": self.NOW, "overall": "READY", "api": {"online": True}, "worker": {"online": True},
              "tunnel": {"online": True, "url": "https://x.trycloudflare.com"}}
        v = L.compute_view(st, True, False, self.NOW)
        self.assertEqual(v["rows"]["Overall"], ("READY", L.GREEN))
        self.assertIn("trycloudflare", v["detail"])
        st = {"updated": self.NOW, "overall": "DEGRADED", "api": {"online": True}, "worker": {"running": True},
              "tunnel": {}, "database": False}
        v = L.compute_view(st, True, False, self.NOW)
        self.assertEqual(v["rows"]["Worker"][0], "STARTING")
        self.assertEqual(v["rows"]["Tunnel"][0], "OFFLINE")
        self.assertEqual(v["rows"]["Overall"][0], "NOT READY")
        self.assertIn("Database", v["detail"])


class PanelActionsTest(TempRoot):
    def actions(self, **kw):
        self.fake = FakeCommon(self.paths, **kw)
        self.opened, self.folders = [], []
        return L.PanelActions(self.paths, common=self.fake, open_url=self.opened.append,
                              open_folder=self.folders.append, sleep=lambda s: None)

    def test_buttons_map_to_manager_functions(self):
        a = self.actions()
        a.run("start")
        a.run("stop")
        a.run("restart")
        self.assertEqual(self.fake.calls, ["start", "stop", "stop", "start"])
        self.assertEqual(a.run("open_site"), C.FRONTEND_URL)
        self.assertEqual(self.opened, [C.FRONTEND_URL])
        a.run("view_logs")
        self.assertEqual(self.folders, [str(self.paths.logs)])
        a.run("uninstall")
        spawn = self.fake.calls[-1]
        self.assertEqual(spawn[0], "spawn")
        self.assertIn("--confirmed", spawn[1])
        with self.assertRaises(ValueError):
            a.run("format_disk")

    def test_status_uses_the_view(self):
        a = self.actions(status={}, alive=False)
        self.assertEqual(a.run("status"), "NOT RUNNING")

    def test_update_only_runs_a_newer_setup(self):
        a = self.actions()
        self.assertIn("No newer", a.run("update"))
        (self.root / "version.json").write_text(json.dumps({"version": "1.2.0"}))
        updates = self.root / "updates"
        updates.mkdir()
        (updates / "SANA-GTM-Setup-1.1.0.exe").write_bytes(b"")
        self.assertIn("No newer", a.run("update"))
        (updates / "SANA-GTM-Setup-1.10.0.exe").write_bytes(b"")
        self.assertEqual(a.run("update"), "running SANA-GTM-Setup-1.10.0.exe")
        self.assertEqual(self.fake.calls[-1][1], [str(updates / "SANA-GTM-Setup-1.10.0.exe")])

    def test_panel_launches_only_through_hidden_spawn(self):
        text = (INSTALLER / "manager" / "panel_logic.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", text)
        self.assertIn("spawn_hidden", text)


# --- uninstall --------------------------------------------------------------------------------------

class UninstallPlan(TempRoot):
    def setUp(self):
        super().setUp()
        for name in ("runtime", "app", "bin", "manager", "versions"):
            (self.root / name).mkdir(exist_ok=True)
        (self.root / "version.json").write_text("{}")
        (self.paths.data / "files" / "keep.txt").write_text("user data")
        C.save_secrets(self.paths, GOOD_SECRETS)
        import uninstall

        self.U = uninstall

    def actions(self, plan):
        return [(s["action"], Path(s["target"]).name) for s in plan]

    def test_full_plan(self):
        plan = self.U.build_plan(self.paths)
        acts = self.actions(plan)
        for a in ("stop_services", "remove_task", "remove_shortcuts", "remove_apps_entry", "unpublish_api_origin"):
            self.assertIn(a, [x for x, _ in acts])
        deleted = {n for a, n in acts if a == "delete"}
        self.assertTrue({"app", "bin", "config", "data", "logs", "state", "versions", "version.json"} <= deleted)
        self.assertEqual(acts[-1], ("delete_later", self.root.name))

    def test_keep_data_keeps_data_and_config(self):
        acts = self.actions(self.U.build_plan(self.paths, keep_data=True))
        deleted = {n for a, n in acts if a == "delete"}
        self.assertNotIn("data", deleted)
        self.assertNotIn("config", deleted)
        self.assertIn(("delete_later", "runtime"), acts)
        self.assertIn(("delete_later", "manager"), acts)
        self.assertNotIn(("delete_later", self.root.name), acts)
        self.assertIn("remove_task", [a for a, _ in acts])

    def test_keep_config_copies_first(self):
        acts = [s["action"] for s in self.U.build_plan(self.paths, keep_config=True)]
        self.assertLess(acts.index("copy_config"), acts.index("delete"))

    def test_dry_run_changes_nothing(self):
        before = sorted(str(p) for p in self.root.rglob("*"))
        with mock.patch.object(self.U, "P", self.paths), mock.patch.object(self.U, "execute") as execute, \
                mock.patch.object(self.U.C, "unregister_task") as unreg, \
                mock.patch.object(self.U.C, "stop_everything") as stop:
            out = io.StringIO()
            with redirect_stdout(out):
                code = self.U.main(["--dry-run", "--json"])
        self.assertEqual(code, 0)
        execute.assert_not_called()
        unreg.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)
        data = json.loads(out.getvalue())
        self.assertTrue(data["dry_run"])
        self.assertTrue(any(s["action"] == "remove_task" for s in data["plan"]))
        self.assertNotIn(SECRET, out.getvalue())

    def test_execute_keep_data(self):
        fake = mock.Mock()
        fake.owned_processes.return_value = []
        plan = [s for s in self.U.build_plan(self.paths, keep_data=True) if s["action"] != "unpublish_api_origin"]
        self.U.execute(self.paths, plan, common=fake)
        fake.stop_everything.assert_called_once()
        fake.unregister_task.assert_called_once()
        fake.remove_shortcuts.assert_called_once()
        fake.unregister_uninstall.assert_called_once()
        self.assertTrue((self.paths.data / "files" / "keep.txt").exists())
        self.assertTrue(self.paths.secrets.exists())
        self.assertFalse((self.root / "app").exists())
        self.assertFalse(self.paths.logs.exists())
        later = [c.args[0] for c in fake.spawn_hidden.call_args_list]
        self.assertTrue(all(a[:3] == ["cmd.exe", "/d", "/c"] for a in later))
        self.assertTrue(any(str(self.root / "runtime") in a[3] for a in later))


# --- verify.py ----------------------------------------------------------------------------------------

class Proc:
    def __init__(self, pid, kind):
        rt = r"C:\SANA GTM\runtime\python.exe"
        cmd = {"api": [rt, "-m", "uvicorn", "cloud.api.main:app"], "worker": [rt, "-m", "cloud.intel.tasks.worker"],
               "supervisor": [rt, "supervisor.py"], "tunnel": ["cloudflared"]}[kind]
        exe = r"C:\SANA GTM\bin\cloudflared.exe" if kind == "tunnel" else rt
        self.pid, self.info = pid, {"exe": exe, "cmdline": cmd}


class VerifyReport(TempRoot):
    def setUp(self):
        super().setUp()
        import verify

        self.V = verify
        self.paths.runtime.mkdir()
        self.paths.python.write_bytes(b"")
        self.paths.pythonw.write_bytes(b"")
        (self.paths.app / "cloud").mkdir(parents=True)
        C.save_secrets(self.paths, GOOD_SECRETS)
        self.status = {"overall": "READY", "worker": {"online": True, "heartbeat_age_s": 3},
                       "tunnel": {"url": "https://x.trycloudflare.com", "published": True}}

    def probes(self, **kw):
        base = dict(http_get=lambda url, timeout=20: (200, "{\"status\":\"ok\"}"), port_open=lambda port: True,
                    processes=lambda paths: [Proc(1, "api"), Proc(2, "worker"), Proc(3, "supervisor"), Proc(4, "tunnel")],
                    task_exists=lambda: True, windows=lambda: [], status=lambda paths: self.status)
        base.update(kw)
        return self.V.Probes(**base)

    def test_all_pass(self):
        report = self.V.run_checks(self.paths, self.probes())
        self.assertTrue(report["ok"], report["failed"])
        cats = {c["category"] for c in report["checks"]}
        self.assertEqual(cats, {"install", "processes", "network", "windows"})
        json.dumps(report)                                            # structured and serialisable
        self.assertIn("RESULT: ALL PASS", self.V.format_report(report))

    def test_missing_runtime_undecryptable_config_closed_port(self):
        self.paths.python.unlink()
        C.set_dpapi_backend(FakeDpapi(b"someone-else"))
        report = self.V.run_checks(self.paths, self.probes(port_open=lambda port: False))
        self.assertFalse(report["ok"])
        for name in ("private Python runtime present", "configuration decryptable (DPAPI, this Windows user)",
                     "API port 8100 listening"):
            self.assertIn(name, report["failed"])
        self.assertNotIn(SECRET, json.dumps(report))

    def test_tunnel_proxy_and_duplicates(self):
        def http(url, timeout=20):
            return (502, "") if "pages.dev/api" in url else (200, "ok")

        report = self.V.run_checks(self.paths, self.probes(
            http_get=http, processes=lambda p: [Proc(1, "api"), Proc(5, "api"), Proc(3, "supervisor")]))
        self.assertIn("sanagtm.pages.dev/api reaches this PC's API", report["failed"])
        self.assertIn("no duplicate api processes", report["failed"])

    def test_visible_console_is_a_failure(self):
        report = self.V.run_checks(self.paths, self.probes(
            windows=lambda: [(1, "ConsoleWindowClass", "python uvicorn")]))
        self.assertIn("no visible window from any SANA GTM process", report["failed"])
        self.assertIn("no CMD / PowerShell / Terminal window running SANA GTM", report["failed"])


class NewSources(unittest.TestCase):
    def test_new_modules_compile_and_avoid_visible_launches(self):
        import py_compile

        for name in ("config_rules.py", "updater.py", "panel_logic.py", "uninstall.py", "verify.py",
                     "control_panel.pyw"):
            f = INSTALLER / "manager" / name
            py_compile.compile(str(f), doraise=True)
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text)
            self.assertNotIn("os.system(", text)

    def test_readme_documents_the_build_blocker(self):
        text = (INSTALLER / "README.md").read_text(encoding="utf-8").lower()
        for phrase in ("webroot", "code sign", "never disable", "build_installer.py"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
