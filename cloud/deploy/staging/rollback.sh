#!/usr/bin/env bash
# Roll CareerCloud STAGING back to the previous release (or a named one).
#
#   sudo ./rollback.sh                    # previous release recorded by install.sh
#   sudo ./rollback.sh --release <commit> # a specific installed release
#
# Database migrations are forward-only and additive; rolling code back does not
# roll the schema back. Every migration so far is backward-compatible with the
# previous release.

set -euo pipefail
ROOT=/opt/careercloud-staging
die() { echo "rollback.sh: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

TARGET=""
if [[ "${1:-}" == "--release" ]]; then
    [[ "${2:-}" =~ ^[0-9a-f]{7,40}$ ]] || die "--release needs a commit hash"
    TARGET="$ROOT/releases/$2"
else
    [[ -f "$ROOT/previous-release" ]] || die "no previous release recorded"
    TARGET="$(cat "$ROOT/previous-release")"
fi
[[ -d "$TARGET" && -x "$TARGET/.venv/bin/python" ]] || die "release $TARGET is not installed"

CURRENT="$(readlink -f "$ROOT/current")"
echo "rolling back: $CURRENT -> $TARGET"
# SIGTERM first: running jobs are released back to the queue, not lost.
systemctl stop careercloud-staging-worker
ln -sfn "$TARGET" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"
echo "$CURRENT" > "$ROOT/previous-release"
systemctl restart careercloud-staging-api
systemctl start careercloud-staging-worker
sleep 3
curl -fsS http://127.0.0.1:8180/api/v1/health && echo
