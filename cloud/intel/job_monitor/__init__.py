"""Continuous job intelligence: source monitors over the master ``job_postings`` table.

* :mod:`.schema` — the 14 mandatory job fields, normalisation, the stable content hash.
* :mod:`.profiles` — deterministic site profiles (WeAreDevelopers first), found by host.
* :mod:`.strategies` — how a monitor reads one listing page (site profile, or the AI
  Scraper's deterministic page extractor for any other site).
* :mod:`.diff` — NEW / CHANGED / UNCHANGED / REOPENED classification.
* :mod:`.service` — monitors, runs, the scheduler tick, job queries, company links.
* :mod:`.runner` — the resumable ``job_monitor`` worker task (incremental + full sweep).
* :mod:`.importer` — historical CSV/XLSX import (the baseline snapshot).

Everything goes through :class:`cloud.intel.core.http.SafeFetcher` (SSRF checks,
robots.txt, pacing, honest User-Agent). Nothing logs in, solves a CAPTCHA, or
works around an access control; a blocked page ends the run as *partial*, and a
partial run never closes a job.
"""
