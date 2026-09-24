# VENDORED from CareerCrawler-seamless zerocredit/clean.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""Getting one fact per field, when the page put several in the same sentence.

A team page rarely marks up a person tidily. What the extractor actually sees is
this, all inside one element::

    Vice President of Sales 1.800.638.6000 ext 306 gclothier@aaglobal.com

Three facts in one string. The first version stored the whole thing as the job
title, which makes the title useless *and* silently discards a phone number the
page was giving away. Both halves of that are worth fixing: the field is wrong,
and data was lost.

So parsing a person is subtractive. Everything recognisable is lifted out and
put where it belongs -- :func:`split_contact_text` returns the phone, the email
and the remaining text -- and only what survives is considered as a title.

The rules that make this safe rather than destructive:

**Take the specific before the general.** Emails and phones have unambiguous
shapes, so they come out first and cannot be mistaken for words of a title.

**Stop at the next person.** ``READ MORE``, ``|``, a run of two or more spaces,
and the person's own name are boundaries. A title that runs past one has
swallowed the next card, which is how ``President & General Manager READ MORE
Steve Schwecke Quality Manager`` happened.

**But the job is not always first.** Splitting at a boundary and keeping the
leading piece was itself a bug: pages print a heading above a person, so
``Investor Relations Contact   Director of Investor Relations`` has the label
first and the job second, and taking the first piece lost the title outright.
:func:`clean_title` therefore evaluates *every* segment and returns the first
one that reads as a job.

**A job title is not a role bucket.** :func:`looks_like_a_title` accepts
anything containing a job word, which is far broader than the handful of named
roles :mod:`zerocredit.extract` classifies. Requiring a bucket cleared "Plant
Manager" and "Quality Manager" -- destroying good data to fix a formatting
problem, which is the worst outcome available here.

**Refuse rather than guess.** :func:`clean_title` returns ``""`` when what is
left is not a job title, and :func:`clean_name` returns ``""`` when a string is
a slogan rather than a person. An empty field is honest; a wrong one is not.

**Never invent.** Nothing here composes a value. It only removes and splits.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Final, List, Tuple
from urllib.parse import unquote

__all__ = [
    "ContactText",
    "clean_company_name",
    "clean_department",
    "clean_name",
    "clean_seniority",
    "clean_title",
    "extension_of",
    "is_dialable",
    "normalise_phone",
    "phones_in",
    "split_contact_text",
]

#: An email address, for removal from a text field.
_EMAIL: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}"
)

#: A phone number in the shapes actually seen on these sites: dotted, dashed,
#: spaced, parenthesised, with or without a country code.
#:
#: The country-code group accepts only "+NN" or a bare leading "1". Letting
#: any one-to-three digits play that role spliced an extension into the next
#: number: "310-530-7274 ext 138 310-530-7274" matched from the "138" and
#: produced "1383105307274".
_PHONE: Final[re.Pattern[str]] = re.compile(
    r"""(?<![\d\w])
    (?:\+\d{1,3}[\s.\-]?|1[\s.\-])?  # country code: "+44", or a bare leading 1
    (?:\(\s*\d{3}\s*\)|\d{3})        # area code, bracketed or not
    [\s.\-]\s?
    \d{3}
    [\s.\-]\s?
    \d{4}
    (?![\d])
    """,
    re.X,
)

#: An extension, in the several ways sites write one.
_EXTENSION: Final[re.Pattern[str]] = re.compile(
    r"(?:,|\s)*\b(?:ext(?:ension)?\.?|x)\s*[:.]?\s*(\d{1,6})\b", re.I
)

#: A URL, which is never part of a title.
_URL: Final[re.Pattern[str]] = re.compile(r"https?://\S+|www\.\S+", re.I)

#: Boilerplate and navigation that ends up inside a card's text.
_NAVIGATION: Final[Tuple[str, ...]] = (
    "read more", "read bio", "learn more", "click here", "view profile",
    "view bio", "full bio", "contact us", "call us", "email us", "get in touch",
    "back to top", "see more", "show more", "load more", "download",
    "linkedin profile", "connect on linkedin", "send email", "send an email",
    "phone:", "tel:", "telephone:", "email:", "e-mail:", "fax:", "mobile:",
    "direct:", "office:", "cell:", "toll free", "toll-free",
)

