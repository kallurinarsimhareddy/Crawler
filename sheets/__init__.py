"""Google Sheets integration, built to never destroy what is already there.

The spreadsheet is the operator's, not the crawler's. It has tabs the crawler
did not create, columns somebody added by hand, notes in the margins, and
filters and formatting that took an afternoon. A weekly job that rewrites it
wholesale would be correct exactly once and destructive every Friday after.

So the integration is built on three rules, enforced in code rather than
intended in a docstring:

**Nothing is ever deleted.** There is no ``deleteSheet`` and no
``deleteDimension`` call anywhere in this package, and a test asserts that no
request of either kind is emitted. A tab the crawler does not recognise is left
alone; a column it does not know is read past, not removed.

**Structure is discovered, never assumed.** :mod:`sheets.schema` reads the live
tab titles and header rows on connect and maps the crawler's fields onto
whatever headers are already present, by normalised name. A header spelled
``"Career page url"`` is matched to ``career_url``; a column the crawler wants
that the sheet lacks is *appended to the right* of the last one in use.

**Values are written by column mapping, never by position.** If somebody inserts
a column in the middle of a tab, the next run writes to the right cells anyway.

    sheets/
        auth.py        service-account or OAuth credentials, from env or file
        client.py      batching, chunking, backoff -- takes an injected service
        schema.py      tab and column definitions, and the mapping onto a live sheet
        sync.py        reads the master list, writes the jobs, discovery and dashboard
        config_tab.py  the industry discovery configuration

:mod:`sheets.client` never imports the Google libraries. It is handed a service
object and calls it, which is what lets the whole package be tested offline
against a fake that records every request.
"""
