# SANA GTM, self-hosted

The complete application on your own Ubuntu Server 24.04 VM:

- PostgreSQL 17
- the API
- the background worker, which runs every crawler and task through the PostgreSQL queue
- the dashboard
- a Cloudflare Tunnel

There is **no Supabase database and no Redis/Upstash**. Sign-in stays on Supabase Auth (free tier): your existing accounts and user IDs keep working.

```
Browser ─https─► Cloudflare ─tunnel (outbound only)─► VM: cloudflared ─► web (nginx: dashboard + /api proxy)
                                                                         └─► api ─┐
                                                       worker (all task kinds) ───┼─► postgres:17 (private network)
                                                       migrate (one-shot, on start)┘
```

| Path on the VM | What |
|---|---|
| `/opt/sanagtm/app` | code (git checkout) |
| `/etc/sanagtm/` | configuration; `sanagtm.env`, `postgres.env` and `tunnel.env` hold **secrets** (root, 0600) |
| `/srv/sanagtm/pgdata` | the database |
| `/srv/sanagtm/files`, `results` | uploads, exports |
| `/srv/sanagtm/backups` | nightly verified dumps + files + configuration |
| `/srv/sanagtm/run` | worker heartbeat, health.json, capacity.csv, resource registry |

## VM specification

| | Minimum | **Recommended** | Growth (millions of jobs) |
|---|---|---|---|
| vCPU | 2 | **4** | 4 |
| RAM | 4 GB | **8 GB** | 12–16 GB |
| Disk 1 (Ubuntu + images) | 20 GB | **30 GB** | 30 GB |
| Disk 2 (`/srv/sanagtm`, data) | 20 GB | **80 GB** (thin) | 250–300 GB + an external backup disk |
| Network | NAT | **NAT** (outbound only) | NAT |

For 8 GB RAM, set `SANAGTM_PG_SHARED_BUFFERS=2GB` and `SANAGTM_PG_CACHE=5GB` in `/etc/sanagtm/compose.env`. The defaults (1 GB / 3 GB) suit 4–8 GB.

## 1. On Windows (one time)

1. **Check the PC** (no admin needed):
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\selfhost\windows\check-host.ps1
   ```
2. **An administrator installs VMware Workstation Pro.** It is free for personal and commercial use; download it from Broadcom's support portal (a free account is needed). Use the default options. Windows' virtualization-based security can stay on: Workstation 17.5+ runs on top of it.
3. **Download Ubuntu Server 24.04 LTS** (`ubuntu-24.04.x-live-server-amd64.iso`) from ubuntu.com.
4. **Create the VM** with VMware: File › New Virtual Machine › Custom.
   - Guest: Linux / Ubuntu 64-bit.
   - Location `E:\VMs\sanagtm`. **Not on C:**, which has only about 6 GB free.
   - 4 processors, 8192 MB of memory, NAT network.
   - Disk 1: 30 GB, single file. Then VM Settings › Add › Hard Disk: 80 GB, **not pre-allocated** (this becomes `/dev/sdb`).
5. **Install Ubuntu Server.** Choose "OpenSSH server" and set a strong password. Do not install the docker snap offered by the installer.
6. **Keep the PC awake:** Settings › System › Power › Sleep = Never. The VM pauses when Windows sleeps.
7. **Start the VM at logon** with a per-user scheduled task (no admin needed):
   ```powershell
   schtasks /Create /SC ONLOGON /TN "SANA GTM VM" /TR "\"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe\" -T ws start E:\VMs\sanagtm\sanagtm.vmx nogui"
   ```
   Starting at power-on, before anyone logs in, needs an administrator: same task with `/RU SYSTEM`.

## 2. Cloudflare (one time, in the dashboard)

1. You need a domain whose DNS is on Cloudflare. A subdomain of a domain you already have there costs nothing extra.
2. Go to Zero Trust › Networks › Tunnels › **Create a tunnel** › Cloudflared, named `sanagtm`. Copy the **token**: it is shown in the install command as `--token eyJ…`.
3. Add a public hostname to the tunnel: `sanagtm.<your-domain>` → service **HTTP** `web:8080`.
4. In **Supabase** › Authentication › URL Configuration, set the Site URL to `https://sanagtm.<your-domain>` and add it to the redirect URLs, so password-reset links come back to the VM.

## 3. On the VM

Copy the code to the VM. Either:

- clone it from GitHub once the self-host branch is pushed, or
- without pushing, bundle it on Windows and copy the bundle:
  ```powershell
  git -C E:\Crawlers\CareerCrawler-sana-safety bundle create E:\sanagtm.bundle fix/production-safety
  scp E:\sanagtm.bundle <user>@<vm-ip>:/tmp/
  ```
  Then on the VM:
  ```bash
  git clone /tmp/sanagtm.bundle /tmp/sanagtm-src
  sudo bash /tmp/sanagtm-src/deploy/selfhost/bin/bootstrap-ubuntu.sh --data-disk /dev/sdb --repo /tmp/sanagtm.bundle --ref fix/production-safety
  ```

The bootstrap script:

