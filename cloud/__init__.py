"""CareerCloud: the control plane that will put CareerCrawler behind a website.

Nothing in this package imports the crawler. The crawler engine, its adapters,
``state/crawler.db`` and the Google Sheet belong to the weekly run and stay
exactly where they are; this package only describes *jobs* — what someone asked
for, where it has got to, and how it ended — and exposes them over HTTP.

The shape it is built to grow into::

    web (React)  ->  api (FastAPI)  ->  job queue  ->  worker  ->  crawler engine

Phase 5A stops before the queue. Jobs live in memory, and the only runner is
:class:`cloud.worker.fake_runner.FakeRunner`, which pretends to crawl without
touching the network or the disk. See ``cloud/README.md``.
"""
