"""job_postings row-level security on a real PostgreSQL (migration 0012's set-based read policy):
members read their workspace's jobs, nobody else reads anything, and writes keep the 0003 rules."""

from __future__ import annotations

import json
import uuid

from cloud.tests._pg import PostgresTestCase


class JobPostingsRlsTests(PostgresTestCase):
    def setUp(self) -> None:
        from cloud.intel.core.context import Ctx
        from cloud.intel.store.postgres import PostgresStore

        self.store = PostgresStore.from_url(self.database_url, max_size=2)
        self.addCleanup(self.store.close)
        self.owner, self.viewer, self.stranger = (str(uuid.uuid4()) for _ in range(3))
        ws = self.store.create_workspace(self.owner, "A", f"rls-a-{uuid.uuid4().hex[:6]}")
        other = self.store.create_workspace(self.stranger, "B", f"rls-b-{uuid.uuid4().hex[:6]}")
        self.ws, self.other = ws["id"], other["id"]
        ctx = Ctx(self.ws, self.owner, "owner")
        self.store.add_member(ctx, self.viewer, "viewer")
        now = "2026-10-01T00:00:00+00:00"
        self.store.insert_many(ctx, "job_postings", [
            {"title": f"Job {i}", "job_url": f"https://x.io/{i}", "url_key": f"https://x.io/{i}", "source_kind": "import",
             "source_name": "t", "first_seen_at": now, "last_seen_at": now} for i in range(3)])
        self.store.insert(Ctx(self.other, self.stranger, "owner"), "job_postings", {
            "title": "Other", "job_url": "https://y.io/1", "url_key": "https://y.io/1", "source_kind": "import",
            "source_name": "t", "first_seen_at": now, "last_seen_at": now})

    def _as(self, user: str, sql: str, params=()):
        import psycopg

        with psycopg.connect(self.database_url) as conn:
            with conn.transaction():
                conn.execute("select set_config('request.jwt.claims', %s, true)",
                             [json.dumps({"sub": user, "role": "authenticated"})])
                conn.execute("set local role authenticated")
                return conn.execute(sql, params).fetchall()

    def test_members_only(self) -> None:
        count = "select count(*) from careercloud.job_postings where workspace_id = %s"
        self.assertEqual(self._as(self.owner, count, [self.ws])[0][0], 3)
        self.assertEqual(self._as(self.viewer, count, [self.ws])[0][0], 3)
        self.assertEqual(self._as(self.stranger, count, [self.ws])[0][0], 0)
        self.assertEqual(self._as(self.owner, "select count(*) from careercloud.job_postings")[0][0], 3)
        self.assertEqual(self._as(str(uuid.uuid4()), "select count(*) from careercloud.job_postings")[0][0], 0)

    def test_writes_keep_their_rules(self) -> None:
        import psycopg

        # a viewer can read but not change rows (update policy is still can_write)
        updated = self._as(self.viewer, "update careercloud.job_postings set title = 'x' where workspace_id = %s "
                                        "returning id", [self.ws])
        self.assertEqual(updated, [])
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self._as(self.stranger, "insert into careercloud.job_postings (id, workspace_id, title, job_url, url_key, "
                                    "source_kind, source_name, first_seen_at, last_seen_at, created_by) values "
                                    "('jp_' || md5(random()::text), %s, 't', 'https://z.io', 'https://z.io', 'import', "
                                    "'t', now(), now(), %s) returning id", [self.ws, self.stranger])
