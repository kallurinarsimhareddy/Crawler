"""Read-only: task/run timeline (ids truncated, no payloads)."""
import sys, psycopg
url = next(l.split("=",1)[1].strip().strip('"') for l in open(sys.argv[1], encoding="utf-8") if l.startswith("CAREERCLOUD_DATABASE_URL="))
with psycopg.connect(url) as conn:
    cur = conn.cursor(); cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("select column_name from information_schema.columns where table_schema='careercloud' and table_name='job_monitor_runs'"); print([r[0] for r in cur.fetchall()])
    cur.execute("select kind,status,created_at,updated_at from careercloud.platform_tasks order by created_at"); [print(r) for r in cur.fetchall()]
    cur.execute("select column_name from information_schema.columns where table_schema='careercloud' and table_name='job_source_monitors'"); print([r[0] for r in cur.fetchall()])
    cur.execute("select created_at::date, count(*) from careercloud.job_posting_changes group by 1 order by 1"); print(cur.fetchall())
    cur.execute("select status, count(*) from careercloud.job_postings group by 1"); print(cur.fetchall())
    conn.rollback()