#: Label words that remain once the value beside them has been lifted out --
#: "Telephone: 310-972-5124" becomes "Telephone" once the number is gone.
#: Stripped only from the ends, so a genuine "Director, Telephone Systems"
#: keeps its word.
_TRAILING_LABELS: Final[Tuple[str, ...]] = (
    "telephone", "phone", "email", "e-mail", "mail", "fax", "mobile", "cell",
    "direct", "office", "tel", "contact", "extension", "ext",
)

#: What joins two roles held by one person. When one of these sits between two
#: role phrases the phrases belong to the same job, not to two people.
_ROLE_CONJUNCTIONS: Final[frozenset] = frozenset({
    "and", "&", "/", "or", "-", "–", "—", ",", "of", "of the", "for",
    "and the", "&amp;", "plus",
})

#: A leading "Title:", "Role:", "Position:" and the like. The value is what
#: matters; the label is the page's furniture.
_LEADING_LABEL: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:job\s+)?(?:title|role|position|designation|department|dept|team|"
    r"seniority|level|function)\s*[:\-–—]\s*",
    re.I,
)

#: Words that make a phrase a job. Broad on purpose: this decides whether a
#: value is a title at all, and a narrow list would clear real titles. The
#: bucket vocabulary in :mod:`zerocredit.extract` is a different question --
#: which of a few named roles this is -- and answering "none of them" there
#: must never mean "not a title" here. Requiring it did, and cleared "Plant
#: Manager".
_TITLE_WORDS: Final[frozenset] = frozenset({
    "accountant", "administrator", "advisor", "adviser", "agent", "analyst",
    "architect", "assistant", "associate", "attorney", "buyer", "captain",
    "chair", "chairman", "chairwoman", "chairperson", "chief", "clerk",
    "controller", "coordinator", "counsel", "counselor", "consultant", "cto",
    "ceo", "cfo", "coo", "cio", "cmo", "cro", "cso", "curator", "dean",
    "designer", "developer", "director", "editor", "engineer", "estimator",
    "executive", "evp", "foreman", "founder", "general", "head", "hr",
    "inspector", "instructor", "lead", "leader", "machinist", "manager",
    "managing", "marketing", "master", "mechanic", "officer", "operator",
    "owner", "partner", "planner", "president", "principal", "producer",
    "professor", "programmer", "proprietor", "recruiter", "registrar",
    "representative", "rep", "scientist", "secretary", "specialist",
    "superintendent", "supervisor", "svp", "technician", "technologist",
    "trainer", "treasurer", "vp", "administrative", "operations", "sales",
    "purchasing", "procurement", "safety", "quality", "production",
    "maintenance", "logistics", "finance", "accounting", "engineering",
    "estimating", "scheduler", "dispatcher", "draftsman", "drafter",
    "welder", "fabricator", "assembler", "inspector", "auditor", "paralegal",
    "nurse", "physician", "pharmacist", "therapist", "surveyor", "geologist",
    "chemist", "physicist", "statistician", "actuary", "underwriter",
    "broker", "trader", "banker", "teller", "cashier", "merchandiser",
    "strategist", "evangelist", "advocate", "ambassador", "liaison",
    "facilitator", "moderator", "mediator", "arbitrator", "investigator",
    "examiner", "appraiser", "adjuster", "processor", "technical",
})

#: Where one person's text stops and the next begins. The second group are the
#: section headings of a bio page: everything after "Professional
#: Registrations" or "Education" is credentials, not the job.
_BOUNDARIES: Final[Tuple[str, ...]] = (
    "read more", "read bio", "view profile", "full bio", "learn more",
    "professional registrations", "professional affiliations", "education",
    "certifications", "credentials", "affiliations", "memberships",
    "publications", "awards", "areas of expertise", "years of experience",
)

#: The longest a real job title runs. Beyond this the string has picked up the
#: next card, a paragraph of prose, or a whole navigation block.
MAX_TITLE_CHARS: Final[int] = 80

#: The longest a personal name runs.
MAX_NAME_CHARS: Final[int] = 60

#: How far into a card a role may appear and still be that person's role.
#: Beyond this it is prose that happens to contain the word.
MAX_WORDS_BEFORE_ROLE: Final[int] = 6

