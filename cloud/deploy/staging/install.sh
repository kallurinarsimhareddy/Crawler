#!/usr/bin/env bash
# Install or upgrade CareerCloud STAGING on a Linux VM (Ubuntu 22.04/24.04, Oracle Cloud Always Free).
#
#   sudo ./install.sh --release <git-commit> --i-am-deploying-staging
#   sudo ./install.sh --release <git-commit> --archive /tmp/careercloud-<commit>.tar --i-am-deploying-staging
#
# --archive installs from `git archive --format=tar <commit>` copied to the VM, so
# the branch does not have to be pushed anywhere.
#
# What it does, idempotently:
#   1. refuses to run on anything that looks like production or without the explicit flag
#   2. creates system users ccstg-api, ccstg-worker, ccstg-tunnel (no login shell)
#   3. unpacks the given commit into /opt/careercloud-staging/releases/<commit> with its own venv
#   4. installs the nftables egress policy for ccstg-worker and loads it
#   5. installs the three systemd units
#   6. runs the staging safety report as each service user; only then switches
#      /opt/careercloud-staging/current to the new release and restarts the services
#
# What it never does: touch careercrawler.service, the CareerCrawler checkout,
# its state/, output/, secrets/ or Google credentials, or anything Seamless.
# Secrets are not handled here: /etc/careercloud-staging/{api,worker}.env and
# resources.json are written by the operator (see cloud/README.md → Staging).

set -euo pipefail

ROOT=/opt/careercloud-staging
ETC=/etc/careercloud-staging
RUNTIME=/var/lib/careercloud-staging/runtime
REPO_URL="${CAREERCLOUD_REPO_URL:-https://github.com/kallurinarsimhareddy/Crawler.git}"
RELEASE=""
ARCHIVE=""
CONFIRMED=0

die() { echo "install.sh: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --release) RELEASE="${2:-}"; shift 2 ;;
        --archive) ARCHIVE="${2:-}"; shift 2 ;;
        --i-am-deploying-staging) CONFIRMED=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ $CONFIRMED -eq 1 ]] || die "refusing without --i-am-deploying-staging"
[[ "$RELEASE" =~ ^[0-9a-f]{7,40}$ ]] || die "--release must be a git commit hash"
case "$(hostname)" in *prod*) die "hostname $(hostname) looks like production; staging is never installed there" ;; esac
[[ ! -e /etc/careercloud-production ]] || die "/etc/careercloud-production exists on this host; staging and production never share a VM"
for file in api.env worker.env resources.json; do
    [[ -f "$ETC/$file" ]] || die "missing $ETC/$file (write it first; see cloud/README.md)"
done
grep -q '^CAREERCLOUD_ENV=staging$' "$ETC/api.env" || die "$ETC/api.env is not CAREERCLOUD_ENV=staging"
grep -q '^CAREERCLOUD_ENV=staging$' "$ETC/worker.env" || die "$ETC/worker.env is not CAREERCLOUD_ENV=staging"
command -v python3.12 >/dev/null || die "python3.12 is required (apt install python3.12 python3.12-venv)"
command -v nft >/dev/null || die "nftables is required (apt install nftables)"

echo "== users"
for user in ccstg-api ccstg-worker ccstg-tunnel; do
    id "$user" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$user"
done

echo "== directories"
install -d -o root -g root -m 0755 "$ROOT" "$ROOT/releases"
install -d -o root -g root -m 0750 "$ETC"
install -d -o ccstg-worker -g ccstg-worker -m 0700 "$RUNTIME"
chown root:ccstg-api "$ETC/api.env" && chmod 0640 "$ETC/api.env"
chown root:ccstg-worker "$ETC/worker.env" && chmod 0640 "$ETC/worker.env"
chmod 0644 "$ETC/resources.json"
# Both service users read the registry; neither can read the other's secrets.
chmod 0751 "$ETC"

echo "== release $RELEASE"
DEST="$ROOT/releases/$RELEASE"
if [[ ! -d "$DEST" ]]; then
    mkdir -p "$DEST"
    if [[ -n "$ARCHIVE" ]]; then
        [[ -f "$ARCHIVE" ]] || die "archive $ARCHIVE not found"
        tar -x -C "$DEST" -f "$ARCHIVE"
    else
        TMP="$(mktemp -d)"
        git clone --quiet --no-checkout "$REPO_URL" "$TMP/repo"
        git -C "$TMP/repo" fetch --quiet origin "$RELEASE" || true
        git -C "$TMP/repo" archive --format=tar "$RELEASE" | tar -x -C "$DEST"
        rm -rf "$TMP"
    fi
    # Crawler production data is never part of a release (it is git-ignored), but be explicit.
    rm -rf "$DEST/state" "$DEST/output" "$DEST/secrets" "$DEST/input" "$DEST/.env"
    python3.12 -m venv "$DEST/.venv"
    "$DEST/.venv/bin/pip" install --quiet --upgrade pip
    "$DEST/.venv/bin/pip" install --quiet -r "$DEST/cloud/worker/requirements.txt"
    chown -R root:root "$DEST" && chmod -R go-w "$DEST"
fi

echo "== egress firewall"
install -d -m 0755 /etc/nftables.d
install -o root -g root -m 0644 "$DEST/cloud/deploy/staging/nftables/careercloud-staging-egress.nft" /etc/nftables.d/careercloud-staging-egress.nft
grep -q 'include "/etc/nftables.d/\*.nft"' /etc/nftables.conf 2>/dev/null \
    || echo 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf
nft -f /etc/nftables.d/careercloud-staging-egress.nft
systemctl enable --quiet nftables.service

echo "== systemd units"
for unit in careercloud-staging-api careercloud-staging-worker careercloud-staging-tunnel; do
    install -o root -g root -m 0644 "$DEST/cloud/deploy/staging/systemd/$unit.service" "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload

echo "== safety report (as the service users, against the new release)"
# The report reads the env file itself, so no secret ever appears on a command line.
( cd "$DEST" && sudo -u ccstg-api "$DEST/.venv/bin/python" -m cloud.ops.safety_report --component api --env-file "$ETC/api.env" ) \
    || die "API safety report failed; nothing was switched"
( cd "$DEST" && sudo -u ccstg-worker "$DEST/.venv/bin/python" -m cloud.ops.safety_report --component worker --env-file "$ETC/worker.env" ) \
    || die "worker safety report failed; nothing was switched"

echo "== switch current -> $RELEASE"
PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"
ln -sfn "$DEST" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"
[[ -n "$PREVIOUS" && "$PREVIOUS" != "$DEST" ]] && echo "$PREVIOUS" > "$ROOT/previous-release"

systemctl enable --quiet careercloud-staging-api careercloud-staging-worker
systemctl restart careercloud-staging-api careercloud-staging-worker
if [[ -f "$ETC/cloudflared.yml" ]]; then
    systemctl enable --quiet careercloud-staging-tunnel && systemctl restart careercloud-staging-tunnel
fi

sleep 3
curl -fsS http://127.0.0.1:8180/api/v1/health && echo
echo "installed staging release $RELEASE"
