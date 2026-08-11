"""Per-platform job extraction adapters.

Each module here handles exactly one applicant tracking system and exposes the
same three names:

``PLATFORM``
    The label written to the ``Platform`` column. It must be the ``value`` of a
    :class:`~crawler.platform_detector.Platform` member.
``parse_*``
    A pure function turning a URL from the input sheet into whatever the vendor
    is addressed by — a board token, a tenant, an origin. Raises
    :class:`~utils.http.AdapterUrlError` when the URL is not this vendor's.
``fetch_jobs(career_url, company_name, session=None)``
    The entry point. Returns ``List[Job]``; an empty list means the board is
    live and advertising nothing, and an exception means it could not be read.
    That distinction is what keeps "no openings" out of the failure reports.

:class:`crawler.crawler_engine.CrawlerEngine` looks adapters up by platform and
never branches on which one it got, so adding a system is a new module plus one
line in :data:`~crawler.crawler_engine.ADAPTER_MODULES`.

**Choosing a route.** In order of preference:

1. **A public API**, where the vendor has one — Greenhouse, Lever, Ashby,
   Breezy, Rippling, Pinpoint, Personio, Manatal, Comeet, Eightfold,
   SmartRecruiters, Workday, Oracle, Workable. These adapters are hand-written
   against the payload the vendor returns.
2. **The server-rendered board**, for the many vendors with no anonymous API.
   Those adapters are thin: they declare the hosts they serve and the shape of
   their posting URLs, and :mod:`adapters._paginated_html` does the rest —
   fetching, paging, extracting, and falling back through structured data,
   embedded JavaScript state and finally a headless browser.
3. **The browser alone**, for portals that render client-side and publish
   nothing anonymously — Bullhorn, ADP Recruiting Management. Those adapters
   supply no link pattern, because none of the static routes can succeed and
   claiming otherwise would only surface navigation links as jobs.

:mod:`adapters.generic` is the fallback for everything unrecognised, and is
also the shared implementation the platform adapters reuse for "which card is
this" and "where is the location" once they have decided which links are jobs.
"""
