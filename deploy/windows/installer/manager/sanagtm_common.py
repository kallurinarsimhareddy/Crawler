r"""Shared code for the installed SANA GTM manager (supervisor, control panel, uninstaller)
and for SANA-GTM-Setup.exe.

Installed layout (per user, no administrator rights needed):

    <install>\runtime\     private Python 3.12 with every dependency
    <install>\app\         SANA GTM API + worker code (replaced by updates)
    <install>\bin\         cloudflared.exe
    <install>\manager\     this package (replaced by updates)
    <install>\config\      settings.json (public values) + secrets.dat (DPAPI) -- NEVER replaced by updates
    <install>\data\        platform files / results
    <install>\logs\        api / worker / tunnel / supervisor logs
    <install>\state\       status.json, stop flag, tunnel URL

Secrets are encrypted with Windows DPAPI for the current user (CryptProtectData):
only this Windows account on this PC can decrypt secrets.dat. They are handed to
the API / worker / tunnel as environment variables of those processes only and
are never written to a log, a command line or a plain-text file.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Dict, Optional

PRODUCT = "SANA GTM"
TASK_NAME = "SANA GTM"
REG_APP_KEY = r"Software\SANA GTM"
REG_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SANAGTM"
FRONTEND_URL = "https://sanagtm.pages.dev/"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

# Secret keys stored in secrets.dat. Everything else lives in settings.json.
SECRET_KEYS = (
    "CAREERCLOUD_DATABASE_URL",
    "CAREERCLOUD_REDIS_URL",
    "CAREERCLOUD_PLATFORM_SECRETS_KEY",
    "GEMINI_API_KEY",
    "TUNNEL_TOKEN",
)
REQUIRED_SECRETS = ("CAREERCLOUD_DATABASE_URL", "CAREERCLOUD_REDIS_URL", "CAREERCLOUD_PLATFORM_SECRETS_KEY")

# Public (non-secret) API/worker configuration. The installer writes these into
# settings.json once; later updates add new keys but never change existing ones.
DEFAULT_ENV = {
    "CAREERCLOUD_ENV": "development",
    "CAREERCLOUD_ALLOW_REMOTE_SERVICES": "1",
    "CAREERCLOUD_STORAGE": "postgres",
    "CAREERCLOUD_DB_USER_ROLE": "authenticated",
    "CAREERCLOUD_QUEUE": "redis",
    "CAREERCLOUD_LOG_LEVEL": "INFO",
    "CAREERCLOUD_DB_POOL_MAX": "4",
    "CAREERCLOUD_AUTH_MODE": "supabase",
    "CAREERCLOUD_RUNNER": "fake",
    "CAREERCLOUD_CORS_ORIGINS": "https://sanagtm.pages.dev",
    "CAREERCLOUD_TRUST_PROXY": "cloudflare",
    # Browser traffic now reaches the API through the sanagtm.pages.dev /api proxy,
    # so every user shares the proxy's address: the per-IP limit is set higher.
    "CAREERCLOUD_RATE_LIMIT_PER_MINUTE": "600",
    "CAREERCLOUD_JOB_CREATE_PER_HOUR": "20",
    "CAREERCLOUD_PLATFORM_CONCURRENCY": "2",
}

DEFAULT_SETTINGS = {
    "api_port": 8100,
    "supabase_url": "",
    "queue_prefix": "sanagtm:staging",
    "frontend_url": FRONTEND_URL,
    "tunnel_mode": "quick",        # quick = trycloudflare.com URL; named = TUNNEL_TOKEN + tunnel_hostname
    "tunnel_hostname": "",
    "env": dict(DEFAULT_ENV),
}


# --- paths ------------------------------------------------------------------------------------

class Paths:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.runtime = self.root / "runtime"
        self.python = self.runtime / "python.exe"
        self.pythonw = self.runtime / "pythonw.exe"
        self.app = self.root / "app"
        self.bin = self.root / "bin"
        self.cloudflared = self.bin / "cloudflared.exe"
        self.manager = self.root / "manager"
        self.config = self.root / "config"
        self.settings = self.config / "settings.json"
        self.secrets = self.config / "secrets.dat"
        self.data = self.root / "data"
        self.logs = self.root / "logs"
        self.state = self.root / "state"
        self.status = self.state / "status.json"
        self.stop_flag = self.state / "stop.flag"
        self.tunnel_url = self.state / "tunnel-url.txt"
        self.version = self.root / "version.json"
        self.icon = self.manager / "sana-gtm.ico"

    def ensure(self) -> None:
        for d in (self.config, self.data / "files", self.data / "results", self.logs, self.state):
            d.mkdir(parents=True, exist_ok=True)


def installed_paths() -> Paths:
    """Paths of the installation this file belongs to (manager\\sanagtm_common.py)."""
    return Paths(Path(__file__).resolve().parent.parent)


def instance_id(root: Path) -> str:
    return hashlib.sha1(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:12]


# --- DPAPI ------------------------------------------------------------------------------------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_ENTROPY = b"SANA GTM local configuration v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    b = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    b._buf = buf  # keep alive
    return b


def dpapi_protect(data: bytes) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, ent, out = _blob(data), _blob(_ENTROPY), _Blob()
    if not crypt32.CryptProtectData(ctypes.byref(src), "SANA GTM", ctypes.byref(ent), None, None,
                                    _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptProtectData failed: %d" % kernel32.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, ent, out = _blob(data), _blob(_ENTROPY), _Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                      _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed: %d (secrets.dat belongs to another Windows user or PC)"
                      % kernel32.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save_secrets(paths: Paths, secrets: Dict[str, str]) -> None:
    clean = {k: v for k, v in secrets.items() if k in SECRET_KEYS and v}
    blob = dpapi_protect(json.dumps(clean).encode("utf-8"))
    paths.config.mkdir(parents=True, exist_ok=True)
    tmp = paths.secrets.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, paths.secrets)


def load_secrets(paths: Paths) -> Dict[str, str]:
    if not paths.secrets.exists():
        return {}
    return json.loads(dpapi_unprotect(paths.secrets.read_bytes()).decode("utf-8"))


# --- settings ---------------------------------------------------------------------------------

def load_settings(paths: Paths) -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if paths.settings.exists():
        saved = json.loads(paths.settings.read_text(encoding="utf-8"))
        env = dict(settings["env"])
        env.update(saved.get("env", {}))
        settings.update(saved)
        settings["env"] = env
    return settings


def save_settings(paths: Paths, settings: dict) -> None:
    paths.config.mkdir(parents=True, exist_ok=True)
    tmp = paths.settings.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(tmp, paths.settings)


def origin_key(settings: dict) -> str:
    """Upstash key the sanagtm.pages.dev /api proxy reads to find this PC's API."""
    return f"{settings.get('queue_prefix') or 'sanagtm:staging'}:frontend:api_origin"


