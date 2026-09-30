# SANA GTM Windows installer

`SANA-GTM-Setup.exe` installs the SANA GTM API, worker and Cloudflare tunnel on one
Windows PC as hidden, per-user background services (no administrator rights).

## Layout

| Path | What |
|---|---|
| `setup_wizard.py` | The setup program (GUI wizard + `--silent`). Frozen by PyInstaller into `SANA-GTM-Setup.exe`. |
| `build_installer.py` | Builds `payload.zip` (private Python 3.12 runtime, app from git HEAD, cloudflared, `manager\`) and the EXE. |
| `manager\sanagtm_common.py` | Paths, DPAPI-encrypted config, hidden process launch, startup task XML, shortcuts, Apps entry. |
| `manager\config_rules.py` | Wizard steps and input validation (Supabase URL/keys, database URL, Upstash URL/token, API port, install folder). Messages never repeat a value. |
| `manager\updater.py` | Staged update: extract to `staging\<v>`, swap, health check, roll back to `versions\<old>` on failure, keep 2 old releases. `config\ data\ logs\ state\` are never touched. |
| `manager\supervisor.py` | Hidden supervisor started by the per-user task: runs API, worker, tunnel; writes `state\status.json`. |
| `manager\panel_logic.py` / `control_panel.pyw` | Control panel: headless logic (status view, Start/Stop/Restart/Open/Logs/Update/Uninstall) + the Tk view. |
| `manager\uninstall.py` | Uninstall with `--dry-run [--json]`, `--keep-data`, `--keep-config`. |
| `manager\verify.py` | Structured health report (`--json`): runtime, config decryptable, task, processes, port, API/worker/tunnel health, pages.dev proxy origin, no visible windows. |
| `manager\validate_config.py` | Read-only connectivity check the wizard runs with the bundled runtime. |

## Security properties

* Secrets live only in `config\secrets.dat`, encrypted with Windows DPAPI for the
  current user (`CryptProtectData`). A tampered or foreign file fails to load; partial
  data is never returned. They reach the services as process environment only.
* The wizard's review page shows secrets as "saved / entered (hidden)"; validation
  errors describe the problem without the value.
* Every process is started with `CREATE_NO_WINDOW` (never `-WindowStyle Hidden`,
  which Windows Terminal ignores). The startup task runs `pythonw.exe`, `LeastPrivilege`.

## Build

```
cloud\.venv\Scripts\python deploy\windows\installer\build_installer.py
```

needs a clean git tree, the official Python 3.12 embeddable runtime and
`cloudflared.exe`; output is `deploy\windows\installer\dist\SANA-GTM-Setup.exe`.

## Build blocker: endpoint security (unresolved)

The last PyInstaller build on the development PC (2026-09-29) was followed by
**Webroot SecureAnywhere** removing the unsigned build artifacts *and* the Python
interpreter, Git's `bash.exe` and `rg` that took part in the build (no Windows
Defender detection was logged). Python had to be restored from the official
python.org archive.

Until this is resolved **do not run `build_installer.py` on that PC**. The fix is one
of:

1. **Code signing** the EXE (and ideally the bundled runtime) with an Authenticode
   certificate, so reputation-based engines stop treating a fresh one-file PyInstaller
   bootloader as unknown; or
2. an administrator **allow-listing** the build folder / hashes in Webroot's console.

Never disable or work around the antivirus to get a build through.

## What is verified (offline, without building)

```
cloud\.venv\Scripts\python -m unittest discover -s deploy\windows\installer\tests -t deploy\windows\installer\tests
node --test deploy\windows\installer\tests\proxy_function.test.mjs
```

* wizard steps and every validation rule, including "no secret in any message";
* DPAPI config round-trip, tamper, other-user and garbage cases through an injectable
  fake DPAPI (plus the real DPAPI on Windows);
* first install, update preserving config/data/logs, rollback on failed or crashing
  health check, rejected packages, pruning, and the wizard rolling back a failed update;
* the startup task XML/definition (generated, never registered by tests);
* `CREATE_NO_WINDOW` on every launch, including a real child process with no window;
* control panel view states and action wiring; update only offers a newer setup;
* uninstall plans (full, keep-data, keep-config), dry-run changes nothing, execution
  with a mocked system layer;
* verify.py report: all-pass, missing runtime, undecryptable config, closed port,
  broken proxy, duplicates, visible console windows.

Not verified: an actual EXE build, a real install/uninstall on a clean Windows
account, and a real update between two built versions (blocked on the item above).
