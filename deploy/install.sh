#!/usr/bin/env bash
#
# Install the CareerCrawler systemd units, with paths substituted.
#
#     sudo CC_HOME=/opt/careercrawler CC_USER=careercrawler ./deploy/install.sh
#
# The units ship with @CC_HOME@ and @CC_USER@ placeholders rather than real
# paths, because systemd expands environment variables in an ExecStart's
# *arguments* but not in `WorkingDirectory=`, `ExecCondition=`, or the executable
# itself. Those three have to be literal, so they are substituted here at install
# time -- which is also what keeps a developer's own directory layout out of the
# committed files.
#
# Idempotent: run it again after `git pull` to refresh the units.
#
# This script installs and enables. It never starts a crawl.

set -euo pipefail

CC_HOME="${CC_HOME:-/opt/careercrawler}"
CC_USER="${CC_USER:-careercrawler}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
ETC_DIR="${ETC_DIR:-/etc/careercrawler}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '  %s\n' "$*"; }

echo "Installing CareerCrawler units"
say "install root : ${CC_HOME}"
say "service user : ${CC_USER}"
say "unit dir     : ${UNIT_DIR}"

if [[ ! -d "${CC_HOME}" ]]; then
    echo "ERROR: ${CC_HOME} does not exist. Deploy the code there first." >&2
    exit 1
fi

if [[ ! -x "${CC_HOME}/venv/bin/python" ]]; then
    echo "ERROR: no interpreter at ${CC_HOME}/venv/bin/python." >&2
    echo "       Create the virtualenv first -- see deploy/README.deploy.md." >&2
    exit 1
fi

if ! id -u "${CC_USER}" >/dev/null 2>&1; then
    echo "ERROR: user ${CC_USER} does not exist. Create it first:" >&2
    echo "       sudo useradd --system --home ${CC_HOME} --shell /usr/sbin/nologin ${CC_USER}" >&2
    exit 1
fi

# --- the units ------------------------------------------------------------
substitute() {
    sed -e "s|@CC_HOME@|${CC_HOME}|g" -e "s|@CC_USER@|${CC_USER}|g" "$1"
}

for unit in careercrawler.service careercrawler.timer careercrawler-resume.service; do
    substitute "${HERE}/${unit}" > "${UNIT_DIR}/${unit}"
    chmod 0644 "${UNIT_DIR}/${unit}"
    say "installed ${UNIT_DIR}/${unit}"
done

# The resume guard is referenced by absolute path from the unit, so it needs the
# same substitution and the executable bit.
substitute "${HERE}/careercrawler-should-resume.sh" > "${CC_HOME}/deploy/careercrawler-should-resume.sh.installed"
mv "${CC_HOME}/deploy/careercrawler-should-resume.sh.installed" "${CC_HOME}/deploy/careercrawler-should-resume.sh"
chmod 0755 "${CC_HOME}/deploy/careercrawler-should-resume.sh"
say "installed ${CC_HOME}/deploy/careercrawler-should-resume.sh"

# --- configuration --------------------------------------------------------
mkdir -p "${ETC_DIR}"
if [[ ! -f "${ETC_DIR}/careercrawler.env" ]]; then
    cp "${HERE}/careercrawler.env.example" "${ETC_DIR}/careercrawler.env"
    chmod 0600 "${ETC_DIR}/careercrawler.env"
    chown "${CC_USER}:${CC_USER}" "${ETC_DIR}/careercrawler.env"
    say "created ${ETC_DIR}/careercrawler.env -- EDIT IT before the first run"
else
    say "kept existing ${ETC_DIR}/careercrawler.env"
fi

# --- writable directories -------------------------------------------------
# ProtectSystem=strict makes the whole filesystem read-only for the service
# except ReadWritePaths, so these two must exist and belong to the service user.
mkdir -p "${CC_HOME}/state" "${CC_HOME}/output"
chown -R "${CC_USER}:${CC_USER}" "${CC_HOME}/state" "${CC_HOME}/output"
say "state/ and output/ are writable by ${CC_USER}"

# --- enable ---------------------------------------------------------------
systemctl daemon-reload
systemctl enable careercrawler.timer
systemctl enable careercrawler-resume.service

# careercrawler.service is deliberately NOT enabled: it has no [Install]
# section and is started by its timer, by the resume unit, or by hand. Enabling
# it would run a crawl at every boot.

echo
echo "Installed. Nothing has been started."
echo
echo "  1. Edit ${ETC_DIR}/careercrawler.env"
echo "  2. Put the service-account key where it names, mode 0600"
echo "  3. systemctl start careercrawler.timer"
echo "  4. systemctl list-timers careercrawler.timer"
echo
echo "A first manual run:  systemctl start careercrawler.service"
echo "Watch it:            journalctl -u careercrawler.service -f"
