"""SANA-GTM-Setup.exe -- install or update SANA GTM on this PC (per user, no admin rights).

GUI (default): Welcome -> Configuration (first install, or "change configuration")
-> Install -> "SANA GTM READY".

Unattended:
    SANA-GTM-Setup.exe --silent [--dir PATH] [--import-env FILE] [--no-start]
        --import-env reads CAREERCLOUD_* / GEMINI_API_KEY / TUNNEL_TOKEN from a .env file
        (first install only; an update keeps the saved configuration).
    Exit code 0 = installed and READY (or installed with --no-start); log in
    %TEMP%\\SANA-GTM-Setup.log.

An update (an installation is registered for this user) stops SANA GTM, replaces
runtime\\ app\\ bin\\ manager\\ and keeps config\\ (settings + DPAPI secrets), data\\ and logs\\.
Secrets are never shown again after they are entered, and never written in plain text.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE / "manager"))
import config_rules as R  # noqa: E402
import sanagtm_common as C  # noqa: E402
from updater import Updater  # noqa: E402

PAYLOAD = HERE / "payload.zip"
LOG_FILE = Path(os.environ.get("TEMP", ".")) / "SANA-GTM-Setup.log"
DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs" / "SANA GTM"
from updater import RELEASE_ITEMS as REPLACED  # noqa: E402  (what an update replaces)


def log(msg: str) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def payload_version() -> str:
    with zipfile.ZipFile(PAYLOAD) as z:
        return json.loads(z.read("version.json"))["version"]


def read_env_file(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def config_from_env(values: dict):
    """(settings updates, secrets) from a .env file's values."""
    secrets = {k: values[k] for k in C.SECRET_KEYS if values.get(k)}
    settings = {}
    if values.get("CAREERCLOUD_SUPABASE_URL"):
        settings["supabase_url"] = values["CAREERCLOUD_SUPABASE_URL"].rstrip("/")
    if values.get("CAREERCLOUD_QUEUE_PREFIX"):
        settings["queue_prefix"] = values["CAREERCLOUD_QUEUE_PREFIX"]
    if secrets.get("TUNNEL_TOKEN") and values.get("TUNNEL_HOSTNAME"):
        settings.update(tunnel_mode="named", tunnel_hostname=values["TUNNEL_HOSTNAME"])
    return settings, secrets


def check_values(settings: dict, secrets: dict, install_dir=None) -> list:
    """Problems with the entered values, as messages that never repeat a value."""
    return R.problem_messages(R.check_config(settings, secrets,
                                             install_dir=None if install_dir is None else str(install_dir)))


# --- the installation itself ------------------------------------------------------------------