#: Words that mean a string is not a person's name, however it is capitalised.
_NOT_NAME_WORDS: Final[frozenset] = frozenset({
    "trust", "approachability", "specialist", "manager", "director", "officer",
    "president", "engineer", "coordinator", "supervisor", "analyst", "lead",
    "sales", "service", "support", "team", "group", "department", "division",
    "solutions", "services", "products", "industries", "company", "corporation",
    "welcome", "about", "contact", "careers", "news", "home", "menu",
    "building", "leadership", "management", "executive", "board", "investor",
    "relations", "quality", "operations", "fulfillment", "international",
    "human", "resources", "marketing", "finance", "technology", "global",
})


def _strip_tags_and_entities(text: str) -> str:
    """Remove markup and decode entities from a field value.

    Args:
        text: Any text, possibly carrying tags or entities.

    Returns:
        Plain text with runs of whitespace collapsed.
    """
    if not text:
        return ""

    plain = re.sub(r"<[^>]{0,200}>", " ", str(text))
    plain = html.unescape(html.unescape(plain))
    try:
        plain = unquote(plain)
    except Exception:  # noqa: BLE001 - a stray % must not raise
        pass
    plain = plain.replace('\xa0', ' ').replace('\u200b', '')

    # Collapse single spaces, but keep a run of two or more as a marker.
    # The page rendered separate elements with that gap, and it is the only
    # evidence that "DIRECTOR   Trainee Supervisor" is two people run
    # together rather than one long title.
    plain = re.sub('[\r\n\t]+', '  ', plain)
    plain = re.sub(r' {2,}', '\x00', plain)
    plain = re.sub(r' +', ' ', plain)
    plain = plain.replace('\x00', '  ')
    return plain.strip()


def normalise_phone(raw: str) -> str:
    """Render a phone number in a single readable form.

    Args:
        raw: The number as printed.

    Returns:
        The digits regrouped, with the extension appended when there was one.
        A number this function cannot make sense of is returned trimmed rather
        than discarded -- it was on the page, and mangling it would be worse
        than leaving it as found.
    """
    text = _strip_tags_and_entities(raw)
    if not text:
        return ""

    extension = extension_of(text)
    digits = re.sub(r"\D", "", _EXTENSION.sub(" ", text))

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        formatted = f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    elif 7 <= len(digits) <= 15:
        formatted = digits
    else:
        return text.strip()

    return f"{formatted} ext {extension}" if extension else formatted


#: Digit counts a dialable number can have once the extension and a leading US
#: country code are removed. Seven is a local number, ten a full US one.
_DIALABLE_LENGTHS: Final[Tuple[int, ...]] = (7, 10)

#: An explicitly international number may be longer, but not arbitrarily so.
_INTERNATIONAL_RANGE: Final[Tuple[int, int]] = (8, 15)


def is_dialable(value: str) -> bool:
    """Whether a value is a number somebody could actually ring.

    "Digits exist" is not the test. ``524499200294`` was retained as a contact
    phone: twelve digits scraped out of a street address, on a row whose
    "person" was *Corporate Office*. A number that cannot be dialled is not a
    phone number, whatever its shape.

    An explicitly international number -- one written with a ``+`` -- is allowed
    a longer run of digits, because that is what the existing normalisation
    already preserves. Everything else must be a local or full US number.

    Args:
        value: The number as held, extension included.

    Returns:
        Whether it is dialable.

    Examples:
        >>> is_dialable("800-638-6000 ext 306")
        True
        >>> is_dialable("524499200294")
        False
        >>> is_dialable("+44 20 7123 4567")
        True
    """
    text = str(value or "")
    digits = re.sub(r"\D", "", text.split(" ext ")[0])
    if not digits:
        return False

    # A single repeated digit, or a straight count up or down, is a placeholder
    # rather than a number. Only *one* distinct digit counts as repeated:
    # "800-800-8008" uses two and is a real toll-free number.
    if len(set(digits)) == 1:
        return False
    if digits in "01234567890123456789" or digits in "98765432109876543210":
        return False

    if text.lstrip().startswith("+"):
        low, high = _INTERNATIONAL_RANGE
        return low <= len(digits) <= high

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) in _DIALABLE_LENGTHS


