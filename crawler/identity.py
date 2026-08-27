"""Give a posting a name that survives to next Friday.

Weekly comparison asks one question of every job: *have I seen this before?* The
answer has to hold up across a week in which the board rewrote its URLs, an
aggregator appended a tracking parameter, and the company moved from Paylocity
to Workday. Getting it wrong is expensive in both directions — a job whose
identity drifts is reported as new every single week, and two jobs that collapse
onto one identity means a real opening silently disappears.

So identity is derived from the strongest evidence a posting offers, and the
weaker evidence is kept alongside it rather than thrown away::

    >>> from crawler.identity import job_identity
    >>> first = job_identity("Acme", "https://boards.greenhouse.io/acme/jobs/4012345",
    ...                      "Senior DevOps Engineer", platform="Greenhouse")
    >>> second = job_identity("Acme", "https://boards.greenhouse.io/acme/jobs/4012345?gh_src=x",
    ...                       "Senior DevOps Engineer", platform="Greenhouse")
    >>> first.job_uid == second.job_uid
    True
    >>> first.basis
    'platform-id'

The ladder, strongest first:

1. **A platform requisition id** — supplied by the adapter, or recovered from
   the URL by :func:`job_id_from_url`. This is what the ATS itself calls the
   posting, so it outlives URL rewrites and even a change of careers domain.
2. **The canonical URL** — :func:`utils.urlkey.url_key`, so tracking parameters
   and ``www.`` cannot manufacture a new job.
3. **Company, title and location** — the last resort, for boards that publish a
   posting with no stable link at all.

Every identity carries all three keys, not just the winner. That is what lets
:mod:`crawler.weekly_diff` re-link a posting whose URL changed but whose
requisition id did not, instead of reporting one closure and one opening.

Identity is always scoped to the company. Requisition id ``12345`` exists at
hundreds of companies, and two firms sharing an ATS tenant must never collide.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from utils.names import company_key as _company_key
from utils.urlkey import url_key

__all__ = [
    "ID_QUERY_PARAMETERS",
    "JobIdentity",
    "job_id_from_url",
    "job_identity",
    "normalise_title",
]

#: Query parameters that hold the posting's own identity. Distinct from the
#: tracking parameters :mod:`utils.urlkey` removes: these are the job.
ID_QUERY_PARAMETERS: Final[Tuple[str, ...]] = (
    "gh_jid",  # Greenhouse, when the board is hosted on the company's domain
    "jobid",
    "job_id",
    "applytojob",  # UKG Ready and Asure, on saashr.com / entertimeonline.com
    "opportunityid",  # UltiPro / UKG
    "requisitionid",
    "reqid",
    "req_id",
    "postingid",
    "posting_id",
    "vacancyid",
    "jobreqid",
    "jobpostid",
    "positionid",
)

#: A UUID, which Lever, Ashby, UltiPro and Comeet all use as the posting id.
_UUID: Final[re.Pattern[str]] = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)

#: A requisition code as Workday and SuccessFactors spell it, e.g. ``R-12345``,
#: ``JR0098765``, ``R2026-1792-1``. Anchored to a segment boundary so it cannot
#: match inside a word of the job title that is also in the URL slug.
#:
#: The trailing ``(?:-\d+)*`` is load-bearing. Workday tenants number
#: requisitions ``R2026-1792-1``, and a pattern that stopped at ``R2026`` gave
#: every posting opened in 2026 the same identity — 1,241 postings on the
#: reference sheet collapsed onto 86 keys before this was widened.
_REQUISITION: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[_/-])((?:JR|R|REQ|JOB)-?\d{3,}(?:-\d+)*)(?:$|[_/-])",
    re.IGNORECASE,
)

#: A numeric segment sitting directly after a segment that says "job". This is
#: how iCIMS spells a posting — ``/jobs/12345/devops-engineer/job`` — and the
#: id is neither long enough nor final enough for the rules below to see it.
#: Requiring the preceding keyword is what makes three digits safe to accept.
_PATH_JOB_ID: Final[re.Pattern[str]] = re.compile(
    r"/(?:jobs?|postings?|positions?|vacanc(?:y|ies)|openings?|requisitions?)/(\d{3,})(?:/|$)",
    re.IGNORECASE,
)

#: A long run of digits *opening* the final path segment — SmartRecruiters'
#: 15-digit ids, Greenhouse's board paths. Six digits minimum, so a year or a
#: page number cannot be mistaken for an identifier.
#:
#: Anchoring to the start of the segment, and refusing a following ``.``, is
#: what keeps a *tenant* number out. UKG Ready and Asure serve every posting
#: from ``/ta/6173477.careers?ApplyToJob=...``: the path number identifies the
#: employer, not the job, and matching it merged 2,043 postings onto 76 keys.
_NUMERIC_ID: Final[re.Pattern[str]] = re.compile(r"^(\d{6,})(?:-|$)")

#: How many trailing path segments may hold a bare UUID. Lever and Ashby put it
#: last; Paylocity puts it second to last, before the company slug. Beyond that
#: a UUID is something else — ``clientapi.gcs-web.com/data/<uuid>/news/13331``
#: keys a *feed*, and honouring it merged 1,139 items onto 152 keys.
_UUID_SEGMENT_DEPTH: Final[int] = 2

#: Anything that is not a letter or a digit, for folding a title or location.
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

#: How the derived keys are labelled, in the order they are preferred.
_BASIS_PLATFORM_ID: Final[str] = "platform-id"
_BASIS_URL: Final[str] = "url"
_BASIS_CONTENT: Final[str] = "content"


@dataclass(frozen=True)
class JobIdentity:
    """Everything known about which posting this is.

    Attributes:
        job_uid: The primary key. Stable across weeks for one posting.
        basis: Which evidence produced ``job_uid`` — ``"platform-id"``,
            ``"url"`` or ``"content"``. Recorded so a run can report how much
            of its identity rests on inference.
        company_key: The company this posting is scoped to.
        job_id: The platform's own requisition id, or ``""`` if none was found.
        url_key: The canonical URL, or ``""``.
        content_key: Hash of company, title and location. Always present.
    """

    job_uid: str
    basis: str
    company_key: str
    job_id: str
    url_key: str
    content_key: str

    @property
    def is_inferred(self) -> bool:
        """Whether identity rests on title and location rather than a real key.

        Returns:
            ``True`` when neither a requisition id nor a URL was available, so
            a retitled posting will read as a closure plus an opening.
        """
        return self.basis == _BASIS_CONTENT


def _digest(*parts: str) -> str:
    """Hash the parts into one stable hexadecimal key.

    Args:
        *parts: Components of the identity, in a fixed order.

    Returns:
        The hexadecimal SHA-1 digest. SHA-1 is used as a checksum for
        deduplication, never as a security primitive.
    """
    joined = "\x00".join(part for part in parts)
    return hashlib.sha1(joined.encode("utf-8"), usedforsecurity=False).hexdigest()


def normalise_title(title: str) -> str:
    """Fold a job title to a comparable token.

    Args:
        title: The posting title as advertised.

    Returns:
        The title lowercased with punctuation and spacing removed, so
        ``"Senior DevOps Engineer (Remote)"`` and
        ``"Senior DevOps Engineer - Remote"`` agree.
    """
    return _NON_ALNUM.sub("", str(title or "").lower())


def job_id_from_url(url: str) -> str:
    """Recover the platform's requisition id from a posting URL.

    Adapters that already know the id should pass it in rather than rely on
    this. It exists because most of the sixty adapters return only a URL, and a
    URL that embeds a requisition id yields a far more stable identity than the
    URL as a whole.

    Args:
        url: The posting's URL.

    Returns:
        The identifier, or ``""`` when the URL embeds none. Deterministic: the
        same URL always yields the same answer, which is what makes it safe to
        key on.
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""

    # 1. An explicit identifier in the query string is unambiguous, and is the
    #    only place several vendors put one. Checked before anything in the
    #    path, where the same URL usually also carries a *tenant* number.
    for name, value in parse_qsl(parts.query, keep_blank_values=False):
        if name.strip().lower() in ID_QUERY_PARAMETERS and value.strip():
            return value.strip()

    # 2. A UUID in the fragment, where single-page portals keep their route.
    if parts.fragment:
        found = _UUID.search(parts.fragment)
        if found:
            return found.group(0).lower()

    segments = [segment for segment in parts.path.split("/") if segment]

    # 3. A UUID near the end of the path. Depth-limited: a UUID further up
    #    identifies a board, a feed or a tenant rather than a posting.
    for segment in segments[-_UUID_SEGMENT_DEPTH:]:
        found = _UUID.fullmatch(segment)
        if found:
            return found.group(0).lower()

    # 4. A requisition code in the vendors' house style, anywhere in the route.
    for haystack in (parts.fragment, parts.path):
        if not haystack:
            continue
        found = _REQUISITION.search(haystack)
        if found:
            return found.group(1).upper()

    # 5. A number introduced by a "job" segment. The keyword is what makes a
    #    short number trustworthy: `/jobs/12345/devops/job` is iCIMS.
    for haystack in (parts.fragment, parts.path):
        if not haystack:
            continue
        found = _PATH_JOB_ID.search(haystack)
        if found:
            return found.group(1)

    # 6. A long numeric run opening the final path segment. Weakest rule, so it
    #    goes last and is anchored hard — see _NUMERIC_ID.
    if segments:
        found = _NUMERIC_ID.match(segments[-1])
        if found:
            return found.group(1)

    return ""


