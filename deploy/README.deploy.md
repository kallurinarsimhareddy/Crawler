# Running CareerCrawler on a Linux server

The point of this is that your PC can be off. The crawl runs weekly on a server
you never log into, resumes itself after a reboot, and can be triggered
remotely over SSH.

Nothing in this directory changes what the crawler does. It is the deployment
layer only: three systemd units, a guard script, and an installer.

---

## What you get

| | |
|---|---|
| **Weekly** | `careercrawler.timer` — Saturday 06:00, and **catches up** if the server was off |
| **Manual** | `systemctl start careercrawler.service` over SSH |
| **After a reboot** | `careercrawler-resume.service` picks an interrupted run back up, and *only* then |
| **Never twice** | The run lock refuses a second crawler against the same database, whoever started it |
| **No login needed** | systemd runs it as a system user; nobody has to be signed in |

---

## 1. Server requirements

- Linux with **systemd 243 or newer** (`systemctl --version`). 243 is where
  `ExecCondition=` arrives, which the resume unit relies on. Debian 11+,
  Ubuntu 20.04+, RHEL 9+ are all fine.
- **Python 3.12+**.
- **~2 GB RAM free** at 6 workers. Chromium is the cost: one headless browser
  per worker thread, and a live run was measured at ~32 Chromium processes and
  roughly 1.5 GB. Do not raise the worker count on a small box.
- **~2 GB disk**, growing. `state/crawler.db` was 343 MB after one full pass and
  the DEBUG logs rotate at 50 MB × 8.
- **Local disk for `state/`.** SQLite's locking is not safe over NFS or SMB, and
  the run lock lives beside the database. A network mount will corrupt both.
- Outbound HTTPS. No inbound ports.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
# Chromium's shared libraries, for the browser fallback:
sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2
```

---

## 2. User, code, virtualenv

```bash
# A system user with no shell and no home of its own.
sudo useradd --system --home /opt/careercrawler --shell /usr/sbin/nologin careercrawler

sudo mkdir -p /opt/careercrawler
sudo chown careercrawler:careercrawler /opt/careercrawler

# Deploy the code (git clone, rsync, whatever you use).
sudo -u careercrawler git clone <your-remote> /opt/careercrawler
cd /opt/careercrawler

sudo -u careercrawler python3 -m venv venv
sudo -u careercrawler ./venv/bin/pip install --upgrade pip
sudo -u careercrawler ./venv/bin/pip install -r requirements.txt
```

### Playwright browser

The browser fallback reads boards that build their job list client-side. Without
it those companies simply fail; everything else still works.

```bash
sudo -u careercrawler ./venv/bin/python -m playwright install chromium
# If the library check above missed something:
sudo ./venv/bin/python -m playwright install-deps chromium
```

Chromium is already launched with `--no-sandbox --disable-dev-shm-usage`, so it
works in a container without extra flags.

---

## 3. Credentials and configuration

Two things: the spreadsheet id, and a Google service-account key.

```bash
sudo mkdir -p /etc/careercrawler