def extension_of(text: str) -> str:
    """The extension published beside a number, if any.

    Args:
        text: The text around the number.

    Returns:
        The extension digits, or ``""``.
    """
    match = _EXTENSION.search(text or "")
    return match.group(1) if match else ""


def phones_in(text: str) -> List[str]:
    """Every phone number in a piece of text, normalised.

    Args:
        text: Any text.

    Returns:
        The numbers found, de-duplicated, in the order they appear. An
        extension immediately following a number is attached to it.
    """
    plain = _strip_tags_and_entities(text)
    found: List[str] = []
    seen = set()

    for match in _PHONE.finditer(plain):
        # Only an extension that follows immediately belongs to this number.
        # Appending the next 24 characters wholesale swept up the *following*
        # number, and "555-123-4567  Mobile 555-987-6543" came back as one
        # unreadable value.
        trailing = plain[match.end(): match.end() + 24]
        extension = _EXTENSION.match(trailing)
        candidate = normalise_phone(
            match.group(0) + (extension.group(0) if extension else "")
        )
        if candidate and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    return found


@dataclass(frozen=True)
class ContactText:
    """One person's text, taken apart.

    Attributes:
        remainder: What is left once the recognisable facts are removed --
            the candidate for a job title.
        emails: Addresses that were embedded in it.
        phones: Numbers that were embedded in it.
    """

    remainder: str
    emails: Tuple[str, ...] = ()
    phones: Tuple[str, ...] = ()


def split_contact_text(text: str) -> ContactText:
    """Separate a person's blob into its parts.

    This is the function that fixes the reported defect. Given::

        Vice President of Sales 1.800.638.6000 ext 306 gclothier@aaglobal.com

    it returns the title, the number and the address as three separate things,
    rather than storing the whole string as a job title.

    Args:
        text: The text found around a person.

    Returns:
        The parts. ``remainder`` still needs :func:`clean_title` applied to it;
        this function only removes, it does not judge what is left.
    """
    plain = _strip_tags_and_entities(text)
    if not plain:
        return ContactText("")

    emails = [address.lower() for address in _EMAIL.findall(plain)]
    plain = _EMAIL.sub(" ", plain)

    phones = phones_in(plain)
    plain = _EXTENSION.sub(" ", _PHONE.sub(" ", plain))

    plain = _URL.sub(" ", plain)
    # Preserve the two-space marker: email and phone removal must not
    # erase the boundary that says where the next card began.
    plain = re.sub(r' {2,}', '\x00', plain)
    plain = re.sub(r'[ \t]+', ' ', plain)
    plain = plain.replace('\x00', '  ').strip(" \t-–—|·,;:")

    return ContactText(plain, tuple(dict.fromkeys(emails)), tuple(phones))


def _cut_at_boundary(text: str) -> str:
    """Truncate where the next person or the next section starts.

    Args:
        text: The remainder of a person's text.

    Returns:
        Everything before the first boundary marker.
    """
    segments = _segments(text)
    return segments[0] if segments else ""


def _segments(text: str) -> List[str]:
    """Split a blob at every boundary between one card and the next.

    Taking only the first segment was wrong, and it was losing real titles. A
    page that prints a heading above a person -- "Media Contact", "Investor
    Relations Contact", "Slowakia" -- puts that heading *before* the job, so the
    first segment is the label and the second is the answer::

        "Investor Relations Contact   Director of Investor Relations"

    The caller therefore gets every segment and picks the one that reads as a
    title, rather than being handed the first and told it is the only candidate.

    Args:
        text: The remainder of a person's text.

    Returns:
        The segments, in order, with empty ones dropped.
    """
    pattern = "|".join(
        [re.escape(marker) for marker in _BOUNDARIES]
        + [r"\s{2,}", r"\|", "·", "•", "»", "—"]
    )
    parts = re.split(pattern, text or "", flags=re.I)
    return [part.strip(" \t-–—|·,;:") for part in parts if part and part.strip(" \t-–—|·,;:")]


