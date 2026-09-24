# VENDORED from CareerCrawler utils/encoding.py::strip_accents (e21045c).
"""Accent folding used by name matching."""
from __future__ import annotations

import unicodedata


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))
