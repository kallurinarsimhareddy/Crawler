"""Boot-time check that a PostgreSQL database holds the platform schema.

The API and the platform worker call :func:`require_schema` before serving
anything. A database that was never migrated, or is behind the code, fails at
startup with the exact command to fix it — instead of the first request
failing with ``relation "careercloud.companies" does not exist``.

Checked:

* every migration file in ``cloud/db/migrations`` is recorded as applied, with
  the same checksum (an edited applied migration is refused);
* every table the code expects exists in schema ``careercloud`` — tenancy
  (``workspaces``, ``workspace_members``) plus one per entity spec — and has
  row-level security enabled.

It never creates, alters or migrates anything itself.
"""

from __future__ import annotations

from typing import Any, Dict, List

from cloud.intel.store.spec import ENTITIES

__all__ = ["SchemaError", "expected_tables", "require_schema", "schema_report"]

FIX = ("run `python -m cloud.devtools.localpg start` (local development) or "
       "`python -m cloud.db.migrate apply` against this database")


class SchemaError(RuntimeError):
    """The database is missing platform tables or migrations."""


def expected_tables() -> List[str]:
    return ["workspaces", "workspace_members"] + [spec.table for spec in ENTITIES.values()]


def schema_report(pool: Any) -> Dict[str, Any]:
    from cloud.db.migrate import load_migrations

    migrations = load_migrations()
    with pool.connection() as conn:
        row = conn.execute("select to_regclass('careercloud.schema_migrations') as t").fetchone()
        exists = (row["t"] if isinstance(row, dict) else row[0]) is not None
        applied: Dict[str, str] = {}
        if exists:
            for r in conn.execute("select version, checksum from careercloud.schema_migrations").fetchall():
                version, checksum = (r["version"], r["checksum"]) if isinstance(r, dict) else (r[0], r[1])
                applied[version] = checksum
        tables = {}
        for r in conn.execute(
                "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'careercloud' and c.relkind = 'r'").fetchall():
            name, rls = (r["relname"], r["relrowsecurity"]) if isinstance(r, dict) else (r[0], r[1])
            tables[name] = rls
    pending = [m.version for m in migrations if m.version not in applied]
    changed = [m.version for m in migrations if m.version in applied and applied[m.version] != m.checksum]
    missing = [t for t in expected_tables() if t not in tables]
    without_rls = [t for t in expected_tables() if t in tables and not tables[t]]
    return {"pending_migrations": pending, "changed_migrations": changed, "missing_tables": missing,
            "tables_without_rls": without_rls, "tables_verified": len(expected_tables()) - len(missing),
            "tables_expected": len(expected_tables())}


def require_schema(pool: Any) -> Dict[str, Any]:
    report = schema_report(pool)
    problems = []
    if report["pending_migrations"]:
        problems.append("migrations not applied: " + ", ".join(report["pending_migrations"]))
    if report["changed_migrations"]:
        problems.append("applied migrations were edited afterwards: " + ", ".join(report["changed_migrations"]))
    if report["missing_tables"]:
        problems.append(f"{len(report['missing_tables'])} platform tables missing (e.g. "
                        + ", ".join(report["missing_tables"][:5]) + ")")
    if report["tables_without_rls"]:
        problems.append("row-level security is off on: " + ", ".join(report["tables_without_rls"]))
    if problems:
        raise SchemaError("The platform database is not ready — " + "; ".join(problems) + f". To fix: {FIX}.")
    return report