#: Words that start a biography rather than continue a job title. "Chief
#: Financial Officer Richard Kuck joined our team in 2000..." is a title
#: followed by prose, and the prose is not part of the title.
_PROSE_STARTERS: Final[frozenset] = frozenset({
    "joined", "joins", "brings", "brought", "leads", "led", "began", "begins",
    "started", "starts", "serves", "served", "oversees", "oversaw", "holds",
    "held", "earned", "received", "graduated", "works", "worked", "manages",
    "managed", "spent", "comes", "came", "returned", "previously", "prior",
    "currently", "he", "she", "they", "his", "her", "their", "bio",
    "biography", "responsible", "focuses", "specializes", "specialises",
    "following", "read", "meet", "learn", "view", "watch", "click",
    "has", "have", "was", "were", "is", "are", "been", "having", "where",
    "who", "whose", "which", "while", "during", "after", "before", "since",
    "about", "our", "we", "you", "this", "that", "these", "those",
})


def _cut_at_prose(text: str) -> str:
    """Truncate where a job title stops and a biography starts.

    Args:
        text: The candidate title.

    Returns:
        The words before the first biography word, or the text unchanged.
    """
    words = (text or "").split()
    for index, word in enumerate(words):
        if index and word.strip(",.;:").lower() in _PROSE_STARTERS:
            return " ".join(words[:index]).strip(" \t-–—|·,;:")
    return text


def _split_at_name(text: str, name: str) -> List[str]:
    """Split a blob where the person's own name appears inside it.

    A title containing this person's name has been concatenated with something
    else, and the job can be on either side of it. Both orders occur::

        "Chief Financial Officer Richard Kuck joined our team in 2000..."
        "Media Contact Kim Rahfaldt, APR Director of Marketing"

    So both halves are returned as candidates, in that order, rather than the
    leading one being assumed to be the answer.

    Args:
        text: The candidate title.
        name: The person's stored full name.

    Returns:
        ``[text]`` when the name does not appear, else the parts either side.
    """
    parts = [p for p in (name or "").split() if len(p) > 2]
    if not parts or not text:
        return [text]

    lowered = text.lower()
    cut, width = len(text), 0
    for part in parts:
        position = lowered.find(part.lower())
        if 0 < position < cut:
            cut, width = position, len(part)

    if cut >= len(text):
        return [text]

    before = text[:cut].strip(" \t-–—|·,;:")

    # Step over the rest of the name too. Cutting at "Kim" alone left
    # "Rahfaldt, APR Director of Marketing", with half a surname on the front.
    after_words = text[cut + width:].strip(" \t-–—|·,;:,").split()
    lowered_parts = {p.lower() for p in parts}
    while after_words and after_words[0].strip(",.").lower() in lowered_parts:
        after_words.pop(0)

    return [before, " ".join(after_words).strip(" \t-–—|·,;:,")]


#: Tokens that end a title and begin an address block.
#:
#: Deliberately short. "Unit" was on this list and cleared "Business Unit
#: Manager"; "Dr" would clear a doctorate; "St" appears in company names. Only
#: words that cannot plausibly sit inside a job title are here, and the digit
#: rule in :func:`_cut_at_address` does most of the work anyway.
_ADDRESS_WORDS: Final[frozenset] = frozenset({
    "suite", "ste", "avenue", "boulevard", "blvd", "parkway", "pkwy",
    "highway", "hwy",
})


def _cut_at_address(text: str) -> str:
    """Truncate where a title runs into a postal address or a phone block.

    Args:
        text: The candidate title.

    Returns:
        The words before the address begins.
    """
    words = (text or "").split()
    for index, word in enumerate(words):
        token = word.strip(",.;:()").lower()
        if index and (
            any(c.isdigit() for c in token)
            or token in _ADDRESS_WORDS
            or "@" in token
        ):
            return " ".join(words[:index]).strip(" \t-–—|·,;:")
    return text


