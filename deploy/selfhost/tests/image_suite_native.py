"""Run the platform test suite the way `sanagtm selftest` runs it inside the app image, without
Docker: from a staged copy of the image's files (Dockerfile COPY set), with the pinned self-host
virtualenv, against an external throwaway PostgreSQL (CAREERCLOUD_TEST_DATABASE_URL).

    python deploy/selfhost/tests/image_suite_native.py --pg-bin <PostgreSQL bin dir> --python <venv python>
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "deploy" / "ops" / "backup"))

from fresh_install_native import stage_image  # noqa: E402
from sana_backup import Cluster  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-bin", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--workdir", type=Path, default=REPO / "cloud" / ".localdev" / "image-suite")
    args = parser.parse_args()
    work = args.workdir.resolve()
    shutil.rmtree(work, ignore_errors=True)
    app = work / "app"
    app.mkdir(parents=True)
    print("staged:", ", ".join(stage_image(app)), flush=True)
    with Cluster(args.pg_bin, work) as cluster:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CAREERCLOUD_", "SANAGTM_"))}
        env.update({"CAREERCLOUD_TEST_DATABASE_URL": cluster.url(), "PYTHONPATH": str(app),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        done = subprocess.run([args.python, "-m", "unittest", "discover", "-s", "cloud/tests", "-t", "."], cwd=app,
                              env=env, capture_output=True, text=True)
    tail = done.stderr.strip().splitlines()
    summary = [line for line in tail if line.startswith(("Ran ", "OK", "FAILED"))]
    print("\n".join(summary))
    log = work.parent / "image-suite.log"
    log.write_text(done.stderr, encoding="utf-8")
    print(f"full output: {log}")
    if done.returncode == 0:
        shutil.rmtree(work, ignore_errors=True)
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