class Installer:
    def __init__(self, target: Path, settings_update: dict, secrets_update: dict, report, start: bool = True):
        self.paths = C.Paths(target)
        self.settings_update = settings_update
        self.secrets_update = secrets_update     # empty on an update: saved secrets are kept
        self.report = report                      # report(step, state, detail)
        self.start = start
        self.update = (self.paths.config / "secrets.dat").exists()
        self.backup = None                        # previous release, while an update can still roll back
        self.rolled_back = False

    def step(self, name, fn):
        self.report(name, "run", "")
        try:
            detail = fn() or ""
        except Exception as e:
            log(f"FAILED {name}: {traceback.format_exc()}")
            self.report(name, "fail", str(e))
            raise
        log(f"ok {name} {detail}")
        self.report(name, "ok", detail)

    def run(self) -> bool:
        log(f"install to {self.paths.root} (update={self.update}) version {payload_version()}")
        self.step("Checking this PC", self.check_pc)
        self.step("Stopping SANA GTM", self.stop_existing)
        self.step("Installing Python runtime and SANA GTM", self.extract)
        try:
            self.step("Saving configuration (encrypted)", self.save_config)
            self.step("Checking Supabase and Upstash", self.validate)
            self.step("Registering automatic startup", self.register)
            self.step("Creating the SANA GTM shortcut", lambda: C.create_shortcuts(self.paths))
            if self.start:
                self.step("Starting background services", lambda: C.start_supervisor(self.paths))
                self.step("Waiting for API, Worker and Tunnel", self.wait_ready)
        except Exception:
            self.rollback_update()
            raise
        self.finish_update()
        return True

    def rollback_update(self) -> None:
        """An update that does not come up healthy is undone: the previous release
        goes back (config, data and logs were never touched) and is restarted."""
        if not (self.update and self.backup):
            return
        log(f"update failed; rolling back to {self.backup.name}")
        try:
            C.stop_everything(self.paths, wait_s=15)
        except Exception:  # noqa: BLE001 - psutil may be missing in a broken release
            log(traceback.format_exc())
        self.rolled_back = Updater(self.paths.root).rollback(self.backup)
        self.backup = None
        if self.rolled_back and self.start:
            try:
                C.start_supervisor(self.paths)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())
        self.report("Installing Python runtime and SANA GTM", "fail",
                    "update rolled back to the previous version" if self.rolled_back else "rollback failed")

    def finish_update(self) -> None:
        if self.backup is not None:
            pruned = Updater(self.paths.root).prune()
            log(f"update complete; previous release kept in {self.backup} (pruned {pruned})")

    def check_pc(self):
        if sys.getwindowsversion().build < 17763:
            raise RuntimeError("Windows 10 1809 or newer is required")
        self.paths.root.mkdir(parents=True, exist_ok=True)
        need = 600 * 1024 * 1024
        free = shutil.disk_usage(self.paths.root).free
        if free < need:
            raise RuntimeError(f"Not enough disk space on {self.paths.root.drive}: {free // 2**20} MB free, 600 MB needed")
        return f"{free // 2**30} GB free"

    def stop_existing(self):
        if not self.paths.manager.exists():
            return "nothing installed yet"
        # Needs psutil, bundled in this setup program too.
        C.request_stop(self.paths)
        deadline = time.time() + 25
        while time.time() < deadline and C.owned_processes(self.paths):
            if not [p for p in C.owned_processes(self.paths) if C.classify(p) == "supervisor"]:
                break
            time.sleep(1)
        for p in C.owned_processes(self.paths):
            C.kill_tree(p)
        time.sleep(1)
        return "stopped"

    def extract(self):
        """Stage the new release beside the live one, then swap it in. The replaced
        release is kept in versions\\ until the new one is READY (rollback)."""
        self.paths.root.mkdir(parents=True, exist_ok=True)
        updater = Updater(self.paths.root, report=lambda msg: self.report(
            "Installing Python runtime and SANA GTM", "run", msg))
        staged = updater.stage(PAYLOAD)
        count = sum(1 for _ in staged.rglob("*") if _.is_file())
        self.backup = updater.swap(staged)
        self.paths.ensure()
        return f"{count} files" + ("; previous release kept for rollback" if self.backup else "")

    def save_config(self):
        settings = C.load_settings(self.paths)       # existing values win over defaults
        saved_env = dict(settings.get("env", {}))
        settings.update(self.settings_update)
        env = dict(C.DEFAULT_ENV)
        env.update(saved_env)                         # new default keys are added, saved ones kept
        settings["env"] = env
        C.save_settings(self.paths, settings)
        secrets = C.load_secrets(self.paths)          # never overwrite a saved secret with a blank
        for k, v in self.secrets_update.items():
            if v:
                secrets[k] = v
        missing = [k for k in C.REQUIRED_SECRETS if not secrets.get(k)]
        if missing:
            raise RuntimeError("Configuration is incomplete: " + ", ".join(missing))
        C.save_secrets(self.paths, secrets)
        return "settings.json + secrets.dat (DPAPI, this Windows user only)"

    def validate(self):
        payload = json.dumps({"secrets": C.load_secrets(self.paths), "settings": C.load_settings(self.paths)})
        r = C.run_hidden([str(self.paths.python), str(self.paths.manager / "validate_config.py")],
                         timeout=90, input_text=payload, cwd=str(self.paths.root))
        try:
            result = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("The bundled Python runtime did not start: " + r.stderr[-400:])
        bad = [f"{k}: {v}" for k, v in result["errors"].items()]
        if not (result["database"] and result["queue"] and result["supabase"]):
            raise RuntimeError("Could not connect -- " + "; ".join(bad) +
                               ". Check the values (and the internet connection) and try again.")
        return "database, queue and Supabase reachable"

    def register(self):
        C.register_task(self.paths)
        C.register_uninstall(self.paths, payload_version())
        return f"task '{C.TASK_NAME}' (at sign-in, watchdog every 2 min, hidden)"

    def wait_ready(self):
        deadline = time.time() + 240
        last = ""
        while time.time() < deadline:
            st = C.read_status(self.paths)
            if st.get("overall") == "READY":
                return "API, Worker and Tunnel online; " + st["tunnel"]["url"]
            if st.get("overall") == "NOT CONFIGURED":
                raise RuntimeError(st.get("error", "not configured"))
            parts = [f"{n}: {'ONLINE' if st.get(n, {}).get('online') else '...'}" for n in ("api", "worker", "tunnel")]
            now = "  ".join(parts)
            if now != last:
                self.report("Waiting for API, Worker and Tunnel", "run", now)
                last = now
            time.sleep(3)
        raise RuntimeError("SANA GTM did not become READY within 4 minutes; see the logs in " + str(self.paths.logs))


