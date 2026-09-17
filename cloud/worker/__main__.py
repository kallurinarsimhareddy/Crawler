"""Run a CareerCloud worker.

    python -m cloud.worker                 # run until Ctrl+C / SIGTERM
    python -m cloud.worker --once          # handle at most one delivery, then exit
    python -m cloud.worker --maintain-once # one reaper/orphan pass, then exit

Run from the repository root so the crawler engine is importable. The worker
reads ``CAREERCLOUD_*`` variables only (see ``cloud/worker/.env.example``); it
never reads the crawler's ``.env``, ``secrets/`` or ``state/``.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:  # the crawler's top-level packages live here
    sys.path.insert(0, str(REPO_ROOT))

from cloud.db.connection import describe_url, resolve_database_url, resolve_redis_url  # noqa: E402
from cloud.shared.queue import RedisJobQueue  # noqa: E402
from cloud.shared.service import JobService, RetryPolicy  # noqa: E402
from cloud.shared.storage import LocalFileStorage  # noqa: E402
from cloud.worker.executor import JobExecutor  # noqa: E402
from cloud.worker.results import ResultWriter  # noqa: E402
from cloud.worker.settings import load_worker_settings  # noqa: E402
from cloud.worker.worker import Worker, WorkerConfig  # noqa: E402

log = logging.getLogger("cloud.worker")


def _load_env_file(path: Optional[str]) -> None:
    if not path:
        return
    from dotenv import load_dotenv  # installed with uvicorn[standard]

    load_dotenv(path, override=False)


def build_worker(stopping: threading.Event) -> Worker:
    settings = load_worker_settings()
    database_url = resolve_database_url(
        settings.database_url, environment=settings.environment, allow_remote=settings.allow_remote_services
    )
    redis_url = resolve_redis_url(
        settings.redis_url, environment=settings.environment, allow_remote=settings.allow_remote_services
    )

    from cloud.db.postgres import PostgresJobRepository

    repository = PostgresJobRepository.from_url(database_url, max_size=settings.db_pool_max)
    queue = RedisJobQueue.from_url(redis_url, prefix=settings.queue_prefix)
    repository.ping()
    queue.ping()

    service = JobService(repository)
    policy = RetryPolicy(
        max_attempts=settings.max_attempts,
        base_delay_seconds=settings.retry_base_delay,
        max_delay_seconds=settings.retry_max_delay,
    )
    if settings.runner == "careercrawler":
        from cloud.worker.careercrawler_runner import CareerCrawlerRunner

        runner = CareerCrawlerRunner(
            company_concurrency=settings.company_concurrency,
            browser_fallback=settings.browser_fallback,
            max_runtime_seconds=settings.max_runtime_seconds,
            runtime_root=settings.runtime_root,
        )
    else:
        from cloud.worker.fake_runner import FakeRunner

        runner = FakeRunner(step_seconds=settings.fake_step_seconds)

    executor = JobExecutor(
        service,
        runner,
        lease_seconds=settings.lease_seconds,
        retry_policy=policy,
        result_writer=ResultWriter(LocalFileStorage(settings.results_dir)),
        runtime_root=settings.runtime_root,
        keep_workspaces=settings.keep_workspaces,
        on_requeue=lambda job_id, delay: queue.enqueue(job_id, delay_seconds=delay),
        stopping=stopping,
    )
    log.info(
        "environment=%s database=%s redis=%s prefix=%s runner=%s runtime=%s results=%s",
        settings.environment,
        describe_url(database_url),
        describe_url(redis_url),
        settings.queue_prefix,
        runner.name,
        settings.runtime_root,
        settings.results_dir,
    )
    return Worker(
        service,
        queue,
        executor,
        config=WorkerConfig(
            concurrency=settings.concurrency,
            poll_interval=settings.poll_interval,
            visibility_timeout=settings.visibility_timeout,
            reap_interval=settings.reap_interval,
            orphan_after_seconds=settings.orphan_after_seconds,
        ),
        retry_policy=policy,
        stopping=stopping,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloud.worker")
    parser.add_argument("--env-file", help="load CAREERCLOUD_* variables from this file first")
    parser.add_argument("--once", action="store_true", help="handle at most one delivery, then exit")
    parser.add_argument("--maintain-once", action="store_true", help="run one maintenance pass, then exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    _load_env_file(args.env_file)

    stopping = threading.Event()
    try:
        worker = build_worker(stopping)
    except ValueError as error:
        log.error("%s", error)
        return 2

    if args.maintain_once:
        report = worker.maintain()
        log.info("maintenance: %s", report)
        return 0
    if args.once:
        worker.process_next()
        return 0

    def stop(signum, _frame) -> None:
        log.info("signal %s: finishing current work and stopping", signum)
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    worker.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
