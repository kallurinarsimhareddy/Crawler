# SANA GTM: crawler cleanup, phase 1 (implementation record)

| | |
|---|---|
| Date | 2026-10-14 |
| Worktree / branch | `E:\Crawlers\CareerCrawler-sana-safety`, `fix/production-safety` |
| Base commit | `5ee805d` (= `origin/feature/careercrawler-platform` before this work) |
| Input | `docs/self-hosting-audit/CRAWLER_AND_TOOL_AUDIT.md` |
| Production | **Not touched.** Production runs `0bdcee4` from `E:\Crawlers\CareerCrawler-platform`. No database, setting, schedule, credential or deployment was changed. No migration was written or run. No crawler or bulk job ran |

**Labels.** **CONFIRMED** = proven by a test, a measurement or code. **ASSUMPTION** = reasoned but not proven.

---

## 1. What was done

| # | Audit item | Change | Status |
|---|---|---|---|
| 1 | D1: `careercrawler` external source always failed | `sources/service.py` resolves each `{name, website}` (or `{company_id}`) to a CRM company through `crm.upsert_company` (domain-first dedupe; an ambiguous match is reported as `needs_review`, never guessed), then submits `crawl` with `company_ids`, which the crawl task accepts | CONFIRMED by tests |
| 2 | D2: no egress guard on the platform worker | `intel/tasks/worker.py`: `install_platform_egress_guard()` runs before the platform is built. It reuses `cloud/worker/egress.py`: every urllib3 connection, first hop and every redirect, is refused if any resolved address is loopback, private (RFC 1918, ULA), link-local (incl. 169.254.169.254), CGNAT, multicast, reserved or unspecified; IPv4-mapped IPv6 is unwrapped; the connection goes to the exact address checked (no DNS rebinding). **On by default**; `CAREERCLOUD_EGRESS_GUARD=false` is refused in staging/production. Exempt: only the worker's own infrastructure (`CAREERCLOUD_S3_ENDPOINT`, a localhost `OPENAI_COMPATIBLE_BASE_URL`, `CAREERCLOUD_EGRESS_ALLOW_HOSTS`) | CONFIRMED by tests + Windows e2e |
| 3 | G1: two job write paths | `JobMonitorService.upsert_batch` is now the **only** writer of `job_postings`. `JobIntelService.ingest_postings` (crawl, external sources, scraper apply, `POST /jobs/ingest`) normalises with `normalize_job` and calls `upsert_batch(..., observed=True)`, then adds only its own extras (see §2) | CONFIRMED by tests (7 new tests fail on the old code) |
| 4 | Utility duplicates | Only the URL identity was consolidated (proven: `job_url_key == canonical_job_url` for every URL without a site profile). Formula-neutralise helpers were **not** merged: their trigger sets differ (`=+-@\t\r` vs `+\n` vs `=+-@`), so merging would change exported cells | CONFIRMED |
| 5 | Job-history growth | `JobMonitorService.prune_history()` (dry run by default) + opt-in daily worker pass (`CAREERCLOUD_JOB_HISTORY_RETENTION_DAYS`, **default 0 = off**). `job_detail` rebuilds pruned timeline entries from the job row. Tested offline on a restored copy of the production backup | CONFIRMED offline; **not enabled anywhere** |
| 6 | Unused code | Removed: `utils/browser_pool.py` (+ its test), `cloud/intel/vendor/it_classify.py`, `cloud/intel/vendor/zc_clean.py`, `TechnologyService.record_from_text` / `remove` / `run_provider` and the duplicate `TechnologyProvider` ABC in `technology/service.py` | CONFIRMED unused (static grep incl. strings, `__all__`, routes, agent tools, scripts, tests) |
| 7 | Dependencies | Self-host runtime: removed `pandas`, `numpy` (`deploy/selfhost/requirements.txt`) and `pandas`, `numpy`, `python-dateutil`, `six` (`deploy/windows-selfhost/requirements-lock.txt`). Declared `dnspython` in `cloud/api/requirements.txt` (the email engine imports it; it was declared only for self-host) | CONFIRMED: full platform suite in a clean venv built from the trimmed lock + Windows e2e |

## 2. The unified job write path

**One identity.** `url_key = job_url_key(url)`: the site profile's stable key where one exists (WeAreDevelopers numeric id), else `canonical_job_url`. Before, a WAD job with a slug got a different key on the crawl/source path and became a second row. Unique per workspace (`job_postings_uq_url_key`).

