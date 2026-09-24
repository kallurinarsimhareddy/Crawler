# VENDORED from CareerCrawler-seamless zerocredit/extract.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""Reading people and companies off a public page, without inventing anything.

Two jobs: find the pages on a company site that name its leadership, and read
the people off them. Both are pattern-matching against messy HTML, so the
interesting part is not what this module finds -- it is what it refuses to
produce.

**An email address is only ever copied, never constructed.** There is no code
path here that assembles one. :func:`emails_in` reads ``mailto:`` links and
literal addresses out of the text; if a page names a CFO and separately lists
``info@company.com``, the CFO's email stays empty, because the page did not say
it was theirs. The temptation is obvious -- ``firstname.lastname@`` is right
often enough to look useful -- and it is exactly the thing that turns a contact
list into a bounce list and a sender domain into a blocked one. A name with no
email is a usable row; a name with a wrong email is worse than nothing.

**A title has to look like a title.** :func:`role_of` matches against a fixed
vocabulary of senior roles. A string that does not match is not stored as a job
title on the theory that it might be one.

**Nothing is promoted by proximity alone.** A name and a title become a contact
only when they are close together in a structure that pairs them -- the same
card, the same list item, the same heading-and-subheading. Two strings that
happen to be near each other on a page are not a person.

Everything returned carries the URL it came from and a source type, so
:mod:`zerocredit.confidence` can decide what it is worth and the sheet can show
the reader where it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Final, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import unquote as _UNQUOTE, urlsplit

__all__ = [
    "LEADERSHIP_HINTS",
    "ROLE_BUCKETS",
    "Discovered",
    "classify_page",
    "emails_in",
    "find_leadership_links",
    "people_on_page",
    "role_of",
    "role_spans",
    "split_name",
]

#: URL and link-text fragments that suggest a page names the leadership. Ordered
#: by how specific they are, because the first match decides the source type and
#: "leadership" is a stronger claim than "about".
LEADERSHIP_HINTS: Final[Tuple[Tuple[str, str], ...]] = (
    ("leadership", "official_leadership"),
    ("executive", "official_leadership"),
    ("management-team", "official_leadership"),
    ("management_team", "official_leadership"),
    ("our-team", "official_team"),
    ("our_team", "official_team"),
    ("meet-the-team", "official_team"),
    ("team", "official_team"),
    ("board-of-directors", "official_leadership"),
    ("board", "official_leadership"),
    ("officers", "official_leadership"),
    ("who-we-are", "official_about"),
    ("about-us", "official_about"),
    ("about", "official_about"),
    ("company/people", "official_team"),
    ("people", "official_team"),
    ("staff", "official_team"),
    ("contact", "official_contact"),
    ("press", "official_press_release"),
    ("news", "official_press_release"),
)

