"""The PostgreSQL / Supabase implementation of :class:`JobRepository`.

**Two ways in.** A user-scoped call (``owner_id`` given) runs in a transaction
that sets the request's JWT claims and switches to the ``authenticated`` role —
exactly what Supabase's own Data API does — so row-level security applies to
every statement. The queries also filter on ``owner_id`` explicitly; RLS is the
second lock, not the only one. A system-scoped call (``owner_id=None``, the
worker's) runs as the connecting role, which owns the tables and is therefore
not subject to RLS.

**Every write is conditional.** Status, worker and attempt conditions live in
the ``WHERE`` clause of a single ``UPDATE ... RETURNING``, so a lost race writes
nothing and returns ``None``. The ``guard_job_update`` trigger additionally
refuses illegal status moves, whatever the caller.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from cloud.shared.models import (
    CompanyTarget,
    Job,
    JobEvent,
    JobProgress,
    JobStatus,
    JobType,
    ResultFile,
    ResultKind,
    TargetRecord,
    TargetStatus,
)
from cloud.shared.repository import (
    MUTABLE_JOB_FIELDS,
    DuplicateJobError,
    JobRepository,
    StatusFilter,
    _statuses,
)

__all__ = ["PostgresJobRepository"]

_PROGRESS_COLUMNS = {
    "completed": "completed_companies",
    "total": "total_companies",
    "failed": "failed_companies",
    "jobs_found": "jobs_found",
    "current_company": "current_company",
    "current_phase": "current_phase",
    "message": "progress_message",
}
_DIRECT_COLUMNS = MUTABLE_JOB_FIELDS - {"progress", "status", "updated_at"}
_TARGET_COLUMNS = frozenset(
    {"status", "platform", "outcome", "jobs_found", "error", "started_at", "completed_at"}
)
_LIST_TARGET_PREVIEW = 10


def _limit_text(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


class PostgresJobRepository(JobRepository):
    """Jobs in PostgreSQL.

    Args:
        pool: A ``psycopg_pool.ConnectionPool``. The repository does not own it
            unless created through :meth:`from_url`.
        user_role: The role user-scoped calls switch to. ``authenticated`` on
            Supabase and in the local compatibility schema.
    """

    name = "postgres"

    def __init__(self, pool: Any, *, user_role: str = "authenticated", owns_pool: bool = False) -> None:
        self._pool = pool
        self._user_role = user_role
        self._owns_pool = owns_pool

    @classmethod
    def from_url(
        cls, url: str, *, min_size: int = 1, max_size: int = 10, user_role: str = "authenticated"
    ) -> "PostgresJobRepository":
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
            name="careercloud",
        )
        return cls(pool, user_role=user_role, owns_pool=True)

    # --- plumbing ------------------------------------------------------------

    @contextmanager
    def _tx(self, owner_id: Optional[str]) -> Iterator[Any]:
        from psycopg import sql
        from psycopg.rows import dict_row

        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            with conn.transaction():
                if owner_id is not None:
                    subject = str(uuid.UUID(owner_id))  # refuses anything that is not a user id
                    conn.execute(
                        "select set_config('request.jwt.claims', %s, true)",
                        [json.dumps({"sub": subject, "role": "authenticated"})],
                    )
                    conn.execute(sql.SQL("set local role {}").format(sql.Identifier(self._user_role)))
                yield conn

    @staticmethod
    def _job_from_row(row: Mapping[str, Any], targets: Sequence[CompanyTarget]) -> Job:
        return Job(
            job_id=row["id"],
            owner_id=str(row["owner_id"]),
            type=JobType(row["type"]),
            status=JobStatus(row["status"]),
            targets=list(targets),
            target_count=row["target_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            progress=JobProgress(
                completed=row["completed_companies"],
                total=row["total_companies"],
                failed=row["failed_companies"],
                jobs_found=row["jobs_found"],
                current_company=row["current_company"],
                current_phase=row["current_phase"],
                message=row["progress_message"],
            ),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            worker_id=row["worker_id"],
            heartbeat_at=row["heartbeat_at"],
            lease_expires_at=row["lease_expires_at"],
            cancel_requested_at=row["cancel_requested_at"],
        )

    def _targets_for(
        self, conn: Any, job_ids: Sequence[str], *, preview: Optional[int] = None
    ) -> Dict[str, List[CompanyTarget]]:
        if not job_ids:
            return {}
        query = "select job_id, position, website, company_name from careercloud.crawl_targets where job_id = any(%s)"
        params: List[Any] = [list(job_ids)]
        if preview is not None:
            query += " and position < %s"
            params.append(preview)
        query += " order by job_id, position"
        grouped: Dict[str, List[CompanyTarget]] = {job_id: [] for job_id in job_ids}
        for row in conn.execute(query, params).fetchall():
            grouped[row["job_id"]].append(
                CompanyTarget(website=row["website"], company_name=row["company_name"])
            )
        return grouped

    def _one_job(self, conn: Any, row: Optional[Mapping[str, Any]]) -> Optional[Job]:
        if row is None:
            return None
        targets = self._targets_for(conn, [row["id"]])[row["id"]]
        return self._job_from_row(row, targets)

    # --- jobs ----------------------------------------------------------------

    def add(self, job: Job, *, owner_id: Optional[str] = None) -> None:
        import psycopg

        if job.owner_id is None:
            raise ValueError("a stored job must have an owner")
        if owner_id is not None and job.owner_id != owner_id:
            raise PermissionError("a user may only create their own jobs")
        progress = job.progress
        try:
            with self._tx(owner_id) as conn:
                conn.execute(
                    """
                    insert into careercloud.jobs (
                      id, owner_id, type, status, target_count, created_at, updated_at,
                      total_companies, completed_companies, failed_companies, jobs_found,
                      current_company, current_phase, progress_message, max_attempts
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        job.job_id,
                        job.owner_id,
                        job.type.value,
                        job.status.value,
                        len(job.targets) if job.target_count is None else job.target_count,
                        job.created_at,
                        job.updated_at or job.created_at,
                        progress.total,
                        progress.completed,
                        progress.failed,
                        progress.jobs_found,
                        progress.current_company,
                        progress.current_phase,
                        _limit_text(progress.message, 500),
                        job.max_attempts,
                    ],
                )
                if job.targets:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            "insert into careercloud.crawl_targets (job_id, position, website, company_name)"
                            " values (%s, %s, %s, %s)",
                            [
                                (job.job_id, index, target.website, target.company_name)
                                for index, target in enumerate(job.targets)
                            ],
                        )
        except psycopg.errors.UniqueViolation as error:
            raise DuplicateJobError(job.job_id) from error

    def get(self, job_id: str, *, owner_id: Optional[str] = None) -> Optional[Job]:
        with self._tx(owner_id) as conn:
            row = conn.execute(
                "select * from careercloud.jobs where id = %s"
                + (" and owner_id = %s" if owner_id is not None else ""),
                [job_id] + ([owner_id] if owner_id is not None else []),
            ).fetchone()
            return self._one_job(conn, row)

    def _where(self, status: Optional[JobStatus], owner_id: Optional[str]) -> tuple:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = %s")
            params.append(status.value)
        if owner_id is not None:
            clauses.append("owner_id = %s")
            params.append(owner_id)
        return (" where " + " and ".join(clauses)) if clauses else "", params

    def list(
        self,
        *,
        status: Optional[JobStatus] = None,
        limit: int = 50,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> List[Job]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must not be negative")
        where, params = self._where(status, owner_id)
        with self._tx(owner_id) as conn:
            rows = conn.execute(
                f"select * from careercloud.jobs{where} order by created_at desc, id desc limit %s offset %s",
                params + [limit, offset],
            ).fetchall()
            targets = self._targets_for(conn, [row["id"] for row in rows], preview=_LIST_TARGET_PREVIEW)
            return [self._job_from_row(row, targets[row["id"]]) for row in rows]

    def count(self, *, status: Optional[JobStatus] = None, owner_id: Optional[str] = None) -> int:
        where, params = self._where(status, owner_id)
        with self._tx(owner_id) as conn:
            return int(conn.execute(f"select count(*) as n from careercloud.jobs{where}", params).fetchone()["n"])

    def count_by_status(self, *, owner_id: Optional[str] = None) -> Dict[JobStatus, int]:
        where, params = self._where(None, owner_id)
        with self._tx(owner_id) as conn:
            rows = conn.execute(
                f"select status, count(*) as n from careercloud.jobs{where} group by status", params
            ).fetchall()
        tally = {row["status"]: int(row["n"]) for row in rows}
        return {status: tally.get(status.value, 0) for status in JobStatus}

    def update_where(
        self,
        job_id: str,
        changes: Mapping[str, Any],
        *,
        expected_status: StatusFilter,
        owner_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        attempts: Optional[int] = None,
    ) -> Optional[Job]:
        unknown = set(changes) - MUTABLE_JOB_FIELDS
        if unknown:
            raise ValueError(f"cannot change {sorted(unknown)}")

        assignments: List[str] = ["updated_at = now()"]
        params: List[Any] = []
        for name, value in changes.items():
            if name == "updated_at":
                continue
            if name == "progress":
                progress: JobProgress = value
                for field, column in _PROGRESS_COLUMNS.items():
                    assignments.append(f"{column} = %s")
                    item = getattr(progress, field)
                    if field == "message":
                        item = _limit_text(item, 500)
                    elif field == "current_company":
                        item = _limit_text(item, 300)
                    params.append(item)
            elif name == "status":
                assignments.append("status = %s")
                params.append(JobStatus(value).value)
            elif name == "error":
                assignments.append("error = %s")
                params.append(_limit_text(value, 4000))
            elif name in _DIRECT_COLUMNS:
                assignments.append(f"{name} = %s")
                params.append(value)

        conditions = ["id = %s", "status = any(%s)"]
        params.extend([job_id, [status.value for status in _statuses(expected_status)]])
        if owner_id is not None:
            conditions.append("owner_id = %s")
            params.append(owner_id)
        if worker_id is not None:
            conditions.append("worker_id = %s")
            params.append(worker_id)
        if attempts is not None:
            conditions.append("attempts = %s")
            params.append(attempts)

        with self._tx(owner_id) as conn:
            row = conn.execute(
                f"update careercloud.jobs set {', '.join(assignments)} where {' and '.join(conditions)} returning *",
                params,
            ).fetchone()
            return self._one_job(conn, row)

    # --- worker --------------------------------------------------------------

    def claim(self, job_id: str, *, worker_id: str, lease_seconds: float) -> Optional[Job]:
        with self._tx(None) as conn:
            row = conn.execute(
                """
                update careercloud.jobs
                   set status = 'running',
                       attempts = attempts + 1,
                       worker_id = %s,
                       started_at = coalesce(started_at, now()),
                       completed_at = null,
                       heartbeat_at = now(),
                       lease_expires_at = now() + make_interval(secs => %s)
                 where id = %s
                   and status = 'queued'
                   and cancel_requested_at is null
                   and attempts < max_attempts
                returning *
                """,
                [worker_id, float(lease_seconds), job_id],
            ).fetchone()
            return self._one_job(conn, row)

    def heartbeat(
        self, job_id: str, *, worker_id: str, attempts: int, lease_seconds: float
    ) -> Optional[Job]:
        with self._tx(None) as conn:
            row = conn.execute(
                """
                update careercloud.jobs
                   set heartbeat_at = now(),
                       lease_expires_at = now() + make_interval(secs => %s)
                 where id = %s and status = 'running' and worker_id = %s and attempts = %s
                returning *
                """,
                [float(lease_seconds), job_id, worker_id, attempts],
            ).fetchone()
            return self._one_job(conn, row)

    def find_stale(self, *, limit: int = 100) -> List[Job]:
        with self._tx(None) as conn:
            rows = conn.execute(
                "select * from careercloud.jobs where status = 'running' and lease_expires_at < now()"
                " order by lease_expires_at limit %s",
                [limit],
            ).fetchall()
            targets = self._targets_for(conn, [row["id"] for row in rows], preview=_LIST_TARGET_PREVIEW)
            return [self._job_from_row(row, targets[row["id"]]) for row in rows]

    def find_orphaned(self, *, older_than_seconds: float, limit: int = 100) -> List[Job]:
        with self._tx(None) as conn:
            rows = conn.execute(
                "select * from careercloud.jobs where status = 'queued'"
                " and updated_at < now() - make_interval(secs => %s) order by created_at limit %s",
                [float(older_than_seconds), limit],
            ).fetchall()
            targets = self._targets_for(conn, [row["id"] for row in rows], preview=_LIST_TARGET_PREVIEW)
            return [self._job_from_row(row, targets[row["id"]]) for row in rows]

    # --- targets -------------------------------------------------------------

    def list_targets(self, job_id: str, *, owner_id: Optional[str] = None) -> List[TargetRecord]:
        owner_clause = " and j.owner_id = %s" if owner_id is not None else ""
        with self._tx(owner_id) as conn:
            rows = conn.execute(
                "select t.* from careercloud.crawl_targets t join careercloud.jobs j on j.id = t.job_id"
                f" where t.job_id = %s{owner_clause} order by t.position",
                [job_id] + ([owner_id] if owner_id is not None else []),
            ).fetchall()
        return [
            TargetRecord(
                job_id=row["job_id"],
                position=row["position"],
                website=row["website"],
                company_name=row["company_name"],
                status=TargetStatus(row["status"]),
                platform=row["platform"],
                outcome=row["outcome"],
                jobs_found=row["jobs_found"],
                error=row["error"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
            for row in rows
        ]

    def update_target(self, job_id: str, position: int, changes: Mapping[str, Any]) -> None:
        unknown = set(changes) - _TARGET_COLUMNS
        if unknown:
            raise ValueError(f"cannot change target fields {sorted(unknown)}")
        if not changes:
            return
        assignments, params = [], []
        for name, value in changes.items():
            assignments.append(f"{name} = %s")
            if isinstance(value, TargetStatus):
                value = value.value
            elif name == "error":
                value = _limit_text(value, 2000)
            elif name in ("platform", "outcome"):
                value = _limit_text(value, 100)
            params.append(value)
        with self._tx(None) as conn:
            conn.execute(
                f"update careercloud.crawl_targets set {', '.join(assignments)} where job_id = %s and position = %s",
                params + [job_id, position],
            )

    # --- events --------------------------------------------------------------

    def add_event(
        self,
        job_id: str,
        kind: str,
        *,
        message: Optional[str] = None,
        attempt: Optional[int] = None,
        data: Optional[Mapping[str, Any]] = None,
        owner_id: Optional[str] = None,
    ) -> None:
        with self._tx(owner_id) as conn:
            conn.execute(
                "insert into careercloud.job_events (job_id, kind, attempt, message, data)"
                " values (%s, %s, %s, %s, %s)",
                [job_id, kind, attempt, _limit_text(message, 2000), json.dumps(dict(data or {}), default=str)],
            )

    def list_events(
        self, job_id: str, *, owner_id: Optional[str] = None, limit: int = 200
    ) -> List[JobEvent]:
        owner_clause = " and j.owner_id = %s" if owner_id is not None else ""
        with self._tx(owner_id) as conn:
            rows = conn.execute(
                "select * from (select e.* from careercloud.job_events e"
                " join careercloud.jobs j on j.id = e.job_id"
                f" where e.job_id = %s{owner_clause} order by e.id desc limit %s) recent order by id",
                [job_id] + ([owner_id] if owner_id is not None else []) + [limit],
            ).fetchall()
        return [
            JobEvent(
                event_id=row["id"],
                job_id=row["job_id"],
                kind=row["kind"],
                created_at=row["created_at"],
                attempt=row["attempt"],
                message=row["message"],
                data=row["data"] or {},
            )
            for row in rows
        ]

    # --- results -------------------------------------------------------------

    @staticmethod
    def _result_from_row(row: Mapping[str, Any]) -> ResultFile:
        return ResultFile(
            result_id=row["id"],
            job_id=row["job_id"],
            owner_id=str(row["owner_id"]),
            kind=ResultKind(row["kind"]),
            filename=row["filename"],
            content_type=row["content_type"],
            storage_key=row["storage_key"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            row_count=row["row_count"],
            created_at=row["created_at"],
        )

    def upsert_result(self, result: ResultFile) -> ResultFile:
        with self._tx(None) as conn:
            row = conn.execute(
                """
                insert into careercloud.job_results
                  (id, job_id, owner_id, kind, filename, content_type, storage_key, size_bytes, sha256, row_count, created_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (job_id, kind) do update set
                  filename = excluded.filename,
                  content_type = excluded.content_type,
                  storage_key = excluded.storage_key,
                  size_bytes = excluded.size_bytes,
                  sha256 = excluded.sha256,
                  row_count = excluded.row_count,
                  created_at = excluded.created_at
                returning *
                """,
                [
                    result.result_id,
                    result.job_id,
                    result.owner_id,
                    result.kind.value,
                    result.filename,
                    result.content_type,
                    result.storage_key,
                    result.size_bytes,
                    result.sha256,
                    result.row_count,
                    result.created_at,
                ],
            ).fetchone()
        return self._result_from_row(row)

    def list_results(self, job_id: str, *, owner_id: Optional[str] = None) -> List[ResultFile]:
        with self._tx(owner_id) as conn:
            rows = conn.execute(
                "select * from careercloud.job_results where job_id = %s"
                + (" and owner_id = %s" if owner_id is not None else "")
                + " order by kind",
                [job_id] + ([owner_id] if owner_id is not None else []),
            ).fetchall()
        return [self._result_from_row(row) for row in rows]

    def get_result(
        self, job_id: str, result_id: str, *, owner_id: Optional[str] = None
    ) -> Optional[ResultFile]:
        with self._tx(owner_id) as conn:
            row = conn.execute(
                "select * from careercloud.job_results where job_id = %s and id = %s"
                + (" and owner_id = %s" if owner_id is not None else ""),
                [job_id, result_id] + ([owner_id] if owner_id is not None else []),
            ).fetchone()
        return None if row is None else self._result_from_row(row)

    # --- lifecycle -----------------------------------------------------------

    def ping(self) -> None:
        with self._tx(None) as conn:
            conn.execute("select 1")

    def close(self) -> None:
        if self._owns_pool:
            self._pool.close()