**One set of rules** (from `upsert_batch`, now applied to every source):
- NEW: inserted with `status=open`, `first_seen_at`/`last_seen_at`, `content_hash`, `observation_count=1`, keyword-set relevance (`relevance_class`, `matched_keywords`, `relevance_version`) and a `new` history row.
- Seen again: `last_seen_at` and `observation_count` move. `first_seen_at` never moves.
- CHANGED: only for the meaningful fields (title, company, location, experience, salary, keywords, remote). The fields are updated and one `changed` history row is written with the before/after values.
- REOPENED: a `closed` or `expired` job seen again becomes `open`. `reopened_at` is set, the closure fields are cleared, and a history row is written.
- `unknown` (import-only) becomes `open`. A **`stale` job stays `stale`**. Before this change, the crawl path forced every matched job back to `open`.

**Attribution.** The first source keeps `source_kind`/`source_name`/`source_monitor_id`. Before, every match overwrote them. A second source only fills blanks in its extra fields (ATS, external id, domain, country, department, employment type…). Campaign relevance (`is_relevant`, `campaign_keys`, `relevance_reasons`) is the workspace's and always updates.

**Extras kept from `ingest_postings`:**
- company link by explicit id or by domain (the dedupe service), passed into `upsert_batch` as `company_id`;
- campaign relevance and classifier fields;
- technology evidence and the `new_job` change event for new jobs, plus the `job_posted` and `long_open_job` automation events;
- a `new_job (reopened)` change event;
- `refresh_company`.

**Closing.** Only after a **successful** crawl of that company, only that source's own `open`/`stale` jobs. The job's `closed_at` is set, and a `closed` history row (reason `missing_from_board`) is now written as well. `closure_reason` stays empty: its allowed values (`missed_full_sweeps`, `source_gone`, `manual`) are a database check constraint, and adding one would need a migration (out of scope).

**Return value.** `ingest_postings` keeps its signature and statistics (`inserted`, `updated`, `reopened`, `closed`, `rejected`, `linked`, `unlinked`, `relevant`, `problems`), so callers are unchanged.

## 3. Job-history retention

**Measured offline** on a throwaway restore of `pre-selfhost_careercloud_20261014T082831Z.dump` (127.0.0.1, deleted afterwards), using the new code:

| Window | `new` matched | `stale` matched | Result |
|---|---|---|---|
| 90 days | 0 | 0 | nothing yet (rows are 8–12 days old) |
| 30 days | 0 | 0 | nothing yet |
| 7 days | 115,680 | 115,680 | deleted 231,360 rows in 2.5 s; after `VACUUM FULL`: table **128 MB → 56 kB**, database **354 MB → 226 MB** |

**Kept, always:** `changed`, `reopened`, `closed`, `expired` and gone evidence.

**Why `new`/`stale` are safe to prune:**
- A `new` row repeats `first_seen_at` and `first_seen_run_id`; a `stale` row repeats `stale_at`.
- The feed's change and date filters read job columns, not history.
- Run summaries count only rows from the last 24 hours or the current run.
- The job timeline is rebuilt (`synthesized: true`).

**Recommendation (needs approval):** set `CAREERCLOUD_JOB_HISTORY_RETENTION_DAYS=30` on the self-hosted worker after cutover, run `prune_history(..., dry_run=True)` first, and restore `stale_after_days` to its default of 30 (currently 1 in production). A 30-day window frees about 128 MB at the first pass after 2026-11-05. ASSUMPTION: growth continues at the measured rate.

## 4. Retained components (and why)

| Component | Why kept |
|---|---|
| Redis queue, `cloud/worker`, fakeredis, `redis` and `boto3` in `cloud/api/requirements.txt`, Pages proxy function, old installer, `deploy/windows/sana-gtm` | Instruction 8. They still serve the legacy crawl worker, the old installer, the Pages proxy and the current production health check (whose queue default is `redis`). Remove after cutover |
| `cloud/worker/egress.py` | Now also used by the platform worker. Moving it out of the legacy package is a follow-up |
| Vendored `vendor/seamless_credits.CreditBudget`, `seamless_targeting.validate_departments` | Dead code, but vendored "keep identical" files. Editing them breaks provenance for little gain |
| `utils/robots.py` | Unused, but a planned "v3 discovery" component of the live legacy crawler. Owner decision |
| `vendor/names.py` (copy of `utils/names.py`) | The duplication is deliberate: `test_isolation` forbids the platform importing the legacy crawler except through two modules |
| `pandas` in the root `requirements.txt` | The legacy crawler's `main.py` and Sheets import tools use it (`crawler/csv_reader.py`) |
| `httpx2`, `tzdata`, `psutil` in the Windows lock | `httpx2` is required by `anthropic`, `tzdata` by `psycopg`, `psutil` by `sanagtm selftest`. These were audit findings, corrected here |
| All legacy CareerCrawler, Seamless, ZoomInfo and Google Sheets production code | Unchanged. The production legacy crawler runs from its own checkout (`E:\Crawlers\CareerCrawler`) |

