# CareerCrawler demo (presentation mode)

A standalone page for showing CareerCrawler to someone. It is plain HTML, CSS
and JavaScript. It does not need Python, Node, a database, an API, Redis,
Supabase or a login, and it makes no network requests.

## Run it

- **Windows:** double-click `run-demo.bat`. It opens `index.html` in your default browser.
- **Any OS:** open `index.html` in a browser.
- **From a terminal (Windows):** `start cloud\demo\index.html`

## What it shows

- Hero and crawl box. Type a company website and press **RUN CRAWL**. Separate
  several sites with commas to crawl a batch, or click "load a sample batch".
- The live crawl goes through five stages: discovering the career page,
  detecting the ATS, discovering jobs, normalizing jobs, and completion. It
  shows companies, jobs discovered, the current company, the ATS detected and
  progress as a percentage.
- Dashboard cards (Companies Crawled, Jobs Found, Running Jobs, Success Rate),
  a Recent Crawls table, and a detail panel. Click any row to see its details.
- How CareerCrawler Works, supported platforms, and scale figures.

## Everything here is simulated

- The crawl is a timed animation. Company names, ATS platforms, job counts and
  sample roles come from a hash of the domain you type, so the same domain
  always produces the same result. `example.com` always returns Example
  Corporation, Workday and 247 jobs.
- The scale figures (12,377 companies, 359K+ jobs ledger, 60+ adapters) are
  labelled on the page as demo/sample metrics.
- External links are disabled on the page.

## Isolation

This folder is self-contained. Nothing in it imports or calls the crawler
engine, `state/crawler.db`, Google Sheets, Seamless, or the CareerCloud API,
worker or web app (`cloud/api`, `cloud/worker`, `cloud/web`). It reuses the
CareerCloud colour tokens and logo only by copying them. You can delete the
folder without affecting anything else.

## Files

```
cloud/demo/
  index.html        page markup
  assets/demo.css   styles (light and dark mode, responsive)
  assets/demo.js    simulated crawl and dashboard
  assets/favicon.svg
  run-demo.bat      double-click launcher for Windows
  README.md
```
