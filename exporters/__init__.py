"""Output writers for crawl results.

Exporters take the :class:`models.job.Job` records produced by a crawl and
serialise them to a deliverable file. The project's primary deliverable is
``output/jobs.xlsx``, written by :mod:`exporters.excel_exporter`.
"""