def service_env(paths: Paths, settings: dict, secrets: Dict[str, str]) -> Dict[str, str]:
    """Environment for the API and worker processes."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CAREERCLOUD_", "PYTHON"))}
    env.pop("GEMINI_API_KEY", None)
    env.pop("TUNNEL_TOKEN", None)
    env.update(settings.get("env", {}))
    env["CAREERCLOUD_SUPABASE_URL"] = settings.get("supabase_url", "")
    env["CAREERCLOUD_QUEUE_PREFIX"] = settings.get("queue_prefix", "sanagtm:staging")
    env["CAREERCLOUD_PLATFORM_FILES_DIR"] = str(paths.data / "files")
    env["CAREERCLOUD_RESULTS_DIR"] = str(paths.data / "results")
    for key in ("CAREERCLOUD_DATABASE_URL", "CAREERCLOUD_REDIS_URL", "CAREERCLOUD_PLATFORM_SECRETS_KEY",
                "GEMINI_API_KEY"):
        if secrets.get(key):
            env[key] = secrets[key]
    env["PYTHONPATH"] = str(paths.app)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONNOUSERSITE"] = "1"
    return env


# --- processes --------------------------------------------------------------------------------

def run_hidden(args, timeout: Optional[float] = 60, input_text: Optional[str] = None, env=None, cwd=None):
    """Run a console program to completion without any window."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW, startupinfo=si, env=env, cwd=cwd,
                          encoding="utf-8", errors="replace")


