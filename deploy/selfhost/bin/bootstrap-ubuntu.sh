#!/usr/bin/env bash
# Prepare a fresh Ubuntu Server 24.04 VM for SANA GTM, then hand over to `sanagtm install`.
#
#   sudo bash bootstrap-ubuntu.sh [--data-disk /dev/sdb] [--repo URL] [--ref BRANCH_OR_TAG]
#
# Installs: Docker Engine + compose plugin (Docker's official apt repository), git, ufw,
# unattended-upgrades; a firewall that only allows SSH in; the SANA GTM code at /opt/sanagtm/app;
# the `sanagtm` command; the backup / health / capacity timers. With --data-disk, an EMPTY disk is
# formatted ext4 and mounted at /srv/sanagtm (a disk with any filesystem is never touched).
set -euo pipefail

REPO="https://github.com/kallurinarsimhareddy/Crawler.git"
REF="feature/careercrawler-platform"
DATA_DISK=""
APP=/opt/sanagtm/app
DATA=/srv/sanagtm

while [[ $# -gt 0 ]]; do case $1 in
  --data-disk) DATA_DISK=$2; shift 2 ;;
  --repo) REPO=$2; shift 2 ;;
  --ref) REF=$2; shift 2 ;;
  *) echo "unknown option $1" >&2; exit 2 ;;
esac; done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || echo "warning: tested on Ubuntu 24.04; this is $PRETTY_NAME" >&2

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl git ufw unattended-upgrades python3 acl
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null

echo "== firewall: deny incoming except SSH (SANA GTM is reached only through the Cloudflare Tunnel)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

if [[ -n "$DATA_DISK" ]]; then
  echo "== data disk $DATA_DISK -> $DATA"
  [[ -b "$DATA_DISK" ]] || { echo "$DATA_DISK is not a block device" >&2; exit 1; }
  if [[ -n "$(lsblk -no FSTYPE "$DATA_DISK" | tr -d '[:space:]')" || -n "$(lsblk -no NAME "$DATA_DISK" | tail -n +2)" ]]; then
    echo "$DATA_DISK already has a filesystem or partitions; refusing to format it" >&2; exit 1
  fi
  mkfs.ext4 -q -L sanagtm-data "$DATA_DISK"
  mkdir -p "$DATA"
  uuid=$(blkid -s UUID -o value "$DATA_DISK")
  grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $DATA ext4 defaults,noatime 0 2" >> /etc/fstab
  mount "$DATA"
fi
mkdir -p "$DATA"

echo "== code -> $APP ($REF)"
if [[ -d "$APP/.git" ]]; then
  git -C "$APP" fetch --quiet --tags origin && git -C "$APP" checkout --quiet "$REF" && git -C "$APP" pull --ff-only --quiet || true
else
  mkdir -p "$(dirname "$APP")"
  git clone --quiet --branch "$REF" "$REPO" "$APP"
fi
install -m 0755 "$APP/deploy/selfhost/bin/sanagtm" /usr/local/bin/sanagtm
install -m 0644 "$APP"/deploy/selfhost/systemd/sanagtm-*.service "$APP"/deploy/selfhost/systemd/sanagtm-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sanagtm-backup.timer sanagtm-health.timer sanagtm-capacity.timer

echo ""
echo "Ready. Next: sudo sanagtm install"