#: The senior roles this campaign cares about, as (pattern, bucket, department,
#: seniority). Matched on a normalised title with word boundaries, so "director"
#: inside another word cannot match.
_ROLE_PATTERNS: Final[Tuple[Tuple[str, str, str, str], ...]] = (
    (r"chief executive officer|(?<![a-z])ceo(?![a-z])", "CEO", "Executive", "C-Level"),
    (r"(?<![a-z])president(?![a-z])", "President", "Executive", "C-Level"),
    (r"chief operating officer|(?<![a-z])coo(?![a-z])", "COO", "Operations", "C-Level"),
    (r"chief financial officer|(?<![a-z])cfo(?![a-z])", "CFO", "Finance", "C-Level"),
    (
        r"chief human resources officer|chief people officer|(?<![a-z])chro(?![a-z])",
        "CHRO", "Human Resources", "C-Level",
    ),
    (r"chief information security officer|(?<![a-z])ciso(?![a-z])",
     "CISO", "IT", "C-Level"),
    (r"chief information officer|(?<![a-z])cio(?![a-z])", "CIO", "IT", "C-Level"),
    (r"chief technology officer|(?<![a-z])cto(?![a-z])", "CTO", "IT", "C-Level"),
    (r"chief (marketing|revenue|legal|medical|commercial|strategy|digital) officer",
     "Other C-Level", "Executive", "C-Level"),
    (r"(vice president|vp|svp|evp)[^a-z]{0,4}(of )?(human resources|hr|people|talent)",
     "VP HR", "Human Resources", "VP"),
    (r"(head|director) of (human resources|hr|people|talent)",
     "HR Director", "Human Resources", "Director"),
    (r"(human resources|hr) (director|manager|lead)",
     "HR Director", "Human Resources", "Director"),
    (r"(vice president|vp|svp|evp)[^a-z]{0,4}(of )?(information technology|it|technology|engineering)",
     "VP IT", "IT", "VP"),
    (r"(head|director) of (information technology|it|technology|engineering)",
     "IT Director", "IT", "Director"),
    (r"(it|information technology) (director|manager)", "IT Director", "IT", "Director"),
    (r"(vice president|vp|svp|evp)[^a-z]{0,4}(of )?(finance|accounting)",
     "VP Finance", "Finance", "VP"),
    (r"(head|director) of (finance|accounting)", "Finance Director", "Finance", "Director"),
    (r"(?<![a-z])controller(?![a-z])|(?<![a-z])treasurer(?![a-z])",
     "Finance Director", "Finance", "Director"),
    (r"(vice president|vp|svp|evp)[^a-z]{0,4}(of )?(operations|manufacturing|supply chain)",
     "VP Operations", "Operations", "VP"),
    (r"(head|director) of (operations|manufacturing|supply chain)",
     "Operations Director", "Operations", "Director"),
    (r"(owner|founder|co-founder|managing director|general manager|partner)",
     "Owner/Founder", "Executive", "C-Level"),
    (r"(?<![a-z])(svp|evp|senior vice president|executive vice president)(?![a-z])",
     "Other Senior", "Executive", "VP"),
    (r"(?<![a-z])(vice president|vp)(?![a-z])", "Other Senior", "Executive", "VP"),
    (r"(?<![a-z])director(?![a-z])", "Other Senior", "Executive", "Director"),
)

#: The buckets, in the priority order the brief gave them.
ROLE_BUCKETS: Final[Tuple[str, ...]] = (
    "CEO", "President", "COO", "CFO", "CHRO", "VP HR", "HR Director",
    "CIO", "CTO", "CISO", "VP IT", "IT Director",
    "VP Finance", "Finance Director", "VP Operations", "Operations Director",
    "Owner/Founder", "Other C-Level", "Other Senior",
)

#: Where a bucket sits in that priority order, for sorting.
_BUCKET_RANK: Final[Dict[str, int]] = {
    name: index for index, name in enumerate(ROLE_BUCKETS)
}

#: A literal email address. Deliberately strict about the local part so that a
#: version string or a filename cannot be read as an address.
_EMAIL: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}"
)

#: Addresses that are never a person's, however they were found.
_ROLE_ADDRESS_PREFIXES: Final[Tuple[str, ...]] = (
    "info", "sales", "support", "hello", "contact", "admin", "office",
    "enquiries", "inquiries", "help", "service", "marketing", "press",
    "media", "careers", "jobs", "hr", "recruiting", "webmaster", "noreply",
    "no-reply", "donotreply", "privacy", "legal", "billing", "accounts",
)

#: File extensions that look like an address but are not one.
_NOT_EMAIL_SUFFIXES: Final[Tuple[str, ...]] = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico",
)

#: A plausible personal name: two to four capitalised words, allowing the
#: particles and punctuation real names contain.
_NAME: Final[re.Pattern[str]] = re.compile(
    r"^(?:[A-Z][A-Za-z'’\-\.]{1,20}\s+){1,3}[A-Z][A-Za-z'’\-\.]{1,20}$"
)

#: Words that disqualify a string from being a person's name.
_NOT_A_NAME: Final[frozenset] = frozenset({
    "the", "and", "our", "your", "we", "us", "company", "team", "group",
    "board", "directors", "leadership", "management", "executive", "officers",
    "contact", "about", "home", "careers", "privacy", "policy", "terms",
    "solutions", "services", "products", "news", "press", "release",
    "read", "more", "learn", "view", "all", "meet", "join", "apply",
})

#: How much text around a match to keep as evidence.
_EVIDENCE_CHARS: Final[int] = 180


