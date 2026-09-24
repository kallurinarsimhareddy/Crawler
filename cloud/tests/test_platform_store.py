"""The platform store contract, run against MemoryStore and PostgresStore, plus
RLS checked directly in SQL (bypassing the store's own workspace filter)."""

from __future__ import annotations

import json
import unittest
import uuid

from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError
from cloud.intel.store.memory import MemoryStore
from cloud.tests._pg import drop_database, fresh_database


class StoreContract:
    """Mixed into a TestCase that provides ``self.store``."""

    store = None

    def setUp(self) -> None:  # noqa: D401
        super().setUp()
        self.owner = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.ws = self.store.create_workspace(self.owner, "Acme", f"acme-{uuid.uuid4().hex[:8]}")
        self.ctx = Ctx(self.ws["id"], self.owner, "owner")
        self.other_ws = self.store.create_workspace(self.other, "Other", f"other-{uuid.uuid4().hex[:8]}")
        self.other_ctx = Ctx(self.other_ws["id"], self.other, "owner")

    def test_insert_get_defaults_and_version(self) -> None:
        row = self.store.insert(self.ctx, "companies", {"name": "Foo Inc", "domain": "foo.com"})
        self.assertTrue(row["id"].startswith("co_"))
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["aliases"], [])
        self.assertEqual(row["lifecycle"], "prospect")
        self.assertEqual(row["created_by"], self.owner)
        got = self.store.get(self.ctx, "companies", row["id"])
        self.assertEqual(got["name"], "Foo Inc")
        updated = self.store.update(self.ctx, "companies", row["id"], {"industry": "Manufacturing"})
        self.assertEqual(updated["version"], 2)

    def test_optimistic_concurrency(self) -> None:
        row = self.store.insert(self.ctx, "companies", {"name": "Foo"})
        self.store.update(self.ctx, "companies", row["id"], {"city": "Tulsa"}, expected_version=1)
        with self.assertRaises(ConflictError):
            self.store.update(self.ctx, "companies", row["id"], {"city": "Dallas"}, expected_version=1)

    def test_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.insert(self.ctx, "companies", {})
        with self.assertRaises(ValidationError):
            self.store.insert(self.ctx, "companies", {"name": "x", "lifecycle": "bogus"})
        with self.assertRaises(ValidationError):
            self.store.insert(self.ctx, "companies", {"name": "x", "not_a_field": 1})
        with self.assertRaises(ValidationError):
            self.store.insert(self.ctx, "companies", {"name": "x", "workspace_id": self.other_ws["id"]})
        with self.assertRaises(ValidationError):
            self.store.insert(self.ctx, "companies", {"name": "x", "account_score": 101})

    def test_unique_per_workspace(self) -> None:
        self.store.insert(self.ctx, "companies", {"name": "A", "domain": "same.com"})
        with self.assertRaises(ConflictError):
            self.store.insert(self.ctx, "companies", {"name": "B", "domain": "same.com"})
        # the same domain in another workspace is fine
        self.store.insert(self.other_ctx, "companies", {"name": "A", "domain": "same.com"})
        # nullable unique columns do not collide when null
        self.store.insert(self.ctx, "companies", {"name": "C"})
        self.store.insert(self.ctx, "companies", {"name": "D"})

    def test_filters_search_order_paging(self) -> None:
        for i, (name, score, tag) in enumerate([("Alpha", 10, "erp"), ("Beta", 50, "erp"), ("Gamma", 90, "cloud")]):
            self.store.insert(self.ctx, "companies", {"name": name, "account_score": score, "tags": [tag],
                                                      "industry": "Manufacturing" if i < 2 else None})
        page = self.store.list(self.ctx, "companies", {"account_score__gte": 50}, order="-account_score")
        self.assertEqual([r["name"] for r in page.rows], ["Gamma", "Beta"])
        self.assertEqual(self.store.count(self.ctx, "companies", {"tags": "erp"}), 2)
        self.assertEqual(self.store.count(self.ctx, "companies", {"q": "amm"}), 1)
        self.assertEqual(self.store.count(self.ctx, "companies", {"industry__isnull": True}), 1)
        self.assertEqual(self.store.count(self.ctx, "companies", {"name__in": ["Alpha", "Gamma"]}), 2)
        page = self.store.list(self.ctx, "companies", order="name", limit=2, offset=1)
        self.assertEqual([r["name"] for r in page.rows], ["Beta", "Gamma"])
        self.assertEqual(page.total, 3)
        self.assertEqual(self.store.group_count(self.ctx, "companies", "tags"), {"erp": 2, "cloud": 1})
        with self.assertRaises(ValidationError):
            self.store.list(self.ctx, "companies", {"password": "x"})
        with self.assertRaises(ValidationError):
            self.store.list(self.ctx, "companies", order="name; drop table x")

    def test_workspace_isolation(self) -> None:
        row = self.store.insert(self.ctx, "companies", {"name": "Secret Co"})
        self.assertIsNone(self.store.find(self.other_ctx, "companies", row["id"]))
        self.assertEqual(self.store.count(self.other_ctx, "companies"), 0)
        with self.assertRaises(NotFoundError):
            self.store.update(self.other_ctx, "companies", row["id"], {"name": "pwned"})
        with self.assertRaises(NotFoundError):
            self.store.delete(self.other_ctx, "companies", row["id"])
        # claiming another workspace's id in the context does not help a non-member
        forged = Ctx(self.ws["id"], self.other, "owner")
        self.assertEqual(self.store.count(forged, "companies"), 0)
        with self.assertRaises((NotFoundError, ForbiddenError)):
            self.store.insert(forged, "companies", {"name": "x"})

    def test_viewer_is_read_only(self) -> None:
        viewer = str(uuid.uuid4())
        self.store.add_member(self.ctx, viewer, "viewer")
        row = self.store.insert(self.ctx, "companies", {"name": "Foo"})
        vctx = Ctx(self.ws["id"], viewer, "viewer")
        self.assertEqual(self.store.get(vctx, "companies", row["id"])["name"], "Foo")
        with self.assertRaises(ForbiddenError):
            self.store.insert(vctx, "companies", {"name": "Bar"})
        # even if the caller claims to be a member, the stored role decides
        with self.assertRaises(ForbiddenError):
            self.store.insert(Ctx(self.ws["id"], viewer, "member"), "companies", {"name": "Bar"})

    def test_append_only_and_system_write(self) -> None:
        entry = self.store.insert(self.ctx, "audit_log", {"action": "x", "actor_kind": "user"})
        with self.assertRaises(ValidationError):
            self.store.update(self.ctx, "audit_log", entry["id"], {"action": "y"})
        with self.assertRaises(ValidationError):
            self.store.insert(self.ctx, "credit_ledger", {"provider": "zoominfo", "entry_type": "grant",
                                                          "amount": 5, "reason": "forged"})
        system = Ctx.for_system(self.ws["id"])
        self.store.insert(system, "credit_ledger", {"provider": "zoominfo", "entry_type": "grant", "amount": 5,
                                                    "reason": "sync"})
        self.assertEqual(self.store.count(self.ctx, "credit_ledger"), 1)

    def test_workspaces_and_members(self) -> None:
        names = [w["name"] for w in self.store.workspaces_for(self.owner)]
        self.assertEqual(names, ["Acme"])
        self.assertIsNone(self.store.membership(self.other, self.ws["id"]))
        self.assertEqual(self.store.membership(self.owner, self.ws["id"])["role"], "owner")
        with self.assertRaises((ForbiddenError, NotFoundError)):
            self.store.add_member(Ctx(self.ws["id"], self.other, "owner"), self.other, "admin")
        self.store.update_workspace(self.ctx, ai_external_allowed=True)
        self.assertTrue(self.store.membership(self.owner, self.ws["id"])["ai_external_allowed"])
        self.assertIn(self.ws["id"], self.store.list_workspace_ids())


