"""Read-only PostgreSQL size statistics (catalog metadata only; no row contents).
Usage: python db_stats.py <env-file> [out.json]"""
import json, sys, psycopg
url = None
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("CAREERCLOUD_DATABASE_URL="):
        url = line.split("=", 1)[1].strip().strip('"')
conn = psycopg.connect(url, autocommit=False)
cur = conn.cursor()
cur.execute("SET TRANSACTION READ ONLY")
cur.execute("SET LOCAL statement_timeout = '60s'")
out = {}
cur.execute("select version(), current_database(), pg_database_size(current_database())")
out["version"], out["db"], out["db_bytes"] = cur.fetchone()
cur.execute("""
select n.nspname, c.relname, c.reltuples::bigint,
       pg_table_size(c.oid), pg_indexes_size(c.oid), pg_total_relation_size(c.oid),
       coalesce(s.n_live_tup,0), coalesce(s.n_dead_tup,0)
from pg_class c join pg_namespace n on n.oid=c.relnamespace
left join pg_stat_user_tables s on s.relid=c.oid
where c.relkind in ('r','p','m') and n.nspname not in ('pg_catalog','information_schema','pg_toast')
order by pg_total_relation_size(c.oid) desc""")
out["tables"] = [dict(zip(["schema","table","reltuples","table_bytes","index_bytes","total_bytes","live","dead"], r)) for r in cur.fetchall()]
cur.execute("select n.nspname, sum(pg_total_relation_size(c.oid)) from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p','m') group by 1 order by 2 desc")
out["by_schema"] = cur.fetchall()
conn.rollback(); conn.close()
json.dump(out, open(sys.argv[2], "w") if len(sys.argv) > 2 else sys.stdout, indent=1, default=str)
