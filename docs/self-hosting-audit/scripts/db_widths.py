"""Read-only: planner column-width statistics (pg_stats) + table counts. No row contents."""
import sys, psycopg
url = next(l.split("=",1)[1].strip().strip('"') for l in open(sys.argv[1], encoding="utf-8") if l.startswith("CAREERCLOUD_DATABASE_URL="))
with psycopg.connect(url) as conn:
    cur = conn.cursor(); cur.execute("SET TRANSACTION READ ONLY")
    for t in sys.argv[2:]:
        cur.execute("select attname, avg_width, null_frac from pg_stats where schemaname='careercloud' and tablename=%s order by avg_width desc limit 8", (t,))
        print(t, [(a, w, round(n,2)) for a, w, n in cur.fetchall()])
    cur.execute("select count(*) from pg_tables where schemaname='careercloud'"); print("careercloud tables:", cur.fetchone()[0])
    cur.execute("select count(*), min(created_at)::date, max(created_at)::date from careercloud.job_postings"); print("job_postings count/min/max created:", cur.fetchone())
    cur.execute("select count(*), min(created_at)::date, max(created_at)::date from careercloud.job_posting_changes"); print("changes count/min/max:", cur.fetchone())
    cur.execute("select created_at::date d, count(*) from careercloud.job_postings group by 1 order by 1"); print("postings/day:", cur.fetchall())
    cur.execute("select kind, status, count(*) from careercloud.platform_tasks group by 1,2 order by 1"); print("tasks:", cur.fetchall())
    cur.execute("select setting from pg_settings where name in ('max_connections')"); print("max_conn", cur.fetchone())
    conn.rollback()
