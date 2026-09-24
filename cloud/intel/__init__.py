"""The CareerCrawler platform: company + hiring intelligence, CRM and GTM automation.

Built on CareerCloud (``cloud/``): the same auth, PostgreSQL/RLS, Redis queue
and worker patterns, extended from per-user crawl jobs to workspace-scoped
intelligence. See ``cloud/intel/README.md`` for the architecture map.

Nothing in this package imports the production crawler's ``store``, ``sheets``,
``crawler.weekly_run`` or SQLite; ``cloud/tests/test_isolation.py`` and
``cloud/tests/test_platform_isolation.py`` enforce that.
"""
