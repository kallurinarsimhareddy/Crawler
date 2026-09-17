"""Row-level security, tested in the database itself.

These tests bypass the repository and speak SQL as the ``authenticated`` role
with a user's JWT claims set — exactly what Supabase's Data API would do if the
schema were ever exposed, and what the API's user-scoped transactions do. They
prove the database refuses cross-tenant access even if application code forgot
an ``owner_id`` filter.
"""

from __future__ import annotations

import json
import unittest
import uuid
from contextlib import contextmanager

from cloud.tests._pg import PostgresTestCase

ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())


def jid() -> str:
    return f"job_{uuid.uuid4().hex}"


class TestRowLevelSecurity(PostgresTestCase):
    def setUp(self) -> None:
        import psycopg

        self.truncate()
        self.alice_job, self.bob_job = jid(), jid()
        with psycopg.connect(self.database_url, autocommit=True) as conn:  # as the table owner
            for job_id, owner in ((self.alice_job, ALICE), (self.bob_job, BOB)):
                conn.execute(
                    "insert into careercloud.jobs (id, owner_id, type, target_count) values (%s, %s, 'single_company', 1)",
                    [job_id, owner],
                )
                conn.execute(
                    "insert into careercloud.crawl_targets (job_id, position, website) values (%s, 0, 'https://a.example.com')",
                    [job_id],
                )
                conn.execute("insert into careercloud.job_events (job_id, kind) values (%s, 'created')", [job_id])
                conn.execute(
                    "insert into careercloud.job_results (id, job_id, owner_id, kind, filename, content_type, storage_key, size_bytes, sha256)"
                    " values (%s, %s, %s, 'jobs_csv', 'jobs.csv', 'text/csv', %s, 1, %s)",
                    [f"res_{uuid.uuid4().hex}", job_id, owner, f"results/x/{job_id}/jobs.csv", "b" * 64],
                )

    @contextmanager
    def as_user(self, user_id, role: str = "authenticated"):
        import psycopg

        with psycopg.connect(self.database_url) as conn:
            with conn.transaction():
                if user_id is not None:
                    conn.execute(
                        "select set_config('request.jwt.claims', %s, true)",
                        [json.dumps({"sub": user_id, "role": role})],
                    )
                conn.execute(f"set local role {role}")
                yield conn

    def assertRefused(self, statement: str, params=None, user=ALICE) -> None:
        import psycopg

        with self.assertRaises(psycopg.Error):
            with self.as_user(user) as conn:
                conn.execute(statement, params)

    def count(self, table: str, user=ALICE) -> int:
        with self.as_user(user) as conn:
            return conn.execute(f"select count(*) from careercloud.{table}").fetchone()[0]

    # --- reads ---------------------------------------------------------------

    def test_each_user_sees_only_their_own_rows_in_every_table(self) -> None:
        for table in ("jobs", "crawl_targets", "job_events", "job_results"):
            with self.subTest(table=table):
                self.assertEqual(self.count(table, ALICE), 1)
                self.assertEqual(self.count(table, BOB), 1)
        with self.as_user(ALICE) as conn:
            ids = [row[0] for row in conn.execute("select id from careercloud.jobs").fetchall()]
        self.assertEqual(ids, [self.alice_job])

    def test_no_claims_means_no_rows(self) -> None:
        for table in ("jobs", "crawl_targets", "job_events", "job_results"):
            with self.subTest(table=table):
                self.assertEqual(self.count(table, None), 0)

    def test_the_anon_role_has_no_access_at_all(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            conn.execute("do $$ begin if not exists (select 1 from pg_roles where rolname='anon') then create role anon nologin; end if; end $$")
        with self.assertRaises(psycopg.Error):
            with self.as_user(ALICE, role="anon") as conn:
                conn.execute("select count(*) from careercloud.jobs")

    # --- writes --------------------------------------------------------------

    def test_a_user_cannot_create_a_job_for_someone_else(self) -> None:
        self.assertRefused(
            "insert into careercloud.jobs (id, owner_id, type) values (%s, %s, 'single_company')", [jid(), BOB]
        )

    def test_a_user_cannot_create_a_job_that_is_already_running(self) -> None:
        self.assertRefused(
            "insert into careercloud.jobs (id, owner_id, type, status, worker_id, lease_expires_at)"
            " values (%s, %s, 'single_company', 'running', 'me', now())",
            [jid(), ALICE],
        )

    def test_a_user_can_create_their_own_queued_job(self) -> None:
        with self.as_user(ALICE) as conn:
            conn.execute("insert into careercloud.jobs (id, owner_id, type) values (%s, %s, 'single_company')", [jid(), ALICE])
        self.assertEqual(self.count("jobs", ALICE), 2)

    def test_a_user_cannot_cancel_or_touch_someone_elses_job(self) -> None:
        with self.as_user(ALICE) as conn:
            updated = conn.execute(
                "update careercloud.jobs set status = 'cancelled', cancel_requested_at = now(), completed_at = now()"
                " where id = %s",
                [self.bob_job],
            ).rowcount
        self.assertEqual(updated, 0)
        with self.as_user(BOB) as conn:
            self.assertEqual(
                conn.execute("select status from careercloud.jobs where id = %s", [self.bob_job]).fetchone()[0],
                "queued",
            )

    def test_a_user_can_cancel_their_own_job(self) -> None:
        with self.as_user(ALICE) as conn:
            updated = conn.execute(
                "update careercloud.jobs set status = 'cancelled', cancel_requested_at = now(), completed_at = now()"
                " where id = %s",
                [self.alice_job],
            ).rowcount
        self.assertEqual(updated, 1)

    def test_a_user_cannot_mark_their_own_job_completed(self) -> None:
        self.assertRefused(
            "update careercloud.jobs set status = 'completed', completed_at = now() where id = %s", [self.alice_job]
        )

    def test_a_user_cannot_change_execution_columns_even_on_their_own_job(self) -> None:
        for statement in (
            "update careercloud.jobs set attempts = 0 where id = %s",
            "update careercloud.jobs set worker_id = 'me' where id = %s",
            "update careercloud.jobs set lease_expires_at = now() where id = %s",
            "update careercloud.jobs set owner_id = gen_random_uuid() where id = %s",
            "update careercloud.jobs set error = 'x' where id = %s",
        ):
            with self.subTest(statement=statement):
                self.assertRefused(statement, [self.alice_job])

    def test_a_user_cannot_write_results_or_foreign_events(self) -> None:
        self.assertRefused(
            "insert into careercloud.job_results (id, job_id, owner_id, kind, filename, content_type, storage_key, size_bytes, sha256)"
            " values (%s, %s, %s, 'jobs_xlsx', 'jobs.xlsx', 'x', 'results/a/b', 1, %s)",
            [f"res_{uuid.uuid4().hex}", self.alice_job, ALICE, "c" * 64],
        )
        self.assertRefused(
            "insert into careercloud.job_events (job_id, kind) values (%s, 'created')", [self.bob_job]
        )
        self.assertRefused(
            "insert into careercloud.job_events (job_id, kind) values (%s, 'completed')", [self.alice_job]
        )
        self.assertRefused("delete from careercloud.jobs where id = %s", [self.alice_job])

    # --- the status machine --------------------------------------------------

    def test_the_trigger_refuses_illegal_transitions_for_everyone(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:  # even the table owner
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "update careercloud.jobs set status = 'completed', completed_at = now() where id = %s",
                    [self.alice_job],
                )
            conn.execute(
                "update careercloud.jobs set status = 'cancelled', completed_at = now() where id = %s", [self.alice_job]
            )
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "update careercloud.jobs set status = 'queued', completed_at = null where id = %s", [self.alice_job]
                )
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute("update careercloud.jobs set owner_id = %s where id = %s", [ALICE, self.bob_job])

    def test_result_owner_must_match_the_job_owner(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                conn.execute(
                    "insert into careercloud.job_results (id, job_id, owner_id, kind, filename, content_type, storage_key, size_bytes, sha256)"
                    " values (%s, %s, %s, 'jobs_xlsx', 'jobs.xlsx', 'x', 'results/a/b', 1, %s)",
                    [f"res_{uuid.uuid4().hex}", self.alice_job, BOB, "d" * 64],
                )

    def test_storage_keys_cannot_traverse(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            for key in ("results/../../etc/passwd", "/abs/path", "C:\\x"):
                with self.subTest(key=key), self.assertRaises(psycopg.errors.CheckViolation):
                    conn.execute(
                        "insert into careercloud.job_results (id, job_id, owner_id, kind, filename, content_type, storage_key, size_bytes, sha256)"
                        " values (%s, %s, %s, 'crawl_log', 'crawl.log', 'x', %s, 1, %s)",
                        [f"res_{uuid.uuid4().hex}", self.alice_job, ALICE, key, "e" * 64],
                    )


class TestMigrations(PostgresTestCase):
    def test_applying_twice_is_a_no_op(self) -> None:
        from cloud.db.migrate import apply_migrations

        self.assertEqual(apply_migrations(self.database_url), [])

    def test_an_edited_migration_is_refused(self) -> None:
        from cloud.db.migrate import Migration, MigrationError, apply_migrations, load_migrations

        original = load_migrations()[0]
        edited = Migration(original.version, original.path, original.sql + "\n-- edited", "0" * 64)
        with self.assertRaises(MigrationError):
            apply_migrations(self.database_url, [edited])

    def test_a_fresh_database_gets_every_table(self) -> None:
        import psycopg

        from cloud.tests._pg import drop_database, fresh_database

        url = fresh_database()
        try:
            with psycopg.connect(url) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select tablename from pg_tables where schemaname = 'careercloud'"
                    ).fetchall()
                }
                rls = {
                    row[0]
                    for row in conn.execute(
                        "select relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
                        " where n.nspname = 'careercloud' and c.relrowsecurity"
                    ).fetchall()
                }
        finally:
            drop_database(url)
        self.assertEqual(tables, {"jobs", "crawl_targets", "job_events", "job_results", "schema_migrations", "deployment"})
        self.assertEqual(rls, {"jobs", "crawl_targets", "job_events", "job_results", "deployment"})


if __name__ == "__main__":
    unittest.main()
