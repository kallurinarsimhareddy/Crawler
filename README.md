# CareerCrawler v2

Extracts every publicly visible job posting — title, location, country and URL —
from a sheet of companies, whatever applicant tracking system each one uses.

Version 2 covers **58 platforms** with a dedicated adapter, falls back to
generic HTML extraction for the rest, and falls back again to a headless browser
for boards that only exist once JavaScript has run.

---

## Setup on a new machine

Requires **Python 3.10 or newer** (developed on 3.12).

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux

# 2. Install the Python dependencies
pip install -r requirements.txt

# 3. Install the browser used by the fallback path (~150 MB, one time)
python -m playwright install chromium
```

Step 3 is **optional but strongly recommended**. Without it the crawler still
runs and every adapter still works; it simply cannot read boards that render
client-side or sit behind an interstitial, and those companies are reported as
technical failures instead. Nothing crashes — there is a test that proves it.

### Verify the install

```bash
python -m unittest discover -s tests     # 397 tests, all offline
python main.py --preview                 # platform mix, no network calls
```

---

## Running a crawl

```bash
python main.py                           # full run over input/companies.csv
python main.py --limit 50                # first 50 companies, a smoke test
python main.py --workers 16              # more concurrency
python main.py --no-browser              # HTTP only, much faster, less coverage
python main.py --input other.csv         # a different sheet
python main.py --preview                 # what would be crawled, then exit
```

A full 1,216-company run takes roughly **25–30 minutes** with 16 workers and the
browser enabled, or about a third of that with `--no-browser`.

### Command-line options

| Flag | Default | What it does |
|---|---|---|
| `--input` | `input/companies.csv` | Source sheet |
| `--output-dir` | `output/` | Where every report is written |
| `--output` | `<output-dir>/jobs.xlsx` | Workbook path, overriding the directory |
| `--failures` | `<output-dir>/failed_companies.csv` | Failure CSV path |
| `--limit` | `0` (all) | Crawl only the first N companies |
| `--workers` | cores × 2, capped 4–16 | Companies crawled at once; `1` is sequential |
| `--per-host-delay` | `0.35` | Minimum seconds between two crawls of one host |
| `--retries` | `2` | HTTP attempts per request, including the first |
| `--no-browser` | off | Never fall back to headless Chromium |
| `--no-discover` | off | Do not search a company website for its careers page |
| `--no-diagnostics` | off | Do not write evidence dumps for unreadable boards |
| `--diagnostics-limit` | `60` | Ceiling on those dumps per run |
| `--log-level` | `WARNING` | Console verbosity; the log file is always DEBUG |
| `--log-file` | `<output-dir>/crawl.log` | Full log destination |
| `--preview` | off | List what would be crawled, then exit |

---

## Input

`input/companies.csv` needs three columns; a fourth is used when present.
Headers are matched loosely, so `Careers / Jobs URL` and `Careers URL` are both
understood.

| Column | Required | Purpose |
|---|---|---|
| `Company Name` | yes | Name written to every posting |
| `Website` | yes | Used for careers-page discovery when no board URL works |
| `Career Page URL` | yes | Usually a marketing page |
| `IT LINK` | no | A direct link to the ATS — tried first, and the best signal |

---

## Output

Everything lands in `--output-dir` (`output/` by default).

| File | Contents |
|---|---|
| `jobs.xlsx` | One row per posting: company, title, location, country, job URL, board URL, platform |
| `failed_companies.csv` | Every company that produced no jobs — the union of the four below |
| `no_open_jobs.csv` | Boards read successfully that are genuinely empty. Nothing to fix |
| `unsupported_platforms.csv` | Platform identified, no adapter yet. Fix: write the adapter |
| `technical_failures.csv` | Board could not be read — blocked, timed out, changed shape |
| `unknown_platforms.csv` | Nothing recognisable behind the URL, or no usable URL |
| `summary.json` | The whole run as data — totals, per-platform coverage, timings, grouped failure reasons |
| `crawl.log` | Full DEBUG log, rotated at 50 MB |
| `unknown_platforms/` | Evidence dumps for unreadable boards: rendered DOM, screenshot, captured API calls, JS bundles, and a `report.md` explaining how to use them |

If a destination is open in Excel — which locks it on Windows — the report is
written to a timestamped sibling rather than discarded. A crawl costs half an
hour; an open spreadsheet must not throw that away.

---

## How it decides what to do

```
read sheet → pick the best URL → detect the platform → adapter → Job records
```

**Seed selection** tries `IT LINK`, then `Career Page URL`, then `Website`, and
takes the first that resolves to a usable URL — so filler like `N/A` in one
column cannot mask a good value in the next.

**Extraction** is tried in order of how much the source tells us:

1. **The vendor's public API**, where one exists — Greenhouse, Lever, Ashby,
   Workday, Oracle, SmartRecruiters, Workable, Breezy, Rippling, Pinpoint,
   Personio, Manatal, Comeet, Eightfold, Cornerstone, Phenom, UKG Ready, Asure.
2. **Structured data** — JSON-LD and microdata `JobPosting`.
3. **The rendered listing** — job cards, tables, lists, accordions, and "Apply"
   links whose title sits in a nearby heading.
4. **Embedded JavaScript state** — `__NEXT_DATA__`, `__NUXT__`, Apollo caches,
   `window.__INITIAL_STATE__`, Inertia `data-page` attributes.
5. **A headless browser** — renders the page, clicks *Load more* until it stops
   appearing, scrolls an infinite list to its end, waits out self-clearing
   interstitials, and keeps every JSON response the page fetched. A board's own
   XHR is a better source than the DOM it produced, so it is mined first.

**Career discovery** kicks in when the sheet gives no usable board URL, and
again when a marketing page yields nothing: the company's own site is searched
for a careers link, and a link that leaves for a recognised ATS wins outright.

**Nothing stops a run.** Every company is attempted; a failure is recorded
against that company and the crawl moves on.

---

## Layout

```
main.py                 CLI entry point, run report, coverage table
config/settings.py      Run-wide knobs. Defaults are inert — main() opts in
crawler/
  csv_reader.py         Input sheet, with loose header matching
  platform_detector.py  URL → Platform. Pure, no I/O
  crawler_engine.py     Worker pool, dispatch, outcomes, failure isolation
  career_finder.py      Website → careers page
  diagnostics.py        Evidence dumps for unreadable boards
