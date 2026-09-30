"""Build SANA-GTM-Setup.exe.

    cloud\\.venv\\Scripts\\python deploy\\windows\\installer\\build_installer.py [--version 1.0.0]

Steps (everything under deploy\\windows\\installer\\build\\, git-ignored):
  1. runtime\\  a private copy of the Python 3.12 the platform venv is based on (no
     site-packages, tests, docs or headers) + every API/worker dependency, installed
     with pip --target (binary wheels only).
  2. app\\      the COMMITTED code (git archive HEAD): cloud\\ without web\\ and tests\\,
     plus the crawler engine packages the platform imports. Git-ignored files
     (.env*, secrets\\, .venv, .localdev) can therefore never enter the package.
  3. bin\\      cloudflared.exe;  manager\\  supervisor / control panel / uninstaller.
  4. payload.zip -> PyInstaller one-file, windowed -> dist\\SANA-GTM-Setup.exe
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
BUILD = HERE / "build"
DIST = HERE / "dist"
STAGE = BUILD / "payload"
VENV_PY = REPO / "cloud" / ".venv" / "Scripts" / "python.exe"
CLOUDFLARED = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
APP_PATHS = ["cloud", "crawler", "config", "utils", "models", "exporters", "adapters"]
APP_DROP = ["cloud/web", "cloud/tests", "cloud/demo"]
RUNTIME_SKIP = {"Doc", "include", "libs", "Scripts", "__install__.json", "NEWS.txt"}
LIB_SKIP = {"site-packages", "test", "idlelib", "ensurepip", "lib2to3", "turtledemo", "__pycache__"}
PIP_EXTRA = ["psutil>=5.9", "python-dotenv>=1.0", "anthropic>=1.0"]


def sh(args, **kw):
    print("  $", " ".join(str(a) for a in args), flush=True)
    subprocess.run([str(a) for a in args], check=True, **kw)


def base_python() -> Path:
    if os.environ.get("SANA_PYTHON_HOME"):
        home = os.environ["SANA_PYTHON_HOME"]
    else:
        cfg = (REPO / "cloud" / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
        home = [l.split("=", 1)[1].strip() for l in cfg.splitlines() if l.startswith("home")][0]
    home = Path(home)
    missing = [n for n in ("python.exe", "pythonw.exe", "python312.dll", "Lib/os.py") if not (home / n).exists()]
    if missing:
        raise SystemExit(f"Python at {home} is incomplete (missing {', '.join(missing)}); an antivirus may have "
                         "quarantined it. Restore it, or set SANA_PYTHON_HOME to a complete Python 3.12 folder.")
    return home


def verify_output(exe: Path, wait_s: int = 120) -> str:
    """The exe must still be there after a while: an antivirus that removes unsigned new
    executables (Webroot did, 2026-09-29) otherwise leaves a build that silently vanished."""
    import hashlib

    if not exe.exists():
        raise SystemExit(f"{exe} was not written")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    size = exe.stat().st_size
    print(f"    waiting {wait_s} s to confirm {exe.name} is not removed by an antivirus...", flush=True)
    for _ in range(wait_s // 5):
        time.sleep(5)
        if not exe.exists():
            raise SystemExit(f"{exe} DISAPPEARED after the build -- an antivirus removed it. Allow-list it "
                             f"(SHA-256 {digest}) or code-sign it, then build again.")
    if exe.stat().st_size != size or hashlib.sha256(exe.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"{exe} changed after the build")
    (exe.parent / (exe.name + ".sha256")).write_text(f"{digest}  {exe.name}\n", encoding="ascii")
    return digest


def build_runtime():
    src = base_python()
    dst = STAGE / "runtime"
    print(f"[1] runtime from {src}")
    for item in src.iterdir():
        if item.name in RUNTIME_SKIP:
            continue
        if item.name == "Lib":
            shutil.copytree(item, dst / "Lib", ignore=lambda d, names: [n for n in names if n in LIB_SKIP])
        elif item.is_dir():
            shutil.copytree(item, dst / item.name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(item, dst / item.name)
    site = dst / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PIP_CACHE_DIR=str(BUILD / "pip-cache"), PIP_DISABLE_PIP_VERSION_CHECK="1")
    sh([VENV_PY, "-m", "pip", "install", "--quiet", "--only-binary=:all:", "--no-warn-script-location",
        "--target", site, "-r", REPO / "cloud" / "worker" / "requirements.txt", *PIP_EXTRA],
       env=env, cwd=REPO / "cloud" / "worker")
    shutil.rmtree(site / "bin", ignore_errors=True)


def build_app():
    print("[2] app from git HEAD")
    commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                            check=True).stdout.strip()
    tar = subprocess.run(["git", "-C", REPO, "archive", "--format=tar", "HEAD", *APP_PATHS], capture_output=True,
                         check=True).stdout
    dst = STAGE / "app"
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(dst, filter="data")
    for d in APP_DROP:
        shutil.rmtree(dst / d, ignore_errors=True)
    for bad in list(dst.rglob(".env*")):
        if bad.name != ".env.example":
            raise SystemExit(f"refusing to package {bad}")
    return commit


def build_rest(version, commit):
    print("[3] cloudflared + manager")
    (STAGE / "bin").mkdir(parents=True)
    shutil.copy2(CLOUDFLARED, STAGE / "bin" / "cloudflared.exe")
    shutil.copytree(HERE / "manager", STAGE / "manager", ignore=shutil.ignore_patterns("__pycache__"))
    make_icon(STAGE / "manager" / "sana-gtm.ico")
    (STAGE / "version.json").write_text(json.dumps({"version": version, "commit": commit,
                                                    "built": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))


def smoke_test():
    print("[4] import smoke test with the packaged runtime")
    code = ("import cloud.api.main, cloud.intel.tasks.worker, psutil, psycopg, redis, uvicorn, tkinter;"
            "import sys; print('imports ok', sys.version.split()[0], sys.prefix)")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "CAREERCLOUD_", "VIRTUAL_ENV"))}
    env["PYTHONPATH"] = str(STAGE / "app")
    sh([STAGE / "runtime" / "python.exe", "-I", "-c", "import sys; sys.path.insert(0, r'%s'); %s" % (STAGE / "app", code)],
       env=env, cwd=BUILD)


def make_icon(path: Path):
    from PIL import Image, ImageDraw, ImageFont  # build venv only

    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(11, 31, 51, 255))
    d.rounded_rectangle((8, 180, 248, 248), radius=56, fill=(31, 136, 229, 255))
    d.rectangle((8, 180, 248, 200), fill=(31, 136, 229, 255))
    try:
        font = ImageFont.truetype("segoeuib.ttf", 150)
    except OSError:
        font = ImageFont.load_default()
    d.text((128, 112), "S", font=font, fill="white", anchor="mm")
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def make_zip() -> Path:
    print("[5] payload.zip")
    out = BUILD / "payload.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(STAGE.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(STAGE).as_posix())
    print(f"    {out.stat().st_size / 2**20:.1f} MB")
    return out


def pyinstaller(payload: Path):
    print("[6] PyInstaller")
    venv = BUILD / "pyi-venv"
    py = venv / "Scripts" / "python.exe"
    if not py.exists():
        sh([base_python() / "python.exe", "-m", "venv", venv])
        sh([py, "-m", "pip", "install", "--quiet", "pyinstaller", "psutil", "pillow"],
           env=dict(os.environ, PIP_CACHE_DIR=str(BUILD / "pip-cache")))
    sh([py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "SANA-GTM-Setup",
        "--icon", STAGE / "manager" / "sana-gtm.ico",
        "--add-data", f"{payload};.", "--add-data", f"{STAGE / 'manager'};manager",
        # manager\ modules the wizard imports at run time (and their stdlib dependencies)
        "--paths", HERE / "manager", "--hidden-import", "sanagtm_common", "--hidden-import", "config_rules",
        "--hidden-import", "updater", "--hidden-import", "urllib.parse",
        "--hidden-import", "psutil", "--distpath", DIST, "--workpath", BUILD / "pyi-work", "--specpath", BUILD,
        HERE / "setup_wizard.py"], env=dict(os.environ, PYTHONNOUSERSITE="1"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.0.0")
    args = ap.parse_args()
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    # Pillow (for the icon) lives in the PyInstaller build venv: create that first.
    venv_py = BUILD / "pyi-venv" / "Scripts" / "python.exe"
    if "PIL" not in sys.modules and not os.environ.get("SANA_BUILD_INNER"):
        if not venv_py.exists():
            sh([base_python() / "python.exe", "-m", "venv", BUILD / "pyi-venv"])
            sh([venv_py, "-m", "pip", "install", "--quiet", "pyinstaller", "psutil", "pillow"],
               env=dict(os.environ, PIP_CACHE_DIR=str(BUILD / "pip-cache")))
        sh([venv_py, __file__, "--version", args.version], env=dict(os.environ, SANA_BUILD_INNER="1"))
        return
    build_runtime()
    commit = build_app()
    build_rest(args.version, commit)
    smoke_test()
    payload = make_zip()
    pyinstaller(payload)
    exe = DIST / "SANA-GTM-Setup.exe"
    digest = verify_output(exe)
    print(f"\nBuilt {exe}  ({exe.stat().st_size / 2**20:.1f} MB)  version {args.version} commit {commit}\n"
          f"SHA-256 {digest}")


if __name__ == "__main__":
    main()