@dataclass
class Discovered:
    """One person read off one page.

    Attributes:
        full_name: The name as printed.
        job_title: The title as printed.
        role_bucket: Which of :data:`ROLE_BUCKETS` the title falls into.
        department: The function the title implies.
        seniority: The band the title implies.
        email: A published address, or ``""``. Never constructed.
        phone: A published number, or ``""``.
        linkedin_url: A profile URL found beside the person, or ``""``.
        source_url: The page this came from.
        source_type: What kind of page it was.
        evidence: The surrounding text, so a human can check the reading.
    """

    full_name: str
    job_title: str
    role_bucket: str = ""
    department: str = ""
    seniority: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    source_url: str = ""
    source_type: str = ""
    evidence: str = ""

    @property
    def rank(self) -> int:
        """Where this person sits in the campaign's priority order.

        Returns:
            A sort key; lower is more senior.
        """
        return _BUCKET_RANK.get(self.role_bucket, len(ROLE_BUCKETS))


def split_name(full_name: str) -> Tuple[str, str]:
    """Split a printed name into first and last.

    Args:
        full_name: The name as printed.

    Returns:
        ``(first, last)``. A single-word name is all first name; nothing is
        invented to fill the other half.
    """
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def looks_like_a_name(value: str) -> bool:
    """Whether a string is plausibly a person's name.

    Args:
        value: The candidate.

    Returns:
        Whether it matches the shape of a name and contains no word that rules
        it out. Conservative on purpose: a missed person costs nothing, and a
        heading stored as a person pollutes the dataset.
    """
    text = (value or "").strip()
    if not (4 <= len(text) <= 60) or not _NAME.match(text):
        return False
    words = {word.lower().strip(".,'’-") for word in text.split()}
    return not (words & _NOT_A_NAME)


def role_spans(title: str) -> List[Tuple[int, int]]:
    """Where each recognised role appears in a string.

    Used to detect that a value holds two people's titles run together, which
    is what a card with no markup boundary produces.

    Args:
        title: The text to scan.

    Returns:
        ``(start, end)`` per match, in order, non-overlapping, as offsets into
        ``title`` itself.

        Keeping them usable against the original string is the whole point:
        the normalisation replaces each non-alphanumeric character with one
        space rather than collapsing runs, and the leading pad is subtracted
        on the way out. An earlier version collapsed runs, so every index after
        the first punctuation mark was wrong, and a caller slicing on them cut
        "Executive Vice President and Chief Operating Officer" to "...and C".
    """
    original = title or ""

    # One character in, one character out -- no "+" on the class.
    normalised = re.sub(r"[^a-z0-9]", " ", original.lower())
    text = f" {normalised} "
    spans: List[Tuple[int, int]] = []

    for pattern, _bucket, _dept, _sen in _ROLE_PATTERNS:
        for match in re.finditer(pattern, text):
            spans.append((match.start(), match.end()))

    spans.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    limit = len(original)
    return [
        (max(0, start - 1), min(limit, end - 1))
        for start, end in merged
    ]


def role_of(title: str) -> Optional[Tuple[str, str, str]]:
    """Classify a job title into the campaign's role vocabulary.

    Args:
        title: The title as printed.

    Returns:
        ``(bucket, department, seniority)``, or ``None`` when the title is not
        one of the senior roles this campaign targets. ``None`` is a real
        answer: a Marketing Coordinator is a person, but not one to record.
    """
    text = f" {re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).strip()} "
    if len(text) > 160:
        return None

    for pattern, bucket, department, seniority in _ROLE_PATTERNS:
        if re.search(pattern, text):
            return bucket, department, seniority
    return None


def _decode(text: str) -> str:
    """Percent-decode text, tolerating anything that is not encoded.

    Args:
        text: Any text, encoded or not.

    Returns:
        The decoded text, or the original when it cannot be decoded. A page
        that is simply not encoded must pass through unchanged rather than
        raise.
    """
    try:
        return _UNQUOTE(text)
    except Exception:  # noqa: BLE001 - a bad escape must not lose the page
        return text


def emails_in(text: str) -> List[str]:
    """Every literal email address in a piece of text.

    Copies only. There is deliberately no counterpart to this function that
    *builds* an address from a name and a domain -- see the module docstring.

    Args:
        text: Any text.

    Returns:
        The addresses found, lowercased and de-duplicated, with role addresses
        and things that are really filenames removed.
    """
    found: List[str] = []
    seen: Set[str] = set()

    # Decode first. A mailto href routinely arrives percent-encoded --
    # "mailto:%20bblock@diamondmowers.com" is a real example from the cache --
    # and matching before decoding captures the %20 as part of the local part,
    # producing an address that looks plausible and bounces. Decoding twice is
    # deliberate: some sites double-encode.
    decoded = _decode(_decode(text or ""))

    for match in _EMAIL.finditer(decoded):
        address = match.group(0).lower().strip(".")
        if address in seen:
            continue
        if any(address.endswith(suffix) for suffix in _NOT_EMAIL_SUFFIXES):
            continue
        seen.add(address)
        found.append(address)

    return found


