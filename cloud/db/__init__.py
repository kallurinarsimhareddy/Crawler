"""PostgreSQL / Supabase persistence for CareerCloud.

Only cloud jobs live here. CareerCrawler's own SQLite database
(``state/crawler.db``) is never opened by anything under ``cloud/`` —
:func:`cloud.db.connection.resolve_database_url` refuses anything that is not a
PostgreSQL URL, and ``cloud/tests/test_isolation.py`` checks that no cloud module
imports ``sqlite3`` or the crawler's ``store`` package.
"""