def _title_around_role(text: str) -> str:
    """Lift a title out of the middle of a blob, around the role it names.

    The last resort, for text where the job is neither at the start nor after a
    boundary::

        "Samuel W. Croll III is the Chief Executive Officer of"
        -> "Chief Executive Officer"

    The window starts at the recognised role and grows outwards only over words
    that are themselves job words or the connectives between them, so it cannot
    wander into the surrounding prose.

    Args:
        text: One segment.

    Returns:
        The title found around the first role, or ``""``.
    """
    from cloud.intel.vendor.zc_extract import role_spans

    spans = role_spans(text)
    if not spans:
        return ""

    words = list(re.finditer(r"\S+", text or ""))
    if not words:
        return ""

    start, end = spans[0]
    first = next((i for i, w in enumerate(words) if w.end() > start), None)
    last = next(
        (i for i in range(len(words) - 1, -1, -1) if words[i].start() < end), None
    )
    if first is None or last is None or last < first:
        return ""

    # A job named near the front of a card is that person's job. A role word
    # appearing fifteen words into a paragraph is prose using the word --
    # "...investing in the success of our partners" is not a title.
    if first > MAX_WORDS_BEFORE_ROLE:
        return ""

    def joinable(index: int) -> bool:
        """Whether a neighbouring word may be pulled into the window.

        Args:
            index: The word index.

        Returns:
            Whether it is a job word or a connective.
        """
        token = words[index].group().strip(",.;:&()").lower()
        return token in _TITLE_WORDS or token in _ROLE_CONJUNCTIONS

    left = first
    while left > 0 and first - (left - 1) <= 3 and joinable(left - 1):
        left -= 1

    right = last
    while right + 1 < len(words) and (right + 1) - last <= 4 and joinable(right + 1):
        right += 1

    return text[words[left].start(): words[right].end()].strip(" \t-–—|·,;:&")


def _trim_tail(text: str) -> str:
    """Drop a trailing run that carries no job words.

    "District Director Capital Center 251 N. Illinois St." is a title followed
    by a building and an address, and everything after the last recognised role
    goes.

    The tail has to *prove* it is not part of the job before it is dropped, by
    containing a digit or an address word. Trimming on "no job words in the
    tail" alone was too eager and cut "Vice President, Human Resources and
    Administration" down to "Vice President" -- job titles are full of nouns no
    vocabulary lists.

    Args:
        text: The candidate title.

    Returns:
        The trimmed text.
    """
    from cloud.intel.vendor.zc_extract import role_spans

    spans = role_spans(text)
    if not spans:
        return text

    tail = text[spans[-1][1]:]
    if len(tail.split()) <= 2:
        return text

    tail_words = {w.strip(",.;:").lower() for w in tail.split()}
    looks_like_an_address = (
        any(c.isdigit() for c in tail) or bool(tail_words & _ADDRESS_WORDS)
    )
    if looks_like_an_address and not (tail_words & _TITLE_WORDS):
        return text[: spans[-1][1]].strip(" \t-–—|·,;:&")

    return text


def _cut_at_second_role(text: str) -> str:
    """Truncate before a second job title, which belongs to the next person.

    ``HUMAN RESOURCES DIRECTOR Trainee Supervisor`` is two people's titles run
    together by a card whose markup gave the extractor no boundary. Keeping the
    first is right; keeping both is wrong for each of them.

    Args:
        text: The candidate title.

    Returns:
        The text up to the second role, or unchanged when there is only one.
    """
    from cloud.intel.vendor.zc_extract import role_spans

    spans = role_spans(text)
    if len(spans) < 2:
        return text

    # One person often holds two roles, and the join says so: "Executive Vice
    # President *and* Chief Operating Officer" is one job. Cutting on any second
    # role turned that into "Executive Vice President and C". Only an
    # unconnected second role starts somebody else.
    between = text[spans[0][1]: spans[1][0]].strip(" \t-–—|·,;:")
    if not between or between.lower() in _ROLE_CONJUNCTIONS:
        return text

    return text[: spans[1][0]].strip(" \t-–—|·,;:&")


def _strip_labels(text: str) -> str:
    """Remove label words stranded at either end of a value.

    Args:
        text: The candidate.

    Returns:
        The text without a leading or trailing bare label.
    """
    result = _LEADING_LABEL.sub("", text or "")
    for _ in range(3):
        lowered = result.lower().strip(" 	-–—|·,;:")
        changed = False
        for label in _TRAILING_LABELS:
            if lowered.endswith(" " + label) or lowered == label:
                result = result[: len(result) - len(label)].strip(" 	-–—|·,;:")
                changed = True
                lowered = result.lower()
            if lowered.startswith(label + " "):
                result = result[len(label):].strip(" 	-–—|·,;:")
                changed = True
                lowered = result.lower()
        if not changed:
            break
    return result