# --- GUI --------------------------------------------------------------------------------------

def run_gui(existing):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    version = payload_version()
    root = tk.Tk()
    root.title("SANA GTM Setup")
    root.geometry("620x560")
    root.resizable(False, False)
    try:
        root.iconbitmap(str(HERE / "manager" / "sana-gtm.ico"))
    except tk.TclError:
        pass
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    header = tk.Frame(root, bg="#0b1f33", height=64)
    header.pack(fill="x")
    tk.Label(header, text="SANA GTM", fg="white", bg="#0b1f33", font=("Segoe UI Semibold", 18)).pack(
        side="left", padx=20, pady=12)
    tk.Label(header, text=f"Setup {version}", fg="#9fb3c8", bg="#0b1f33", font=("Segoe UI", 10)).pack(
        side="left", pady=18)
    body = tk.Frame(root, padx=24, pady=16)
    body.pack(fill="both", expand=True)
    footer = tk.Frame(root, padx=24, pady=12)
    footer.pack(fill="x", side="bottom")
    back_btn = ttk.Button(footer, text="< Back")
    next_btn = ttk.Button(footer, text="Next >")
    cancel_btn = ttk.Button(footer, text="Cancel", command=root.destroy)
    cancel_btn.pack(side="right")
    next_btn.pack(side="right", padx=6)
    back_btn.pack(side="right")

    state = {"dir": tk.StringVar(value=str(existing or DEFAULT_DIR)), "change": tk.BooleanVar(value=False)}
    fields = {}
    saved_settings = C.load_settings(C.Paths(existing)) if existing else dict(C.DEFAULT_SETTINGS)
    saved_secrets_present = set()
    if existing:
        try:
            saved_secrets_present = {k for k, v in C.load_secrets(C.Paths(existing)).items() if v}
        except OSError:
            saved_secrets_present = set()

    def clear():
        for w in body.winfo_children():
            w.destroy()

    def para(text, **kw):
        tk.Label(body, text=text, justify="left", wraplength=560, anchor="w", **kw).pack(fill="x", pady=(0, 8))

    # page 1 ---------------------------------------------------------------------------------
    def page_welcome():
        clear()
        if existing:
            tk.Label(body, text="Update SANA GTM", font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
            para(f"SANA GTM is installed in:\n{existing}\n\nSetup will update it to version {version}. Your "
                 "configuration and secrets are kept; nothing is overwritten.")
            ttk.Checkbutton(body, text="Also change the configuration (Supabase, Upstash, keys)",
                            variable=state["change"]).pack(anchor="w", pady=6)
        else:
            tk.Label(body, text="Install SANA GTM", font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
            para("This installs the SANA GTM API, worker and secure Cloudflare tunnel on this PC as hidden "
                 "background services that start automatically when you sign in to Windows. A private Python "
                 "runtime is included: nothing else needs to be installed, and no administrator rights are needed.")
            para("You will need your SANA GTM configuration: the Supabase project URL and database connection "
                 "string, the Upstash Redis URL and the SANA GTM platform secret.")
            para("After setup you only open https://sanagtm.pages.dev/ in your browser.")
            row = tk.Frame(body)
            row.pack(fill="x", pady=(10, 0))
            tk.Label(row, text="Install folder:").pack(anchor="w")
            ttk.Entry(row, textvariable=state["dir"], width=62).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Browse...", command=lambda: state["dir"].set(
                filedialog.askdirectory(initialdir=state["dir"].get()) or state["dir"].get())).pack(side="left", padx=6)
        back_btn.configure(state="disabled")

        def next_from_welcome():
            if not existing:
                problem = R.check_install_dir(state["dir"].get())
                if problem:
                    messagebox.showerror("SANA GTM Setup", problem)
                    return
            if not existing or state["change"].get():
                page_config()
            else:
                page_install({}, {})

        next_btn.configure(text="Next >", state="normal", command=next_from_welcome)

    # page 2 ---------------------------------------------------------------------------------
    spec = [
        ("supabase_url", "Supabase project URL", False, "https://<project-ref>.supabase.co"),
        ("CAREERCLOUD_DATABASE_URL", "Supabase database connection string", True, "postgresql://..."),
        ("CAREERCLOUD_REDIS_URL", "Upstash Redis URL", True, "rediss://default:...@....upstash.io:6379"),
        ("CAREERCLOUD_PLATFORM_SECRETS_KEY", "SANA GTM platform secret", True, ""),
        ("GEMINI_API_KEY", "Gemini API key (optional)", True, ""),
    ]
    advanced = [
        ("queue_prefix", "Queue prefix", False),
        ("api_port", "Local API port", False),
        ("TUNNEL_TOKEN", "Cloudflare named tunnel token (optional; blank = automatic quick tunnel)", True),
        ("tunnel_hostname", "Named tunnel public hostname (only with a token)", False),
    ]

    def page_config():
        clear()
        tk.Label(body, text="Configuration", font=("Segoe UI Semibold", 14)).pack(anchor="w")
        para("Secrets are encrypted for your Windows account on this PC and are not shown again after "
             "they are entered." + (" Leave a secret blank to keep the saved one." if existing else ""),
             fg="#57606a")
        grid = tk.Frame(body)
        grid.pack(fill="x")

        def add(row, key, label, secret, hint=""):
            tk.Label(grid, text=label, anchor="w").grid(row=row * 2, column=0, sticky="w", pady=(4, 0))
            var = fields.get(key) or tk.StringVar()
            if key not in fields:
                if not secret:
                    var.set(str(saved_settings.get(key, "")))
                fields[key] = var
            e = ttk.Entry(grid, textvariable=var, width=78, show="•" if secret else "")
            e.grid(row=row * 2 + 1, column=0, sticky="we")
            if secret and key in saved_secrets_present and not var.get():
                tk.Label(grid, text="saved", fg="#1a7f37").grid(row=row * 2, column=0, sticky="e")
            elif hint and not var.get():
                tk.Label(grid, text=hint, fg="#8c959f").grid(row=row * 2, column=0, sticky="e")

        for i, (key, label, secret, hint) in enumerate(spec):
            add(i, key, label, secret, hint)
        adv = tk.Frame(body)
        shown = {"v": False}

        def toggle():
            if shown["v"]:
                adv.pack_forget()
            else:
                adv.pack(fill="x")
            shown["v"] = not shown["v"]

        links = tk.Frame(body)
        links.pack(fill="x", pady=(8, 0))
        ttk.Button(links, text="Load from .env file...", command=load_env).pack(side="left")
        ttk.Button(links, text="Advanced...", command=toggle).pack(side="left", padx=6)
        global_grid = grid
        grid = tk.Frame(adv)
        grid.pack(fill="x")
        for i, (key, label, secret) in enumerate(advanced):
            add(i, key, label, secret)
        grid = global_grid
        back_btn.configure(state="normal", command=page_welcome)
        next_btn.configure(text="Next >", command=submit)

    def load_env():
        f = filedialog.askopenfilename(title="Select a SANA GTM .env file",
                                       filetypes=[("Environment files", "*.env* .env*"), ("All files", "*.*")])
        if not f:
            return
        s, sec = config_from_env(read_env_file(Path(f)))
        for k, v in list(s.items()) + list(sec.items()):
            if k in fields:
                fields[k].set(v)
        messagebox.showinfo("SANA GTM Setup", f"Loaded {len(s) + len(sec)} values from the file.")

    def collect():
        settings = {k: fields[k].get().strip() for k in ("supabase_url", "queue_prefix", "tunnel_hostname") if k in fields}
        settings["supabase_url"] = settings.get("supabase_url", "").rstrip("/")
        try:
            settings["api_port"] = int(fields["api_port"].get()) if "api_port" in fields else 8100
        except ValueError:
            settings["api_port"] = 8100
        secrets = {k: fields[k].get().strip() for k in C.SECRET_KEYS if k in fields and fields[k].get().strip()}
        settings["tunnel_mode"] = "named" if (secrets.get("TUNNEL_TOKEN") or "TUNNEL_TOKEN" in saved_secrets_present) \
            and settings.get("tunnel_hostname") else "quick"
        return settings, secrets

    def submit():
        settings, secrets = collect()
        # A secret left blank on an update keeps the saved one: check the format of new values only.
        placeholders = {"CAREERCLOUD_DATABASE_URL": "postgresql://saved", "CAREERCLOUD_REDIS_URL": "rediss://saved",
                        "CAREERCLOUD_PLATFORM_SECRETS_KEY": "saved" * 4, "TUNNEL_TOKEN": "saved"}
        effective = {k: placeholders.get(k, "saved") for k in saved_secrets_present}
        effective.update(secrets)
        problems = check_values(settings, effective, None if existing else state["dir"].get())
        if problems:
            messagebox.showerror("SANA GTM Setup", "\n".join(problems))
            return
        page_review(settings, secrets)

    # review ---------------------------------------------------------------------------------
    def page_review(settings, secrets):
        clear()
        tk.Label(body, text="Review", font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 8))
        rows = [("Install folder", state["dir"].get()),
                ("Supabase project URL", settings.get("supabase_url", "")),
                ("Queue prefix", settings.get("queue_prefix", "")),
                ("Local API port", str(settings.get("api_port", 8100))),
                ("Tunnel", "named: " + settings.get("tunnel_hostname", "") if settings.get("tunnel_mode") == "named"
                 else "automatic quick tunnel")]
        for key, label, *_ in spec + advanced:
            if key in C.SECRET_KEYS:
                rows.append((label.split(" (")[0], R.mask(secrets.get(key) or (key in saved_secrets_present))))
        for label, value in rows:
            para(f"{label}:  {value}")
        back_btn.configure(state="normal", command=page_config)
        next_btn.configure(text="Install", command=lambda: page_install(settings, secrets))

    # page 3 ---------------------------------------------------------------------------------
    def page_install(settings, secrets):
        target = Path(state["dir"].get())
        clear()
        tk.Label(body, text="Installing SANA GTM", font=("Segoe UI Semibold", 14)).pack(anchor="w", pady=(0, 10))
        rows = {}
        steps = ["Checking this PC", "Stopping SANA GTM", "Installing Python runtime and SANA GTM",
                 "Saving configuration (encrypted)", "Checking Supabase and Upstash", "Registering automatic startup",
                 "Creating the SANA GTM shortcut", "Starting background services", "Waiting for API, Worker and Tunnel"]
        for s in steps:
            f = tk.Frame(body)
            f.pack(fill="x", pady=2)
            mark = tk.Label(f, text="○", width=2, fg="#8c959f", font=("Segoe UI", 11))
            mark.pack(side="left")
            tk.Label(f, text=s, anchor="w").pack(side="left")
            detail = tk.Label(f, text="", fg="#57606a", anchor="e", font=("Segoe UI", 8))
            detail.pack(side="right")
            rows[s] = (mark, detail)
        back_btn.configure(state="disabled")
        next_btn.configure(state="disabled")
        cancel_btn.configure(state="disabled")
        q = queue.Queue()

        def report(step, st, detail):
            q.put(("step", step, st, detail))

        def work():
            try:
                Installer(target, settings, secrets, report).run()
                q.put(("done", True, ""))
            except Exception as e:  # noqa: BLE001
                q.put(("done", False, str(e)))

        def pump():
            try:
                while True:
                    msg = q.get_nowait()
                    if msg[0] == "step":
                        _, step, st, detail = msg
                        mark, dl = rows.get(step, (None, None))
                        if mark:
                            mark.configure(text={"run": "◔", "ok": "✔", "fail": "✖"}[st],
                                           fg={"run": "#9a6700", "ok": "#1a7f37", "fail": "#cf222e"}[st])
                            dl.configure(text=detail[:70] if st != "fail" else "")
                    else:
                        finish(target, msg[1], msg[2])
                        return
            except queue.Empty:
                pass
            root.after(150, pump)

        threading.Thread(target=work, daemon=True).start()
        pump()

    def finish(target, ok, error):
        paths = C.Paths(target)
        cancel_btn.configure(state="normal", text="Close")
        if ok:
            clear()
            tk.Label(body, text="SANA GTM READY", fg="#1a7f37", font=("Segoe UI Semibold", 26)).pack(pady=(40, 10))
            para("API, Worker and Tunnel are running in the background and will start automatically every time "
                 "you sign in to Windows. No windows need to stay open.", fg="#57606a")
            para("Open SANA GTM in your browser:  https://sanagtm.pages.dev/")
            para("To check or restart the services, use the \"SANA GTM\" shortcut on the desktop / Start menu.",
                 fg="#57606a")
            next_btn.configure(state="normal", text="Open SANA GTM",
                               command=lambda: (__import__("webbrowser").open(C.FRONTEND_URL), root.destroy()))
            back_btn.configure(state="normal", text="Control panel",
                               command=lambda: (C.spawn_hidden([str(paths.pythonw), str(paths.manager / "control_panel.pyw")],
                                                               cwd=str(paths.root)), root.destroy()))
        else:
            para("Setup could not finish:\n\n" + error, fg="#cf222e")
            para(f"Details: {LOG_FILE}", fg="#57606a")
            back_btn.configure(state="normal", text="< Back", command=page_config)

    page_welcome()
    root.mainloop()
    return 0


# --- entry point ------------------------------------------------------------------------------

def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--dir")
    ap.add_argument("--import-env")
    ap.add_argument("--no-start", action="store_true")
    args = ap.parse_args(argv)
    existing = C.registered_install_dir()
    if existing and not (existing / "manager").exists():
        existing = None
    log(f"SANA GTM Setup {payload_version()} started (existing: {existing})")
    if not args.silent:
        return run_gui(existing)

    target = Path(args.dir) if args.dir else (existing or DEFAULT_DIR)
    settings, secrets = {}, {}
    if args.import_env and not (target / "config" / "secrets.dat").exists():
        settings, secrets = config_from_env(read_env_file(Path(args.import_env)))
        problems = check_values({**C.DEFAULT_SETTINGS, **settings}, secrets)
        if problems:
            log("configuration problems: " + "; ".join(problems))
            return 2

    def report(step, st, detail):
        if st != "run":
            log(f"  [{st}] {step} {detail}")

    try:
        Installer(target, settings, secrets, report, start=not args.no_start).run()
    except Exception as e:  # noqa: BLE001
        log(f"SETUP FAILED: {e}")
        return 1
    log("SANA GTM READY" if not args.no_start else "installed (not started)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