# The key file, copied from the Windows machine's secrets/service_account.json.
sudo cp service_account.json /etc/careercrawler/service_account.json
sudo chown careercrawler:careercrawler /etc/careercrawler/service_account.json
sudo chmod 600 /etc/careercrawler/service_account.json
```

The spreadsheet must be shared as an **Editor** with the service account's
address (`...iam.gserviceaccount.com`) — the same sharing the Windows install
already has, so no change is needed if you are moving the same sheet.

Configuration lives in `/etc/careercrawler/careercrawler.env`, created by the
installer from `careercrawler.env.example`:

| Variable | Meaning |
|---|---|
| `CAREERCRAWLER_SPREADSHEET_ID` | The spreadsheet |
| `CAREERCRAWLER_GOOGLE_CREDENTIALS` | Path to the key file |
| `CAREERCRAWLER_WORKERS` | Companies crawled at once (**leave at 6**) |
| `CAREERCRAWLER_PER_HOST_DELAY` | Seconds between requests to one host |
| `CAREERCRAWLER_LOG` | Full DEBUG log path |
| `CAREERCRAWLER_DATABASE` | The durable store; the run lock is derived from it |

Every unit reads this file with `EnvironmentFile=-`, so it is optional and the
defaults compiled into the units apply if it is missing.

---

## 4. Install the units

```bash
sudo CC_HOME=/opt/careercrawler CC_USER=careercrawler /opt/careercrawler/deploy/install.sh
```

`CC_HOME` and `CC_USER` are yours to choose; the units ship with `@CC_HOME@` and
`@CC_USER@` placeholders and the installer substitutes them. That is necessary
rather than decorative: systemd expands environment variables in an `ExecStart`'s
*arguments*, but **not** in `WorkingDirectory=`, `ExecCondition=`, or the
executable path — those must be literal.

The installer enables the timer and the resume unit, creates `state/` and
`output/` owned by the service user, and **starts nothing**.

```bash
sudo systemctl start careercrawler.timer
systemctl list-timers careercrawler.timer
```

---

## 5. Running it

```bash
# Manual run, right now
sudo systemctl start careercrawler.service

# Is it running?
systemctl status careercrawler.service
systemctl is-active careercrawler.service

# Stop it gracefully -- finishes the batch in flight, persists, checkpoints
sudo systemctl stop careercrawler.service

# The schedule
systemctl list-timers careercrawler.timer
systemctl status careercrawler.timer
```

`systemctl start` returns as soon as the unit is started; the crawl runs for
hours in the background. It does not need your SSH session to stay open.

### Logs

Two places, and they answer different questions.

```bash
# What the run is doing now
journalctl -u careercrawler.service -f

# This run, from the beginning
journalctl -u careercrawler.service -b

# The last week, errors only
journalctl -u careercrawler.service --since '1 week ago' -p err

# The boot-resume unit, including why it decided to skip
journalctl -u careercrawler-resume.service -b
```

The full DEBUG log — every company, every adapter decision — is the file at
`CAREERCRAWLER_LOG`, `/opt/careercrawler/output/weekly.log` by default. The
crawler rotates it itself at 50 MB, keeping eight. journald has the run-level
story; the file has everything.

```bash
sudo -u careercrawler tail -f /opt/careercrawler/output/weekly.log
sudo -u careercrawler grep -a 'Progress: ' /opt/careercrawler/output/weekly.log | tail
```

### Exit codes

| Code | Meaning | Treated as success? |
|---|---|---|
| `0` | Finished | yes |
| `75` | Another run holds the database; this one declined to start | **yes** |
| `130` | Stopped on a signal, after checkpointing | **yes** |
| `1` | Failed | no |
| `2` | Not configured — no spreadsheet id or no credentials | no |

`SuccessExitStatus=0 75 130` is why a skipped or interrupted run does not show
as failed and does not page anybody.

---

## 6. What happens on a reboot

`careercrawler-resume.service` runs at boot, and **usually decides to do
nothing** — which is the point.

Resume is the crawler's *default*, not a mode: started with no checkpoint it
crawls all 12,377 companies from the beginning. So the unit is guarded by
`ExecCondition=deploy/careercrawler-should-resume.sh`, which resumes only when
all three of these hold:

1. `state/checkpoint.json` **exists** — a completed run archives its own, so a
   checkpoint means a run was interrupted.
2. It is **durable** — written after its batch was persisted. The crawler
   refuses to resume one that is not, and there is no point booting a
   twenty-hour crawl to find that out.
3. It belongs to **this ISO week** — checkpoints are week-scoped, and a stale one
   would start a fresh full crawl, which is exactly what a reboot must not do.

Otherwise the unit is skipped cleanly and journald records the reason:

```bash
journalctl -u careercrawler-resume.service -b
# "No checkpoint at ...: nothing was interrupted, not resuming."
# "Resuming run 2026-W37-...Z at 6400/12377."
```

Missed *scheduled* runs are a separate mechanism: `Persistent=true` on the timer
means a Saturday that passed while the server was off fires at the next
opportunity, rather than being lost for a week.

---

## 7. Why two crawlers cannot run at once

The timer, the resume unit and your `systemctl start` can all fire close
together. That is safe, and the safety does not come from systemd.

`utils.runlock` takes an advisory file lock on `state/crawler.lock` — derived
from the database path, so a different `--database` takes a different lock and
nothing is ever system-wide. A second crawler finds it held, prints who has it
(host, pid, run id, start time) and exits **75**.

This is deliberately *not* systemd's `MultipleInstances` or a `RefuseManualStart`:
systemd can only see units it started. On 2026-09-05 a shell-started crawl and
the Windows scheduled task ran together against one database, closed 125
postings on an unluckier re-read, and overwrote the run's record. The scheduler
could not have prevented it; a lock inside the process can and does.

The lock is held by the kernel, so a crawler killed with `SIGKILL`, OOM-killed,
or lost to a power cut leaves **nothing stale** — the next run acquires
immediately. There is no PID file to clean up and no heartbeat to wait out.

```bash
# Who holds it, if anyone
sudo -u careercrawler cat /opt/careercrawler/state/crawler.lock | head -c 512; echo
```

`--no-lock` exists and you should not use it. It is for a lock left by a process
you have *confirmed* is gone — and the kernel already releases that one, so the
situation it addresses essentially does not arise.

---

## 8. Backups

Back up **`state/`**. It is the durable ledger — every posting the crawler has
ever seen, with its first-seen date and status. The spreadsheet is a report
derived from it; `state/` is the thing that cannot be regenerated.

```bash
# Consistent copy of a live SQLite database. Do NOT just cp the .db --
# a copy taken mid-write without the -wal is not a database.
sudo -u careercrawler /opt/careercrawler/venv/bin/python - <<'PY'
import sqlite3, pathlib, datetime
out = pathlib.Path("/var/backups/careercrawler")
out.mkdir(parents=True, exist_ok=True)
stamp = datetime.date.today().isoformat()
src = sqlite3.connect("/opt/careercrawler/state/crawler.db")
dst = sqlite3.connect(str(out / f"crawler-{stamp}.db"))
with dst:
    src.backup(dst)          # safe against a running crawl