def spawn_hidden(args, *, cwd=None, env=None, stdout=None, stderr=None) -> subprocess.Popen:
    """Start a long-running console program with NO console window (CREATE_NO_WINDOW,
    not SW_HIDE: Windows 11 hands a hidden console to Windows Terminal, which shows it)."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    out = open(stdout, "ab") if stdout else subprocess.DEVNULL
    err = open(stderr, "ab") if stderr else subprocess.DEVNULL
    try:
        return subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, startupinfo=si,
                                close_fds=True)
    finally:
        for f in (out, err):
            if f is not subprocess.DEVNULL:
                f.close()


def boot_id() -> str:
    """Identifies the current Windows boot (boot time rounded to a minute)."""
    uptime = ctypes.windll.kernel32.GetTickCount64() / 1000.0
    return str(int((time.time() - uptime) // 60))


def stop_requested(paths: Paths) -> bool:
    """A Stop from the control panel holds until Start or the next Windows start."""
    try:
        return abs(int(paths.stop_flag.read_text(encoding="ascii").strip()) - int(boot_id())) <= 1
    except (OSError, ValueError):
        return False


def request_stop(paths: Paths) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    paths.stop_flag.write_text(boot_id(), encoding="ascii")


def clear_stop(paths: Paths) -> None:
    try:
        paths.stop_flag.unlink()
    except FileNotFoundError:
        pass


def owned_processes(paths: Paths):
    """Every process started from this installation (runtime python / cloudflared)."""
    import psutil

    root = str(paths.root.resolve()).lower()
    found = []
    for p in psutil.process_iter(["pid", "exe", "cmdline", "create_time", "name"]):
        exe = (p.info.get("exe") or "").lower()
        if exe.startswith(root + os.sep):
            found.append(p)
    return found


def classify(proc) -> str:
    cmd = " ".join(proc.info.get("cmdline") or []).lower()
    exe = (proc.info.get("exe") or "").lower()
    if exe.endswith("cloudflared.exe"):
        return "tunnel"
    if "uvicorn" in cmd and "cloud.api.main:app" in cmd:
        return "api"
    if "cloud.intel.tasks.worker" in cmd:
        return "worker"
    if "supervisor.py" in cmd:
        return "supervisor"
    if "control_panel.pyw" in cmd:
        return "panel"
    if "uninstall.py" in cmd:
        return "uninstall"
    return "other"


def kill_tree(proc) -> None:
    import psutil

    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    for p in children + [proc]:
        try:
            p.kill()
        except psutil.Error:
            pass


def own_heartbeat_age(beats, hostname: str, pids, now: float) -> Optional[float]:
    """Seconds since the newest heartbeat of one of OUR worker processes, or None.

    ``beats`` is the <prefix>:platform:workers ZSET as (member, score) pairs. Members are
    "<hostname>-<pid>-<6 hex>" (cloud.intel.tasks.worker.default_worker_id), so other
    workers on the same queue -- another PC, or a dev worker on this PC -- never count.
    """
    import re

    wanted = {int(p) for p in pids}
    pattern = re.compile(r"^%s-(\d+)-[0-9a-f]{6}$" % re.escape(hostname.lower()))
    newest = None
    for member, score in beats:
        m = pattern.match(str(member).lower())
        if m and int(m.group(1)) in wanted:
            newest = score if newest is None else max(newest, score)
    return None if newest is None else round(now - float(newest), 1)


def read_status(paths: Paths) -> dict:
    try:
        return json.loads(paths.status.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# --- scheduled task (per user; no administrator rights) ----------------------------------------

def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def register_task(paths: Paths) -> None:
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + 60))
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts and supervises the SANA GTM API, worker and Cloudflare tunnel ({_xml_escape(str(paths.root))}). Hidden; no console windows.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_xml_escape(user)}</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{start}</StartBoundary>
      <Repetition>
        <Interval>PT2M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml_escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(str(paths.pythonw))}</Command>
      <Arguments>"{_xml_escape(str(paths.manager / 'supervisor.py'))}"</Arguments>
      <WorkingDirectory>{_xml_escape(str(paths.root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    xml_file = paths.state / "task.xml"
    paths.state.mkdir(parents=True, exist_ok=True)
    xml_file.write_text(xml, encoding="utf-16")
    r = run_hidden(["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", str(xml_file), "/F"])
    xml_file.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError("Could not register the startup task: " + (r.stderr or r.stdout).strip())


def unregister_task() -> None:
    run_hidden(["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"])


def task_exists() -> bool:
    return run_hidden(["schtasks.exe", "/Query", "/TN", TASK_NAME]).returncode == 0


def run_task() -> bool:
    return run_hidden(["schtasks.exe", "/Run", "/TN", TASK_NAME]).returncode == 0


def start_supervisor(paths: Paths) -> None:
    """Start the background supervisor (through the scheduled task when it exists)."""
    clear_stop(paths)
    if not (task_exists() and run_task()):
        spawn_hidden([str(paths.pythonw), str(paths.manager / "supervisor.py")], cwd=str(paths.root))


def stop_everything(paths: Paths, wait_s: float = 25.0) -> None:
    """Stop the supervisor (it stops its services), then anything still left."""
    request_stop(paths)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not [p for p in owned_processes(paths) if classify(p) in ("supervisor", "api", "worker", "tunnel")]:
            break
        time.sleep(1)
    for p in owned_processes(paths):
        if classify(p) in ("supervisor", "api", "worker", "tunnel"):
            kill_tree(p)


def python_exe_is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# --- shortcuts / registry -----------------------------------------------------------------------

def _shell_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


def shortcut_paths():
    return [_shell_folder(0x02) / "SANA GTM.lnk",   # CSIDL_PROGRAMS (Start menu)
            _shell_folder(0x10) / "SANA GTM.lnk"]   # CSIDL_DESKTOPDIRECTORY


def create_shortcuts(paths: Paths) -> None:
    script = (
        "$s = New-Object -ComObject WScript.Shell;"
        "foreach ($p in $env:SANA_LNKS.Split('|')) {"
        " $l = $s.CreateShortcut($p); $l.TargetPath = $env:SANA_TARGET; $l.Arguments = $env:SANA_ARGS;"
        " $l.WorkingDirectory = $env:SANA_WD; $l.IconLocation = $env:SANA_ICON;"
        " $l.Description = 'SANA GTM control panel'; $l.Save() }"
    )
    env = dict(os.environ)
    env.update({
        "SANA_LNKS": "|".join(str(p) for p in shortcut_paths()),
        "SANA_TARGET": str(paths.pythonw),
        "SANA_ARGS": f'"{paths.manager / "control_panel.pyw"}"',
        "SANA_WD": str(paths.root),
        "SANA_ICON": f"{paths.icon},0",
    })
    r = run_hidden(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-Command", script], env=env)
    if r.returncode != 0:
        raise RuntimeError("Could not create the SANA GTM shortcut: " + r.stderr.strip())


def remove_shortcuts() -> None:
    for p in shortcut_paths():
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def dir_size_kb(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total // 1024


def register_uninstall(paths: Paths, version: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_APP_KEY) as k:
        winreg.SetValueEx(k, "InstallDir", 0, winreg.REG_SZ, str(paths.root))
        winreg.SetValueEx(k, "Version", 0, winreg.REG_SZ, version)
    uninstall = f'"{paths.pythonw}" "{paths.manager / "uninstall.py"}"'
    values = {
        "DisplayName": PRODUCT,
        "DisplayVersion": version,
        "Publisher": PRODUCT,
        "InstallLocation": str(paths.root),
        "DisplayIcon": str(paths.icon),
        "UninstallString": uninstall,
        "QuietUninstallString": uninstall + " --quiet",
        "URLInfoAbout": FRONTEND_URL,
    }
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_UNINSTALL_KEY) as k:
        for name, value in values.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, dir_size_kb(paths.root))


def unregister_uninstall() -> None:
    import winreg

    for key in (REG_UNINSTALL_KEY, REG_APP_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


def registered_install_dir() -> Optional[Path]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_APP_KEY) as k:
            value, _ = winreg.QueryValueEx(k, "InstallDir")
            return Path(value)
    except OSError:
        return None