def job_identity(
    company_name: str,
    job_url: str,
    job_title: str,
    location: str = "",
    platform: str = "",
    job_id: str = "",
    website: str = "",
    career_url: str = "",
) -> JobIdentity:
    """Derive the stable identity of one posting.

    Args:
        company_name: Company as named in the master list.
        job_url: Absolute URL of the posting. May be empty.
        job_title: Posting title.
        location: Location as published, used only for the content fallback.
        platform: ATS label, which scopes a requisition id so that id ``12345``
            on Workday and on Paylocity cannot collide.
        job_id: The requisition id, when the adapter knows it. Recovered from
            ``job_url`` when not supplied.
        website: The company's website, for scoping the identity.
        career_url: The company's board, used to scope when no website is known.

    Returns:
        The identity, including every key that could be derived.
    """
    company = _company_key(company_name, website, career_url) or f"name:{normalise_title(company_name)}"

    canonical = url_key(job_url)
    identifier = str(job_id or "").strip() or job_id_from_url(job_url)

    content = _digest(company, normalise_title(job_title), normalise_title(location))

    if identifier:
        basis = _BASIS_PLATFORM_ID
        # The platform scopes the id: two vendors number their requisitions
        # independently, and a company can be mid-migration between them.
        uid = _digest(company, str(platform or "").strip().lower(), identifier.lower())
    elif canonical:
        basis = _BASIS_URL
        # The title joins the key here, and only here. A board with no
        # requisition ids routinely links several postings to one page — its
        # own listing page, or a shared detail view — and keying on the URL
        # alone silently merged them, losing every posting but the first.
        #
        # It costs almost nothing: a URL specific enough to identify a posting
        # has the title in its slug already, so an edited title moves both
        # parts of the key together. Postings that *do* have a stable id are
        # unaffected, because they never reach this branch.
        uid = _digest(company, canonical, normalise_title(job_title))
    else:
        basis = _BASIS_CONTENT
        uid = content

    return JobIdentity(
        job_uid=uid,
        basis=basis,
        company_key=company,
        job_id=identifier,
        url_key=canonical,
        content_key=content,
    )


def identity_of(job: object, website: str = "", career_url: str = "") -> Optional[JobIdentity]:
    """Derive the identity of a :class:`~models.job.Job`.

    A convenience over :func:`job_identity` that reads the fields off a job
    record, tolerating the optional version 3 fields being absent.

    Args:
        job: A job record.
        website: The company's website, for scoping.
        career_url: The company's board, for scoping.

    Returns:
        The identity, or ``None`` if ``job`` is not a job record.
    """
    title = getattr(job, "job_title", None)
    if title is None:
        return None

    return job_identity(
        company_name=getattr(job, "company_name", ""),
        job_url=getattr(job, "job_url", ""),
        job_title=title,
        location=getattr(job, "location", ""),
        platform=getattr(job, "platform", ""),
        job_id=getattr(job, "job_id", ""),
        website=website,
        career_url=career_url or getattr(job, "career_page_url", ""),
    )