def clean_title(text: str, name: str = "") -> str:
    """Reduce a person's text to a job title, or to nothing.

    The value is cut into segments at every card boundary, and the *first
    segment that reads as a title* is the answer. Taking the first segment
    outright was wrong: pages routinely print a heading above the person, so
    "Investor Relations Contact   Director of Investor Relations" has the label
    first and the job second, and keeping the label lost the title entirely.

    Args:
        text: The remainder from :func:`split_contact_text`, or a raw value.
        name: The person's stored full name, when the caller knows it. A title
            containing the person's own name has been concatenated with
            something else, and is cut there.

    Returns:
        The title, or ``""`` when no segment is one. Returning nothing is a
        real answer: a field left empty is honest, and a paragraph of prose
        stored as a job title is not.
    """
    plain = split_contact_text(text).remainder

    # Navigation words are furniture wherever they sit.
    if any(phrase in plain.lower() for phrase in _NAVIGATION):
        plain = re.sub(
            "|".join(re.escape(p) for p in _NAVIGATION), "  ", plain, flags=re.I
        )

    for segment in _segments(plain):
        for part in _split_at_name(segment, name):
            candidate = _refine_title(part)
            if candidate:
                return candidate

    return ""


def _refine_title(segment: str) -> str:
    """Reduce one segment to a job title, or reject it.

    Args:
        segment: One card's worth of text, already split at any name.

    Returns:
        The title, or ``""``.
    """
    # Order matters: the tail trim needs to see the digits that prove the tail
    # is an address, so it runs before they are cut away.
    candidate = _cut_at_prose(segment)
    candidate = _trim_tail(candidate)
    candidate = _cut_at_address(candidate)
    candidate = _cut_at_second_role(candidate)
    candidate = _strip_labels(candidate).strip(" \t-–—|·,;:&")
    candidate = re.sub(r"\s{2,}", " ", candidate).strip()

    candidate = _drop_dangling(candidate)
    if looks_like_a_title(candidate):
        return candidate

    # The job may still be in there, just not at the front.
    window = _drop_dangling(_title_around_role(segment))
    return window if looks_like_a_title(window) else ""


def _drop_dangling(text: str) -> str:
    """Remove a connective left hanging off the end by a cut.

    Args:
        text: The candidate title.

    Returns:
        The text without a trailing "of", "and", "the" and the like.
    """
    parts = (text or "").split()
    while parts and parts[-1].strip(",.;:&").lower() in _ROLE_CONJUNCTIONS | {"the"}:
        parts.pop()
    return " ".join(parts).strip(" \t-–—|·,;:&")


def looks_like_a_title(text: str) -> bool:
    """Whether a phrase names a job.

    The test is that some word in it is a job word. That is deliberately looser
    than the role vocabulary in :mod:`zerocredit.extract`, which answers a
    different question -- *which* of a few named roles this is. Using that as
    the gate cleared every title it did not have a bucket for, "Plant Manager"
    and "Quality Manager" among them, which would have destroyed real data to
    fix a formatting problem.

    Args:
        text: The candidate.

    Returns:
        Whether it reads as a job title.

    Examples:
        >>> looks_like_a_title("Plant Manager")
        True
        >>> looks_like_a_title("Building Trust Through Approachability")
        False
    """
    from cloud.intel.vendor.zc_extract import role_of

    candidate = (text or "").strip()
    if not candidate or len(candidate) > MAX_TITLE_CHARS:
        return False

    words = re.findall(r"[A-Za-z][A-Za-z.\-]*", candidate.lower())
    if not words or len(words) > 12:
        return False

    return bool(
        set(w.strip(".-") for w in words) & _TITLE_WORDS
        or role_of(candidate)
    )


def clean_name(text: str) -> str:
    """Reduce a value to a person's name, or to nothing.

    Args:
        text: The candidate.

    Returns:
        The name, or ``""``. A slogan, a job title or a department is rejected:
        "Building Trust Through Approachability" and "International Fulfillment
        Specialist" both reached the contact table as names, and neither is one.
    """
    plain = split_contact_text(text).remainder
    plain = _cut_at_boundary(plain).strip(" \t-–—|·,;:")

    if not (3 <= len(plain) <= MAX_NAME_CHARS):
        return ""

    words = plain.split()
    if not (2 <= len(words) <= 4):
        return ""

    lowered = {word.lower().strip(".,'’-") for word in words}
    if lowered & _NOT_NAME_WORDS:
        return ""

    if not all(re.match(r"^[A-Z][A-Za-z'’.\-]*$", word) for word in words):
        return ""

    return plain