adapters/               One module per ATS, 57 of them, plus shared helpers
  _paginated_html.py    The shared crawler for server-rendered boards
  _ta_recruitment.py    Shared REST reader for UKG Ready and Asure
  generic.py            Fallback, and the shared card/location reasoning
exporters/              Workbook, failure CSV, the four outcome reports, summary
models/job.py           The Job record and the export column order
utils/
  http.py               Session with retry and backoff
  html.py               Parsing, structured data, URL resolution
  discovery.py          Job listings hidden in JavaScript state
  browser.py            Headless Chromium: render, intercept, scroll, load more
  location.py           Location parsing and country derivation
  jobs.py               Job assembly and deduplication
tests/                  397 offline tests
```

---

## Adding a new ATS

1. Add a member to `Platform` and a `_Rule` to `_RULES` in
   `crawler/platform_detector.py`.
2. Write `adapters/<name>.py` exposing `PLATFORM`, a `parse_*` function and
   `fetch_jobs(career_url, company_name, session=None)`. If the board is
   server-rendered HTML, `fetch_hosted_board` in `adapters/_paginated_html.py`
   is the whole implementation — supply the hosts and the posting-URL shape.
3. Add one line to `ADAPTER_MODULES` in `crawler/crawler_engine.py`.
4. Add a row to the table in `tests/test_adapters_v2.py`, and a host case in
   `tests/test_platform_detector.py` — a coverage guard there fails if a
   `Platform` member has no test.

Return `[]` for an empty board and raise for one that could not be read. That
distinction is what keeps "no openings" out of the failure reports.

If you do not know what a board serves, run the crawler once and read its
directory under `output/unknown_platforms/` — the captured network log usually
names the API to call.

---

## Known limitations

- **Some iCIMS tenants** sit behind an AWS WAF **CAPTCHA** — a puzzle intended
  for a human, not a self-clearing challenge. Every path, including RSS and
  JSON, returns the same gate. The crawler detects it, reports it honestly as a
  technical failure, and does not attempt to defeat it. Most affected companies
  are recovered anyway by falling through to their second URL; on the reference
  sheet 9 of 37 remain blocked. The supported routes for those are iCIMS's own
  job-feed integration, or running from an IP the gate does not challenge.
- **About a third of postings have a blank Country.** `utils/location.py`
  refuses to guess: a multi-location string such as `"3 Locations"` or
  `"CA (+8 more)"` yields nothing rather than something wrong.
- **Four legacy SAP SuccessFactors portals** publish no anonymous endpoint and
  are reported as needing a browser-driven adapter.
