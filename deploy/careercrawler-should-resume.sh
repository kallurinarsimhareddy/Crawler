#!/usr/bin/env bash
#
# Should the boot-time resume actually run?
#
# Exits 0 to resume, 1 to skip. Used as `ExecCondition=` in
# careercrawler-resume.service, where a non-zero exit skips the unit cleanly
# rather than recording a failure.
#
# This guard exists because "resume" is the crawler's *default*, not a mode: a
# run started with no checkpoint crawls all 12,377 companies from the beginning.
# Without a condition, every reboot would launch a twenty-hour full crawl, which
# is emphatically not what "resume after restart" is meant to mean.
#
# A checkpoint only exists when a run was interrupted -- a completed run archives
# its own -- so its presence is the signal. Two further things are checked
# because the crawler would otherwise start fresh anyway:
#
#   durable=true     A checkpoint written before its batch was persisted marks
#                    companies done whose postings were never stored. The
#                    crawler refuses to resume one; there is no point booting a
#                    twenty-hour crawl to discover that.
#   week_start       Checkpoints are scoped to their ISO week. One from a
#                    previous week cannot be resumed, and a fresh full crawl at
#                    boot is exactly what this guard prevents.
#
# Nothing here writes anything, and it never touches the database.

set -euo pipefail

CC_HOME="${CAREERCRAWLER_HOME:-@CC_HOME@}"
CC_PYTHON="${CAREERCRAWLER_PYTHON:-${CC_HOME}/venv/bin/python}"
CC_DATABASE="${CAREERCRAWLER_DATABASE:-${CC_HOME}/state/crawler.db}"

# The checkpoint lives beside the database unless --checkpoint says otherwise,
# and the units do not say otherwise.
CC_CHECKPOINT="${CAREERCRAWLER_CHECKPOINT:-$(dirname "${CC_DATABASE}")/checkpoint.json}"

if [[ ! -f "${CC_CHECKPOINT}" ]]; then
    echo "No checkpoint at ${CC_CHECKPOINT}: nothing was interrupted, not resuming."
    exit 1
fi

if [[ ! -x "${CC_PYTHON}" ]]; then
    echo "No interpreter at ${CC_PYTHON}: cannot judge the checkpoint, not resuming."
    exit 1
fi

# Read-only. Prints a reason and exits 0 (resume) or 1 (skip).
exec "${CC_PYTHON}" - "${CC_CHECKPOINT}" <<'PYTHON'
import datetime
import json
import sys

path = sys.argv[1]

try:
    with open(path, encoding="utf-8") as handle:
        checkpoint = json.load(handle)
except (OSError, ValueError) as exc:
    print(f"Checkpoint at {path} is unreadable ({exc}); not resuming.")
    raise SystemExit(1)

if not checkpoint.get("durable"):
    print(
        f"Checkpoint at {path} is not durable -- it was written before its "
        f"batch was persisted, so the crawler would refuse to resume it and "
        f"start a full crawl instead. Not resuming."
    )
    raise SystemExit(1)

today = datetime.date.today()
monday = today - datetime.timedelta(days=today.weekday())
week = checkpoint.get("week_start", "")

if week != monday.isoformat():
    print(
        f"Checkpoint at {path} belongs to week {week or 'unknown'}, not "
        f"{monday.isoformat()}; it cannot be resumed and a fresh full crawl is "
        f"not what a reboot should start. Not resuming."
    )
    raise SystemExit(1)

done = checkpoint.get("completed", 0)
total = checkpoint.get("total", 0)
print(f"Resuming run {checkpoint.get('run_id', '?')} at {done}/{total}.")
raise SystemExit(0)
PYTHON