#: Words that disqualify a candidate from being read as a person's name here.
#: Stricter than :func:`clean_name`'s own test, because this function is
#: *recovering* a name from a field that was never meant to hold one, and a
#: wrong name is worse than none: "Bert Reeves Vice" and "Aehr Test Systems"
#: both passed the looser check.
_NOT_A_RECOVERED_NAME: Final[frozenset] = frozenset({
    "vice", "senior", "junior", "chief", "head", "global", "regional",
    "national", "corporate", "group", "systems", "technologies", "technology",
    "holdings", "partners", "associates", "enterprises", "labs",
    "laboratories", "international", "inc", "llc", "ltd", "corp", "co",
    "test", "contact", "contacts", "team", "office", "us",
})


def name_in(text: str, company: str = "") -> str:
    """The first personal name inside a blob, if one is clearly there.

    The mirror of the title recovery. The original run sometimes stored a job
    title in ``full_name`` -- "CHIEF FINANCIAL OFFICER" is a real stored value
    -- and put the person's actual name in ``job_title``: "Matthew Jeffries VP".
    Clearing the bad name without looking for the good one would lose somebody
    the page named plainly.

    Deliberately reluctant. Only the first few words are considered, a
    candidate containing a job word or a corporate word is refused, and a
    candidate overlapping the employer's own name is refused -- that rule is
    what stops "Contacts: Aehr Test Systems Vernon Roger" from yielding "Aehr
    Test". When in doubt this returns nothing, because a wrong name is worse
    than a missing one.

    Args:
        text: The text to search.
        company: The employer's name, whose words cannot be a person's here.

    Returns:
        The name, or ``""``.

    Examples:
        >>> name_in("Matthew Jeffries VP")
        'Matthew Jeffries'
        >>> name_in("Vice President of Sales")
        ''
    """
    forbidden = set(_NOT_A_RECOVERED_NAME) | set(_TITLE_WORDS)
    forbidden |= {
        word.strip(",.").lower()
        for word in (company or "").split()
        if len(word) > 2
    }

    words = re.findall(r"\S+", text or "")
    for start in range(min(len(words), 4)):
        for size in (3, 2):
            if start + size > len(words):
                continue

            chunk = words[start:start + size]
            if any(w.strip(" ,.;:|-").lower() in forbidden for w in chunk):
                continue

            candidate = " ".join(chunk).strip(" ,.;:|-")
            cleaned = clean_name(candidate)
            if cleaned:
                return cleaned
    return ""


def split_name(name: str) -> Tuple[str, str]:
    """Split a personal name into the first and last parts.

    Args:
        name: The full name.

    Returns:
        ``(first, last)``. Both are taken from the name as given; nothing is
        supplied that the name did not contain.
    """
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def clean_department(text: str) -> str:
    """Tidy a department value.

    Args:
        text: The candidate.

    Returns:
        The department, or ``""``. Departments are assigned from a fixed
        vocabulary upstream, so this only guards against contamination.
    """
    plain = _strip_labels(split_contact_text(text).remainder)
    plain = re.sub(r"\s{2,}", " ", plain).strip(" \t-–—|·,;:")
    return plain if 2 <= len(plain) <= 60 else ""


def clean_seniority(text: str) -> str:
    """Tidy a seniority band.

    Args:
        text: The candidate.

    Returns:
        The band, or ``""``.
    """
    plain = _strip_labels(split_contact_text(text).remainder)
    plain = re.sub(r"\s{2,}", " ", plain).strip(" \t-–—|·,;:")
    return plain if 2 <= len(plain) <= 30 else ""


def clean_company_name(text: str) -> str:
    """Tidy a company name.

    Args:
        text: The candidate.

    Returns:
        The name with markup, addresses and numbers removed, or ``""``.
    """
    plain = split_contact_text(text).remainder
    plain = _cut_at_boundary(plain).strip(" \t-–—|·,;:")
    plain = re.sub(r"\s{2,}", " ", plain).strip()
    return plain if 2 <= len(plain) <= 120 else ""