- installs Docker Engine and the compose plugin from Docker's repository
- enables a firewall that allows only SSH in
- formats the **empty** second disk and mounts it at `/srv/sanagtm`
- places the code in `/opt/sanagtm/app`
- installs the `sanagtm` command and the backup, health and capacity timers

Then:

```bash
sudo sanagtm install
```

It asks only for:

- the hostname
- the Supabase Auth URL
- the public anon key
- the existing `CAREERCLOUD_PLATFORM_SECRETS_KEY`, if you are migrating (Enter = generate a new one)
- the tunnel token

It generates the database passwords, builds the images, starts everything, waits for the health checks, and prints the URL. Non-interactive: `sudo sanagtm install --answers answers.env` (same variable names; see the tests).

Then sign in at `https://sanagtm.<your-domain>` with your existing account.

## 4. Operating it

| Task | Command |
|---|---|
| status / health | `sudo sanagtm status` · `sudo sanagtm health` (exit 0 = healthy) |
| start / stop / restart | `sudo sanagtm start` · `stop` · `restart [api\|worker\|web\|postgres]` |
| logs | `sudo sanagtm logs api -f` · `sudo sanagtm logs worker --since 1h` |
| backup now | `sudo sanagtm backup` (dump + manifest + files + config; nightly at 02:30 by timer, then a restore test) |
| list backups | `sudo sanagtm backups` |
| test a backup | `sudo sanagtm restore /srv/sanagtm/backups/<file>.dump` (scratch database, dropped afterwards) |
| restore for real | `sudo sanagtm restore <file>.dump --live` (asks for the hostname; takes a safety backup first) |
| disk / database usage | `sudo sanagtm capacity` |
| update | `sudo sanagtm update [--to <tag>]` (backup → fetch → rebuild → migrations → health; prints the rollback) |
| self-test | `sudo sanagtm selftest` (the full platform test suite inside the image, on scratch databases) |
| uninstall | `sudo sanagtm uninstall` keeps the data · `--purge-data` needs a backup under 24 h old and typed confirmation |

**Off-machine backups.** The nightly backups are on the VM's data disk, which is the same physical NVMe as Windows. Copy them off daily with `deploy/selfhost/windows/pull-backups.ps1` to a USB disk or another machine. It verifies the SHA-256 of every copy. On the VM, create a read-only account for it:

```bash
sudo useradd -m -s /bin/bash sanabackup && sudo setfacl -R -m u:sanabackup:rX -m d:u:sanabackup:rX /srv/sanagtm/backups
# put the public key of the Windows key pair (ssh-keygen -f %USERPROFILE%\.ssh\sanagtm_backup) in ~sanabackup/.ssh/authorized_keys
```

Keep `CAREERCLOUD_PLATFORM_SECRETS_KEY` in a password manager. Without it, stored provider credentials (ZoomInfo, Seamless, EmailListVerify, AI keys) cannot be decrypted after a rebuild.

## Security model

- **Network:** no published ports. PostgreSQL is only on the private `backend` network. The only ingress is the outbound tunnel to `web`, and the firewall allows only SSH.
- **Production checks:** the API runs as `production` with every production check on: Supabase auth, no dev tokens, no service-role key, JSON logs, an https origin, and a resource registry that also lists the old hosted database as **another environment**, so this install refuses to connect to it.
- **Self-hosted exceptions (`CAREERCLOUD_SELF_HOSTED=1`):** local file storage on the data disk, and no TLS between containers on the private network. A public database host still requires `sslmode=require`.
- **Containers:** they run as uid 10001 with all capabilities dropped and `no-new-privileges`. Tables are owned by the `sanagtm` role (not a superuser), and row-level security applies to every user request.
- **Secrets:** they live only in `/etc/sanagtm` (0600). They are never on a command line: the CLI passes the admin URL to containers by name (`-e VAR`), and the backup tool hands credentials to `pg_dump`/`pg_restore` through PG* variables. The `.dockerignore` keeps every `.env` file out of the images.

## Verification

See `docs/self-hosting-audit/SELF_HOSTED_PACKAGE.md` for the results. On a machine without Docker:

```powershell
# pinned dependency set in a clean venv
py -3.12 -m venv E:\Crawlers\sana-selfhost-check\venv
E:\Crawlers\sana-selfhost-check\venv\Scripts\python -m pip install -r deploy\selfhost\requirements.txt -r deploy\selfhost\requirements-test.txt
$py = "E:\Crawlers\sana-selfhost-check\venv\Scripts\python.exe"; $pg = "<a PostgreSQL bin directory>"
& $py deploy\selfhost\tests\fresh_install_native.py --pg-bin $pg --python $py       # install → run → use → backup → restore
& $py deploy\selfhost\tests\image_suite_native.py   --pg-bin $pg --python $py       # = sanagtm selftest
& $py deploy\selfhost\tests\migration_rehearsal.py  --pg-bin $pg --python $py --source-dump <any careercloud dump>
cloud\.venv\Scripts\python -m unittest cloud.tests.test_selfhost cloud.tests.test_selfhost_package
```

On the VM, after `sanagtm install`, run `sudo sanagtm selftest` and `sudo sanagtm health`.