dst.close(); src.close()
print("backed up to", out / f"crawler-{stamp}.db")
PY
```

Also worth keeping: `state/completed/` (one archived checkpoint per finished
run) and `/etc/careercrawler/` (config and key). `output/` is logs and
diagnostics — useful, not precious.

---

## 9. Upgrading

```bash
cd /opt/careercrawler
sudo -u careercrawler git pull
sudo -u careercrawler ./venv/bin/pip install -r requirements.txt
sudo /opt/careercrawler/deploy/install.sh     # refresh the units
sudo systemctl daemon-reload
```

Safe while a crawl is running: the units are rewritten but the live process is
untouched, and the next run picks up the new code. Do not `git pull` a change to
`store/schema.py` mid-run.

---

## 10. Troubleshooting

**`systemctl start` returns immediately and the unit shows `inactive (dead)`
with status 75.** Another crawler holds the database. `cat state/crawler.lock`
to see who. This is the guard working.

**Resume unit shows `condition failed`.** Not an error — the guard decided there
was nothing to resume. `journalctl -u careercrawler-resume.service -b` says why.

**Exit code 2.** No spreadsheet id or no readable credentials. Check
`/etc/careercrawler/careercrawler.env` and that the key file is readable by the
service user.

**Chromium fails to launch.** `./venv/bin/python -m playwright install chromium`
as the service user, then `install-deps`. The crawl still runs without it;
client-side boards just fail.

**`ProtectSystem=strict` permission errors.** The service can write only to
`state/` and `output/`. If you move either, update `ReadWritePaths=` in both
units.

**A stop takes 15 minutes and then the process is killed.** `TimeoutStopSec=900`.
Batches have run from 14 to 95 minutes on the real roster, so a stop during a
slow batch will be escalated to `SIGKILL`. That is survivable — the batch
replays on the next run and every write is keyed — but raise `TimeoutStopSec` if
you would rather wait than replay.
