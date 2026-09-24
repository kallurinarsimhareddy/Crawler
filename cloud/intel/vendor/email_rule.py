# VENDORED from CareerCrawler-seamless zerocredit/email_rule.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""The one function that decides whether a contact may be exported.

The business rule is short -- a row without a real email address is not worth
sending -- and the dangerous part is not the rule but the pressure it creates.
Once "no email, no row" is the standard, every near-miss becomes a temptation:
the domain is right, the name is right, ``first.last@`` is right most of the
time. Filling those in would raise the row count and destroy the dataset, and
the damage would not show up until a mail server started rejecting the sending
domain.

So the rule lives in exactly one place, it only ever *reads*, and there is no
counterpart anywhere in this package that constructs an address::

    >>> has_real_email({"email": "jane.doe@acme.com"})
    True
    >>> has_real_email({"email": "info@acme.com"})
    False
    >>> has_real_email({"email": ""})
    False

What disqualifies an address, and why each matters:

**Empty or whitespace.** Obvious, and the commonest case: 97.8% of the contacts
found by the first full run have nothing here.

**Syntactically invalid.** A fragment scraped out of markup is not an address.
The check is deliberately stricter than the RFC: a local part that starts or
ends with a dot, a domain with no dot, a single-character TLD -- all rejected,
because every one of them is far more likely to be a parsing accident than a
real mailbox.

**Placeholders.** ``example.com``, ``yourcompany.com``, ``email@email.com``,
``name@domain.com`` and the rest are on template pages by the thousand.

**Role addresses.** ``info@``, ``sales@``, ``hr@``, ``admin@``. These are real
mailboxes and often useful -- but they belong to a department, not to the
named executive on the page, and writing one into a person's row asserts
something the source never said. :func:`company_contact_address` exists for the
case where a role address is wanted *as* a company field.

**Anything this code produced.** There is no way to produce one; that is the
point. :func:`looks_generated` is a belt-and-braces check against a future edit
introducing a pattern-builder, and a test asserts the package exposes no
function that could be one.