## 5. Unresolved risks

1. **Behaviour change on the crawl/source path (intended):**
   - a matched `stale` job stays `stale` instead of becoming `open`;
   - another source's values are no longer overwritten;
   - history rows now accumulate for these sources too, which section 3's retention controls.

   Production has no jobs from these paths yet (backup: 0 rows), so no existing data changes meaning.
2. **Two observations of one job by different sources** can still produce a `changed` row when their field values differ (for example engine-derived keywords vs a source's own tags). This is accepted canonical behaviour.
3. **The egress guard covers urllib3 only.** A Playwright browser fallback or raw sockets (SMTP preflight, DNS) are not covered; the firewall rule from the staging design is still the control for those. The browser fallback stays off by default.
4. **`closure_reason` is empty for crawler closures** (database constraint). The reason is recorded in the history row.
5. **Service mode and a reboot of the Windows package remain untested** (they need an administrator).

## 6. Proposed weekly CareerCrawler integration (not implemented, not scheduled)

**Goal.** The legacy weekly run (W41: 11,377 companies, 349,317 postings, 46,656 new, 49,991 closed) feeds SANA's `job_postings`, so hiring signals, accounts and campaigns see employer-ATS jobs and not only WeAreDevelopers.

**Approach: import, not re-crawl.** The weekly run keeps crawling exactly as today, with its own politeness, browser budget and run lock. After it finishes, SANA imports its result. No second crawler hits employer sites, so the integration adds **zero** external requests and no new rate-limit exposure.

| Topic | Proposal |
|---|---|
| **Export format** | One NDJSON file per run, written by a small exporter reading the legacy SQLite ledger (`state/crawler.db`), next to the existing Sheets write (no change to the crawl itself). One line per posting: `job_key` (legacy `job_identity`), `company_key` (`domain:acme.com`), `company_name`, `company_website`, `title`, `job_url`, `location`, `platform` (ATS), `requisition_id`, `first_seen`, `last_seen`, `status` (open/closed), `is_tech`, `run_id`. A header line carries `run_id`, the list of **companies read successfully** in the run, and counts. The file plus a SHA-256 manifest is stored under the self-host data folder |
| **Stable identifiers** | SANA key = `job_url_key(job_url)`. The legacy `job_key` goes to the empty `job_postings.external_id` column, so a posting whose URL changed (legacy `weekly_diff` re-links these) can be re-linked by `external_id` before inserting. Companies: `company_key`'s domain → CRM company via `crm.upsert_company` (domain-first; ambiguous → review queue) |
| **Source attribution** | `source_kind="crawler"`, `source_name="careercrawler-weekly"`, `source=<ATS platform>`. Distinct from the on-demand `careercrawler` crawl, so neither closes the other's jobs |
| **Deduplication** | `upsert_batch` (unique `(workspace_id, url_key)`, in-batch dedupe, ConflictError fallback). Cross-source: a URL already found by a monitor keeps its owner; the weekly import only fills blanks (§2) |
| **Incremental updates** | First load: the current **open tech** set (CURRENT_JOBS: 20,586 rows in W41) as an *import* (status UNKNOWN→OPEN on the next observation; no bulk `new` history rows). Each later run: the run's postings as an observation (`observed=True`) through `ingest_postings` with `crawled_company_ids` = companies read successfully, so closures follow the same rule as the legacy weekly diff (never close on a failed read) |
| **Rate limits / load** | No external requests. Database load: `upsert_batch` in 500-row batches; about 20k rows/week tech-only (measured import speed on local PostgreSQL ~1,300 rows/s → ~16 s). A full 349k-row non-tech import would take ~5 min/week, but see storage |
| **Storage** | Measured production density: `job_postings` ≈ 1.5 KB/row incl. indexes. Tech-only (~20.6k open) ≈ 31 MB; all postings (349k) ≈ 520 MB (ESTIMATE). **Recommend tech-only** (`is_tech`), matching what the Sheets users see in CURRENT_JOBS. Plus history retention (§3) |
| **Scheduling** | Later, after cutover and approval: a platform task kind (for example `careercrawler_import`) triggered by the presence of a new export file, run by the self-hosted worker, idempotent per `run_id` (task idempotency key). The legacy Windows task "CareerCrawler Weekly" stays as is |
| **Prerequisites** | Production on the self-hosted PostgreSQL (no 500 MB cap); keyword set covering the legacy IT_KEYWORDS tab; a decision on which Sheets tabs stay; a dry-run import on a restored backup |
| **Tests to write first** | Re-link by `external_id` when the URL changes; a failed company read closes nothing; repeat import of the same `run_id` is a no-op; tech-only filter; company resolution by domain; 20k-row timing on the restored backup |

## 7. Files changed

See section 8 of the final report (`git diff --stat`) for the exact list.

| File | Change |
|---|---|
| `cloud/intel/sources/service.py` | D1 fix (`_crawl_targets`) |
| `cloud/intel/tasks/worker.py` | egress guard installer; opt-in history retention in the daily pass |
| `cloud/intel/jobs/service.py` | `ingest_postings` delegates persistence to `upsert_batch` |
| `cloud/intel/job_monitor/service.py` | `upsert_batch(observed=…)`, explicit `company_id` linking, `prune_history`, `PRUNABLE_CHANGES`, timeline rebuild in `job_detail` |
| `cloud/intel/technology/service.py` | dead methods + duplicate ABC removed |
| `cloud/intel/README.md` | provenance table updated for the removed vendored files |
| `cloud/api/requirements.txt` | `dnspython` declared |
| `deploy/selfhost/requirements.txt`, `deploy/windows-selfhost/requirements-lock.txt` | pandas stack removed |
| removed | `utils/browser_pool.py`, `tests/test_browser_pool.py`, `cloud/intel/vendor/it_classify.py`, `cloud/intel/vendor/zc_clean.py` |
| tests | new `test_platform_egress.py`, `test_job_write_path.py`, `test_job_write_path_pg.py`; `test_platform_sources.py` (careercrawler tests on the real CRM); `test_isolation.py` (allow the new egress test's `utils.http` import) |
| docs | this file; `CRAWLER_AND_TOOL_AUDIT.md` (the audit, first committed here) |

## 8. Test results (all offline, 2026-10-14)

| Suite | Result |
|---|---|
| Platform `cloud/tests`, development venv | **2,002 run, 0 failed**, 18 skipped (all need a real Redis) |
| Platform `cloud/tests`, **clean venv built from the trimmed Windows lock** (no pandas/numpy/dateutil/six), external throwaway PostgreSQL 17 | 1,999 run. The only failure was `test_isolation` flagging the new egress test's `utils.http` import; it was allowlisted like the existing `test_egress.py` and then passed. 52 skipped, all optional packages the lock deliberately excludes (redis/fakeredis 51, Playwright 2, moto 1, real Redis 18; some overlap in counting) |
| CareerCrawler `tests` | **1,683 run, 0 failed** (13 fewer: the removed `test_browser_pool.py`). One run hit a known Windows flake in `test_runlock` (`tearDown` could not delete its temp folder while another process held a handle); the file is unchanged; 6/6 reruns and a full-suite rerun passed |
| Web `npm test` | 117 run, 0 failed |
| Windows package e2e, User mode, no admin, package built from these changes | **40/40** (install, app use, CORS, PostgreSQL-queue worker, backup, restore test, update/rollback, uninstall/reinstall, import with the same key). Package secret scan: 505 files vs 7 real secret values, 0 problems |
| Worker entry point | `python -m cloud.intel.tasks.worker` logs `egress guard installed`; with `CAREERCLOUD_ENV=production CAREERCLOUD_EGRESS_GUARD=false` it refuses to start |
| New tests | `test_platform_egress` 6, `test_job_write_path` 15, `test_job_write_path_pg` 2, careercrawler source 3. **7 of the 10 write-path tests fail on the old code** (identity, attribution/status, expired→reopened, history, relevance, duplicates) |
| Offline retention measurement | §3 (restored production backup, throwaway, deleted afterwards) |

**Not run:**
- Windows Service mode and reboot recovery (need an administrator).
- Anything against production.
- Live crawls.