class TestMemoryStore(StoreContract, unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        super().setUp()


class TestPostgresStore(StoreContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from cloud.intel.store.postgres import PostgresStore

        cls.url = fresh_database()
        cls.pg = PostgresStore.from_url(cls.url, max_size=4)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.pg.close()
        drop_database(cls.url)

    def setUp(self) -> None:
        self.store = self.pg
        super().setUp()

    def _as_user(self, user_id: str, statement: str, params=()):
        import psycopg

        with psycopg.connect(self.url) as conn:
            with conn.transaction():
                conn.execute("select set_config('request.jwt.claims', %s, true)",
                             [json.dumps({"sub": user_id, "role": "authenticated"})])
                conn.execute("set local role authenticated")
                return conn.execute(statement, params).fetchall()

    def test_rls_blocks_raw_sql_across_workspaces(self) -> None:
        import psycopg

        self.store.insert(self.ctx, "companies", {"name": "Hidden"})
        # no workspace filter at all: RLS alone must hide the row
        rows = self._as_user(self.other, "select name from careercloud.companies")
        self.assertEqual(rows, [])
        with self.assertRaises(psycopg.Error):
            self._as_user(self.other,
                          "insert into careercloud.companies (id, workspace_id, name, created_by) "
                          "values (%s, %s, 'x', %s) returning id",
                          (f"co_{uuid.uuid4().hex}", self.ws["id"], self.other))

    def test_rls_blocks_forged_created_by_and_system_tables(self) -> None:
        import psycopg

        with self.assertRaises(psycopg.Error):
            self._as_user(self.owner,
                          "insert into careercloud.companies (id, workspace_id, name, created_by) "
                          "values (%s, %s, 'x', %s) returning id",
                          (f"co_{uuid.uuid4().hex}", self.ws["id"], self.other))
        with self.assertRaises(psycopg.Error):
            self._as_user(self.owner,
                          "insert into careercloud.platform_tasks (id, workspace_id, kind, created_by) "
                          "values (%s, %s, 'crawl', %s) returning id",
                          (f"tsk_{uuid.uuid4().hex}", self.ws["id"], self.owner))

    def test_identity_is_immutable_even_for_the_owner_role(self) -> None:
        import psycopg

        row = self.store.insert(self.ctx, "companies", {"name": "Fixed"})
        with self.assertRaises(psycopg.Error):
            with psycopg.connect(self.url) as conn:
                conn.execute("update careercloud.companies set workspace_id = %s where id = %s",
                             [self.other_ws["id"], row["id"]])


class TestGeneratedMigration(unittest.TestCase):
    def test_committed_migrations_match_the_specs(self) -> None:
        from cloud.intel.store.ddl import GENERATED, generate

        for version, path in GENERATED.items():
            self.assertEqual(path.read_text(encoding="utf-8"), generate(version),
                             f"{path.name} drifted; run: python -m cloud.intel.store.ddl --write")

    def test_applied_migration_0003_never_changes(self) -> None:
        # 0003 may already be applied somewhere; later entities must go to later migrations.
        from cloud.intel.store.spec import ENTITIES

        self.assertEqual(sum(1 for s in ENTITIES.values() if s.migration == "0003"), 47)

    def test_every_table_has_rls(self) -> None:
        from cloud.intel.store.ddl import GENERATED, generate
        from cloud.intel.store.spec import ENTITIES

        sql = "".join(generate(version) for version in GENERATED)
        for spec in ENTITIES.values():
            self.assertIn(f"alter table careercloud.{spec.table} enable row level security;", sql)
            self.assertIn(f"create policy {spec.table}_select", sql)


if __name__ == "__main__":
    unittest.main()