def is_personal_address(address: str) -> bool:
    """Whether an address belongs to a person rather than a department.

    Args:
        address: The address.

    Returns:
        Whether the local part is not one of the well-known role mailboxes. An
        ``hr@`` address is useful, but it is not a person, and storing it on a
        named contact would assert something the page did not say.
    """
    local = (address or "").split("@", 1)[0].lower().strip()
    if not local:
        return False

    # Both the whole local part and its first token are checked: "no-reply"
    # matches whole, "hr.team" matches on its first token, and a real name like
    # "jane.doe" matches neither.
    head = re.split(r"[._\-+]", local)[0]
    return local not in _ROLE_ADDRESS_PREFIXES and head not in _ROLE_ADDRESS_PREFIXES


def classify_page(url: str, link_text: str = "") -> str:
    """Decide what kind of page a URL is.

    Args:
        url: The page URL.
        link_text: The anchor text that pointed at it, when known.

    Returns:
        A source type from :data:`zerocredit.confidence.SOURCE_SCORES`.
    """
    haystack = f"{urlsplit(url or '').path.lower()} {(link_text or '').lower()}"
    for fragment, source_type in LEADERSHIP_HINTS:
        if fragment in haystack:
            return source_type
    return "official_other"


def find_leadership_links(soup, base_url: str, limit: int = 6) -> List[Tuple[str, str]]:
    """Find the pages on a site most likely to name its leadership.

    Args:
        soup: The parsed home page.
        base_url: The URL it was fetched from, for resolving relative links.
        limit: How many to return.

    Returns:
        ``(url, source_type)`` pairs, most promising first, restricted to the
        company's own site. Off-site links are dropped: this subsystem reads
        what a company says about itself.
    """
    from cloud.intel.vendor.html import absolute_url, clean_text, same_site

    scored: List[Tuple[int, str, str]] = []
    seen: Set[str] = set()

    for anchor in soup.find_all("a", href=True):
        url = absolute_url(base_url, anchor.get("href"))
        if not url or url in seen or not same_site(base_url, url):
            continue

        text = clean_text(anchor.get_text())
        haystack = f"{urlsplit(url).path.lower()} {text.lower()}"

        for rank, (fragment, source_type) in enumerate(LEADERSHIP_HINTS):
            if fragment in haystack:
                seen.add(url)
                scored.append((rank, url, source_type))
                break

    scored.sort(key=lambda item: (item[0], len(item[1])))
    return [(url, source_type) for _, url, source_type in scored[:limit]]


def _nearby_text(node, chars: int = _EVIDENCE_CHARS) -> str:
    """The text of a node's neighbourhood, for pairing and evidence.

    Args:
        node: A parsed element.
        chars: How much to keep.

    Returns:
        Cleaned text from the node's container.
    """
    from cloud.intel.vendor.html import clean_text

    container = node
    for _ in range(3):
        if container.parent is None:
            break
        container = container.parent
        text = clean_text(container.get_text(" "))
        if len(text) >= 40:
            return text[:chars]
    return clean_text(node.get_text(" "))[:chars]


