import sys, psycopg
url = next(l.split("=",1)[1].strip().strip('"') for l in open(sys.argv[1], encoding="utf-8") if l.startswith("CAREERCLOUD_DATABASE_URL="))
with psycopg.connect(url) as conn:
    cur = conn.cursor(); cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("select source_name, schedule, enabled, next_run_at, next_full_sweep_at, last_run_at, last_run_status, next_lifecycle_at, last_lifecycle_at, stale_after_days, visible_window_days from careercloud.job_source_monitors"); print(cur.fetchall())
    cur.execute("select mode, trigger, status, started_at, finished_at, pages, found, new_count, changed_count, stop_reason from careercloud.job_monitor_runs order by created_at"); [print(r) for r in cur.fetchall()]
    conn.rollback()
