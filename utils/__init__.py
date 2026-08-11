"""Shared helpers with no dependency on the crawl pipeline itself.

Home for the small, reusable pieces that several stages need — URL cleaning and
joining, text normalisation, country lookup from a location string, HTTP session
and retry setup, and logging configuration. Nothing here should import from
:mod:`crawler` or :mod:`adapters`, so the dependency direction stays one-way.
"""