Nothing here is a confidence score. An address either was published where this
code could read it, or it was not.
"""

from __future__ import annotations

import re
from urllib.parse import unquote as _UNQUOTE
from typing import Any, Final, Iterable, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "EMAIL_FIELDS",
    "PLACEHOLDER_DOMAINS",
    "company_contact_address",
    "name_explains_address",
    "is_departmental_local",
    "first_real_email",
    "has_real_email",
    "is_valid_syntax",
    "looks_generated",
    "real_emails_of",
]

#: The fields on a contact that may hold an address, in preference order. The
#: first that holds a real one becomes ``Email 1`` on export.
EMAIL_FIELDS: Final[Tuple[str, ...]] = (
    "email",
    "email_2",
    "contact_email",
    "personal_email",
)

#: Stricter than the RFC on purpose: a local part that begins or ends with a
#: dot, a domain without a dot, or a one-letter TLD is far more likely to be a
#: parsing accident than a mailbox somebody reads.
_VALID: Final[re.Pattern[str]] = re.compile(
    r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,24}$"
)

#: Domains that appear on template and placeholder pages. An address at one of
#: these is documentation, not a person.
PLACEHOLDER_DOMAINS: Final[frozenset] = frozenset({
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "yourcompany.com", "company.com", "email.com", "test.com", "sample.com",
    "mysite.com", "website.com", "sentry.io", "wixpress.com", "localhost",
})

#: Local parts that are placeholders whatever the domain.
_PLACEHOLDER_LOCALS: Final[frozenset] = frozenset({
    "email", "youremail", "your-email", "name", "yourname", "firstname",
    "lastname", "user", "username", "someone", "somebody", "example",
    "test", "sample", "placeholder", "address", "emailaddress",
})

#: Departmental mailboxes. Real, often useful, never a named person's.
_ROLE_LOCALS: Final[frozenset] = frozenset({
    "info", "sales", "support", "hello", "contact", "contactus", "admin",
    "office", "enquiries", "enquiry", "inquiries", "inquiry", "help",
    "helpdesk", "service", "services", "customerservice", "marketing",
    "press", "media", "pr", "careers", "jobs", "recruiting", "recruitment",
    "hr", "humanresources", "webmaster", "postmaster", "noreply", "no-reply",
    "donotreply", "privacy", "legal", "billing", "accounts", "accounting",
    "accountspayable", "ap", "ar", "orders", "order", "quotes", "quote",
    "rfq", "purchasing", "procurement", "shipping", "returns", "warranty",
    "team", "general", "mail", "email", "reception", "frontdesk", "newsletter",
    "subscribe", "unsubscribe", "abuse", "security", "compliance",
})


def _clean(value: Any) -> str:
    """Reduce a stored value to a comparable address.

    Args:
        value: Anything held in an email field.

    Returns:
        The trimmed, lowercased text. Never ``None``.
    """
    if value is None:
        return ""

    text = str(value)

    # Percent-decode on read. Values stored before the extraction bug was fixed
    # still carry their encoding -- "%20bblock@diamondmowers.com" is a real one
    # -- and "%" is RFC-legal in a local part, so such a value passes a syntax
    # check and bounces. Decoding here repairs those rows at the point of use
    # rather than requiring a migration, and is a no-op for a clean address.
    for _ in range(2):
        try:
            decoded = _UNQUOTE(text)
        except Exception:  # noqa: BLE001 - a bad escape must not raise here
            break
        if decoded == text:
            break
        text = decoded

    return text.strip().strip("<>,;").lower()


def is_valid_syntax(address: Any) -> bool:
    """Whether a value is shaped like a deliverable address.

    Args:
        address: The candidate.

    Returns:
        Whether it passes the strict pattern. Length is capped because a
        multi-kilobyte "address" is a parsing failure, not a mailbox.
    """
    text = _clean(address)
    return bool(text) and len(text) <= 254 and bool(_VALID.match(text))


def is_placeholder(address: Any) -> bool:
    """Whether an address is template text rather than a real mailbox.

    Args:
        address: The candidate.

    Returns:
        Whether its domain or local part marks it as a placeholder.
    """
    text = _clean(address)
    if "@" not in text:
        return False

    local, _, domain = text.partition("@")
    local = re.sub(r"[._\-+]", "", local)
    return domain in PLACEHOLDER_DOMAINS or local in _PLACEHOLDER_LOCALS


def is_role_address(address: Any) -> bool:
    """Whether an address belongs to a department rather than a person.

    Args:
        address: The candidate.

    Returns:
        Whether the local part names a role mailbox. Both the whole local part
        and its first token are checked, so ``no-reply`` and ``hr.team`` are
        both caught while ``jane.doe`` is not.
    """
    text = _clean(address)
    if "@" not in text:
        return False

    local = text.split("@", 1)[0]
    collapsed = re.sub(r"[._\-+]", "", local)
    head = re.split(r"[._\-+]", local)[0]
    return (
        local in _ROLE_LOCALS
        or collapsed in _ROLE_LOCALS
        or head in _ROLE_LOCALS
    )


#: Role words long enough to be recognised at the *end* of a local part.
#: "cablesales@", "partsteam@", "italyorders@" and "technicalsupport@" are all
#: departmental, and all passed the whole-local-part check.
#:
#: Short role words are excluded deliberately. "hr" would reject every Bahr,
#: Lohr, Mohr and Rohr on the roster, and losing a real person is worse than
#: keeping a departmental address.
_SUFFIX_ROLES: Final[frozenset] = frozenset({
    "sales", "info", "support", "service", "services", "contact", "contactus",
    "team", "admin", "office", "careers", "jobs", "marketing", "orders",
    "quotes", "help", "helpdesk", "inquiries", "enquiries", "purchasing",
    "billing", "accounting", "reception", "webmaster", "customerservice",
})


def _explained_by_name(local: str, first_name: str, last_name: str) -> bool:
    """Whether a local part is accounted for by the person's own name.

    The guard on the suffix rule. "rosales" ends in "sales", and Rosales is a
    surname: ``jrosales@`` belongs to Juan Rosales and must not be discarded as
    a sales mailbox.

    Args:
        local: The local part, with separators already removed.
        first_name: The person's first name.
        last_name: The person's last name.

    Returns:
        Whether the person's own name appears in the local part.
    """
    for part in (last_name, first_name):
        cleaned = re.sub(r"[^a-z]", "", (part or "").lower())
        if len(cleaned) >= 3 and cleaned in local:
            return True
    return False


def name_explains_address(address: Any, first_name: str = "",
                          last_name: str = "") -> bool:
    """Whether an address's local part is accounted for by a person's name.

    Not a rule about whether an address is *real* -- it is published either way.
    It is a rule about whether it is **theirs**, and it exists because a block
    on a team page can enclose everybody: one person's element yielded fifteen
    addresses, of which one was theirs.

    Args:
        address: The candidate.
        first_name: The person's first name.
        last_name: The person's last name.

    Returns:
        Whether the local part contains their first or last name.

    Examples:
        >>> name_explains_address("carsonbr@acme.com", "Carson", "Brinkley")
        True
        >>> name_explains_address("jbisk@acme.com", "Gerry", "Clothier")
        False
    """
    text = _clean(address)
    if "@" not in text:
        return False

    local = re.sub(r"[._\-+]", "", text.split("@", 1)[0])
    return _explained_by_name(local, first_name, last_name)


def is_departmental_local(address: Any, first_name: str = "",
                          last_name: str = "") -> bool:
    """Whether a local part is a role word with something stuck to it.

    :func:`is_role_address` matches a local part that *is* a role word, or whose
    first dotted token is one. That misses the compounds, which reached the
    export attached to named people: ``cablesales@``, ``partsteam@``,
    ``italyorders@``, ``technicalsupport@``, ``contacthr@``. The role word can
    be at either end.

    Args:
        address: The candidate.
        first_name: The person's first name, when known.
        last_name: The person's last name, when known.

    Returns:
        Whether the address is a departmental mailbox wearing a longer name.
        A local part the person's own name accounts for is never one, which is
        what keeps ``jrosales@`` with Juan Rosales.
    """
    text = _clean(address)
    if "@" not in text:
        return False

    local = re.sub(r"[._\-+]", "", text.split("@", 1)[0])
    for role in _SUFFIX_ROLES:
        if len(local) <= len(role):
            continue
        if local.endswith(role) or local.startswith(role):
            return not _explained_by_name(local, first_name, last_name)
    return False


def looks_generated(address: Any, first_name: str = "", last_name: str = "") -> bool:
    """Whether an address looks like it was built from a person's name.

    This does **not** reject every ``first.last@`` address -- plenty of real,
    published ones take that form, and discarding them would throw away good
    data. It exists so that a caller can tell the two cases apart, and so that
    a future edit which starts *constructing* addresses is caught by a test
    rather than by a bounce report.

    Args:
        address: The candidate.
        first_name: The person's first name, when known.
        last_name: The person's last name, when known.

    Returns:
        Whether the local part is exactly one of the well-known constructions
        of this person's name.
    """
    text = _clean(address)
    first, last = (first_name or "").strip().lower(), (last_name or "").strip().lower()
    if "@" not in text or not (first and last):
        return False

    local = text.split("@", 1)[0]
    return local in {
        f"{first}.{last}", f"{first}_{last}", f"{first}-{last}", f"{first}{last}",
        f"{first[0]}{last}", f"{first[0]}.{last}", f"{first}{last[0]}",
        f"{last}.{first}", f"{last}{first}", f"{last}{first[0]}",
    }


def real_emails_of(contact: Mapping[str, Any]) -> Tuple[str, ...]:
    """Every address on a contact that may be exported.

    Args:
        contact: A ``zc_contact`` row, or any mapping with the email fields.

    Returns:
        The qualifying addresses, de-duplicated, in :data:`EMAIL_FIELDS` order.
        A value that is empty, malformed, a placeholder or a role mailbox is
        not returned -- and nothing is ever synthesised to fill the gap.
    """
    found: list = []
    seen: Set[str] = set()

    for field in EMAIL_FIELDS:
        address = _clean(contact.get(field))
        if not address or address in seen:
            continue
        if not is_valid_syntax(address):
            continue
        if is_placeholder(address) or is_role_address(address):
            continue
        if is_departmental_local(
            address,
            str(contact.get("first_name") or ""),
            str(contact.get("last_name") or ""),
        ):
            continue
        seen.add(address)
        found.append(address)

    return tuple(found)


def has_real_email(contact: Mapping[str, Any]) -> bool:
    """Whether a contact may appear in the final export.

    The single authoritative gate. A contact passes if and only if at least one
    of its email fields holds an address that was actually sourced from a page,
    is syntactically valid, is not a placeholder, and is not a departmental
    mailbox.

    Args:
        contact: A ``zc_contact`` row, or any mapping with the email fields.

    Returns:
        Whether the contact qualifies.
    """
    return bool(real_emails_of(contact))


def first_real_email(contact: Mapping[str, Any]) -> str:
    """The address to export as ``Email 1``.

    Args:
        contact: The contact.

    Returns:
        The preferred address, or ``""`` when the contact does not qualify.
    """
    addresses = real_emails_of(contact)
    return addresses[0] if addresses else ""


def company_contact_address(addresses: Iterable[Any]) -> str:
    """Pick a departmental address to use as a *company* field.

    Role addresses are excluded from people, but they are legitimate company
    contact details. This is the only place one is allowed, and it is never
    written into a person's email column.

    Args:
        addresses: Candidate addresses found on the company's pages.

    Returns:
        The best departmental address, or ``""``. ``info@`` and ``contact@``
        are preferred over ``billing@`` because they are what a company
        publishes as its front door.
    """
    ranked = ("info", "contact", "contactus", "hello", "enquiries", "inquiries",
              "office", "general", "sales", "support")
    best, best_rank = "", len(ranked)

    for candidate in addresses:
        text = _clean(candidate)
        if not is_valid_syntax(text) or is_placeholder(text):
            continue
        if not is_role_address(text):
            continue
        head = re.split(r"[._\-+]", text.split("@", 1)[0])[0]
        rank = ranked.index(head) if head in ranked else len(ranked) - 1
        if rank < best_rank:
            best, best_rank = text, rank

    return best