def people_on_page(
    soup,
    url: str,
    source_type: str,
    company_domain: str = "",
) -> List[Discovered]:
    """Read the senior people named on one page.

    A person is recorded only when a name and a recognised senior title are
    found together in the same block of markup. Emails and phone numbers are
    attached only when they appear in that same block -- an address in the site
    footer is not this person's address.

    Args:
        soup: The parsed page.
        url: Where it came from.
        source_type: What kind of page it is.
        company_domain: The company's domain, so an off-domain address is not
            attributed to an employee.

    Returns:
        The people found, most senior first, de-duplicated by name within the
        page.
    """
    from cloud.intel.vendor.html import clean_text

    found: Dict[str, Discovered] = {}

    # Walk elements that plausibly hold one person: cards, list items, table
    # rows, and heading blocks. Pairing inside one element is what stops a name
    # at the top of a page being married to a title at the bottom.
    candidates = soup.find_all(
        ["li", "tr", "article", "section", "div", "figure", "td", "p"]
    )

    for node in candidates:
        text = clean_text(node.get_text(" "))
        if not (8 <= len(text) <= 400):
            continue

        # Long blocks are containers, not people; let their children match.
        if len(node.find_all(["li", "tr", "article", "figure"])) > 1:
            continue

        name, title = _name_and_title_in(node, text)
        if not name or not title:
            continue

        classified = role_of(title)
        if classified is None:
            continue

        bucket, department, seniority = classified
        key = name.lower()

        block = text
        email = ""
        for address in emails_in(block) + _mailto_addresses(node):
            if not is_personal_address(address):
                continue
            if company_domain and not address.endswith(f"@{company_domain}"):
                # An address on another domain may well be theirs, but the page
                # did not establish that. Record nothing rather than guess.
                continue
            email = address
            break

        person = Discovered(
            full_name=name,
            job_title=title,
            role_bucket=bucket,
            department=department,
            seniority=seniority,
            email=email,
            phone=_phone_in(block),
            linkedin_url=_linkedin_in(node),
            source_url=url,
            source_type=source_type,
            evidence=block[:_EVIDENCE_CHARS],
        )

        existing = found.get(key)
        if existing is None or person.rank < existing.rank:
            found[key] = person

    people = sorted(found.values(), key=lambda p: (p.rank, p.full_name))
    return people


def _name_and_title_in(node, text: str):
    """Find a name and a title paired inside one element.

    Args:
        node: The element.
        text: Its cleaned text.

    Returns:
        ``(name, title)``, either of which may be ``""``.
    """
    from cloud.intel.vendor.html import clean_text

    # Preferred: an explicit heading or emphasised element holds the name and a
    # sibling holds the title. This is how nearly every team page is built.
    for tag in node.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "span"]):
        candidate = clean_text(tag.get_text(" "))
        if not looks_like_a_name(candidate):
            continue
        remainder = text.replace(candidate, " ", 1)
        title = _title_in(remainder)
        if title:
            return candidate, title

    # Fallback: "Name, Title" or "Name - Title" on one line.
    for separator in (",", "–", "—", "-", "|", "·"):
        if separator not in text:
            continue
        head, _, tail = text.partition(separator)
        head, tail = head.strip(), tail.strip()
        if looks_like_a_name(head):
            title = _title_in(tail)
            if title:
                return head, title

    return "", ""


def _title_in(text: str) -> str:
    """The first recognised senior title in a piece of text.

    Args:
        text: The text to read.

    Returns:
        The title as printed, or ``""``.
    """
    for chunk in re.split(r"[,–—|·\n]|(?<=[a-z])\s+(?=and\s)", text):
        candidate = chunk.strip(" \t-–—|·")
        if 2 <= len(candidate) <= 120 and role_of(candidate):
            return candidate
    return ""


def _mailto_addresses(node) -> List[str]:
    """Addresses published as ``mailto:`` links inside an element.

    Args:
        node: The element.

    Returns:
        The addresses, lowercased.
    """
    found = []
    for anchor in node.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.lower().startswith("mailto:"):
            # Everything after the scheme, minus a query string or fragment,
            # then decoded and stripped. All four of those appear in the cache.
            address = href.split(":", 1)[1]
            address = address.split("?", 1)[0].split("#", 1)[0]
            address = _decode(_decode(address)).strip().strip(",;").lower()
            if address and "@" in address:
                found.append(address)
    return found


def _linkedin_in(node) -> str:
    """A LinkedIn profile URL inside an element.

    Args:
        node: The element.

    Returns:
        The first ``/in/`` profile URL, or ``""``. Company pages
        (``/company/``) are deliberately excluded: they identify the employer,
        not the person.
    """
    for anchor in node.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if "linkedin.com/in/" in href.lower():
            return href.split("?", 1)[0]
    return ""


#: A published phone number. Requires separators, so a long digit run -- an id,
#: a price, a year range -- is not read as one.
_PHONE: Final[re.Pattern[str]] = re.compile(
    r"(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]\d{3}[\s.\-]\d{4}"
)


def _phone_in(text: str) -> str:
    """A published phone number in a piece of text.

    Args:
        text: The text.

    Returns:
        The first number found, or ``""``.
    """
    match = _PHONE.search(text or "")
    return match.group(0).strip() if match else ""
