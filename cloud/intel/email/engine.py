"""Layered email evidence: everything that can be learned about an address for free,
before (and alongside) a paid mailbox check.

The user-facing result stays simple — :func:`final_status` maps every internal
result to ``VALID``, ``INVALID`` or ``NOT_VERIFIED``. Everything in this module is
*evidence* that explains that result; none of it can make an address VALID on its
own. Only a mailbox-level verifier in :data:`VERIFIERS` (today: EmailListVerify)
can.

Layers (each is a pure function or a small injectable class, so tests never touch
the network):

1. **Format** — :func:`check_format`: syntax, IDN → punycode, placeholders, typos.
2. **Domain / DNS** — :func:`inspect_domain`: NXDOMAIN / SERVFAIL / timeout, MX,
   RFC 7505 null MX, A/AAAA fallback.
3. **Security records** — SPF and DMARC (supporting signals only; missing
   records never make an address invalid).
4. **SMTP preflight** — :class:`SmtpPreflight`, opt-in: connect, EHLO, STARTTLS
   capability, QUIT. It never sends MAIL FROM or RCPT TO, so it probes no mailbox
   and says nothing about one.
5. **Catch-all** — learned from a verifier's answer (``ok_for_all`` /
   ``accept_all``) and cached per domain. There is deliberately no RCPT-based
   probing of random mailboxes.
6. **Risk** — role, disposable, free provider, no-reply, high-abuse TLDs.
7. **Contact** — :func:`contact_signals`: does the address belong to the named
   person at the named company, or is it a shared inbox?
8. **Public evidence** — :class:`PublicEvidenceFinder`, opt-in: the exact address
   on the company's own public pages (robots.txt respected, SSRF-safe fetcher, no
   search engines, no logins). Supporting evidence, never proof of a mailbox.
9. **Verifiers** — :data:`VERIFIERS`, the provider slots that may answer VALID.

Nothing here logs an address.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from cloud.intel.core.normalize import FREE_EMAIL_DOMAINS

__all__ = ["DnsAnswer", "DnsPythonClient", "FINAL_STATUSES", "PublicEvidenceFinder", "SmtpPreflight", "VERIFIERS",
           "catch_all_from", "check_format", "contact_signals", "detect_contact_columns", "evidence_summary",
           "final_status", "inspect_domain", "mailbox_verification", "risk_signals", "sha256", "source_label",
           "to_ascii_email", "INTERNAL_BY_FINAL", "PAID_CANDIDATE_STATUSES"]

#: The only statuses a user sees.
FINAL_STATUSES = ("VALID", "INVALID", "NOT_VERIFIED")
#: Internal result statuses behind each final one (the store keeps the detailed status).
INTERNAL_BY_FINAL = {
    "VALID": ("VALID",),
    "INVALID": ("INVALID",),
    "NOT_VERIFIED": ("UNKNOWN", "RISKY", "ROLE", "DISPOSABLE", "FREE_PROVIDER"),
}
#: Built-in NOT VERIFIED results a mailbox verifier can still settle. Disposable
#: addresses are left out: a verifier only confirms they are throw-away.
PAID_CANDIDATE_STATUSES = ("UNKNOWN", "ROLE", "FREE_PROVIDER")

#: Mailbox-level verifiers: the only sources that may make an address VALID. A new
#: authorized provider gets an adapter (``check(email) -> ValidationResult``) and a slot here.
VERIFIERS: Dict[str, Dict[str, Any]] = {
    "emaillistverify": {"label": "EmailListVerify", "paid": True},
}

_ELV_CATCH_ALL = {"ok_for_all", "accept_all"}


def sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Layer 1: format
# ---------------------------------------------------------------------------

#: Frequent misspellings of big mailbox domains -> the intended domain.
_DOMAIN_TYPOS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gamil.com": "gmail.com", "gnail.com": "gmail.com",
    "gmail.co": "gmail.com", "gmail.con": "gmail.com", "gmaill.com": "gmail.com", "hotmial.com": "hotmail.com",
    "hotmal.com": "hotmail.com", "hotmail.co": "hotmail.com", "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com",
    "outlok.com": "outlook.com", "outllook.com": "outlook.com", "iclod.com": "icloud.com",
}
_TLD_TYPOS = {"con": "com", "cmo": "com", "ocm": "com", "comm": "com", "nett": "net", "ogr": "org"}


def to_ascii_email(email: str) -> Tuple[str, bool]:
    """``(address with an ASCII/punycode domain, whether the domain was internationalized)``."""
    text = (email or "").strip()
    local, sep, domain = text.rpartition("@")
    if not sep or not domain or domain.isascii():
        return text, False
    try:
        return f"{local}@{domain.encode('idna').decode('ascii')}", True
    except UnicodeError:
        return text, True


def check_format(email: str) -> Dict[str, Any]:
    """Layer 1. ``ok`` is the syntax verdict; ``issues`` explains a failure; ``did_you_mean``
    flags a likely typo (a signal only: the DNS layer decides whether the domain works)."""
    from cloud.intel.vendor import email_rule

    raw = email or ""
    issues: List[str] = []
    if raw != raw.strip():
        issues.append("surrounding whitespace")
    text, idn = to_ascii_email(raw.strip())
    text = text.lower()
    local, sep, domain = text.rpartition("@")
    if not sep:
        issues.append("no @")
    elif text.count("@") > 1:
        issues.append("more than one @")
    if any(ch.isspace() for ch in text):
        issues.append("whitespace inside the address")
    if local.startswith(".") or local.endswith("."):
        issues.append("local part starts or ends with a dot")
    if ".." in text:
        issues.append("consecutive dots")
    if sep and ("." not in domain or domain.startswith(".") or domain.endswith(".") or domain.startswith("-")):
        issues.append("malformed domain")
    if re.search(r"[^a-z0-9.!#$%&'*+/=?^_`{|}~@-]", text):
        issues.append("illegal characters")
    blocking = [issue for issue in issues if issue != "surrounding whitespace"]  # trimming fixes that one
    ok = bool(email_rule.is_valid_syntax(text)) and not blocking
    if not ok and not blocking:
        issues.append("not a valid address")
    suggestion = None
    if sep:
        if domain in _DOMAIN_TYPOS:
            suggestion = f"{local}@{_DOMAIN_TYPOS[domain]}"
        else:
            head, _, tld = domain.rpartition(".")
            if tld in _TLD_TYPOS and head:
                suggestion = f"{local}@{head}.{_TLD_TYPOS[tld]}"
    return {"ok": ok, "email": text, "local": local, "domain": domain, "idn": idn, "issues": issues,
            "placeholder": bool(ok and email_rule.is_placeholder(text)), "did_you_mean": suggestion}


# ---------------------------------------------------------------------------
# Layers 2 and 3: DNS, MX, SPF, DMARC
# ---------------------------------------------------------------------------

@dataclass
class DnsAnswer:
    records: List[str] = field(default_factory=list)
    #: None, "nxdomain", "no_answer", "servfail", "timeout" or "error".
    error: Optional[str] = None


_TRANSIENT = ("servfail", "timeout", "error")


class DnsPythonClient:
    """``lookup(name, rtype) -> DnsAnswer`` using dnspython, with short timeouts."""

    def __init__(self, *, timeout: float = 3.0, lifetime: float = 5.0) -> None:
        import dns.resolver

        self._resolver = dns.resolver.Resolver()
        self._resolver.timeout = timeout
        self._resolver.lifetime = lifetime

    def lookup(self, name: str, rtype: str) -> DnsAnswer:
        import dns.exception
        import dns.resolver

        try:
            answer = self._resolver.resolve(name, rtype)
        except dns.resolver.NXDOMAIN:
            return DnsAnswer(error="nxdomain")
        except dns.resolver.NoAnswer:
            return DnsAnswer(error="no_answer")
        except dns.resolver.NoNameservers:
            return DnsAnswer(error="servfail")
        except dns.exception.Timeout:
            return DnsAnswer(error="timeout")
        except dns.exception.DNSException:
            return DnsAnswer(error="error")
        records = []
        for rdata in answer:
            if rtype == "TXT" and hasattr(rdata, "strings"):
                records.append(b"".join(rdata.strings).decode("utf-8", "replace"))
            else:
                records.append(rdata.to_text())
        return DnsAnswer(records=records)


def _parse_mx(records: Sequence[str]) -> List[Tuple[int, str]]:
    out = []
    for record in records:
        parts = record.split()
        if len(parts) == 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1].rstrip(".").lower()))
    return sorted(out)


def inspect_domain(domain: str, dns: Any) -> Dict[str, Any]:
    """Layers 2-3 for one domain. ``mail`` is True (accepts mail: MX, or an A/AAAA fallback),
    False (NXDOMAIN, RFC 7505 null MX, or no MX and no address) or None (a transient DNS
    failure: nothing can be concluded). ``verdict`` is "invalid" only when mail is False."""
    info: Dict[str, Any] = {"domain": domain, "exists": None, "dns_error": None, "mx": [], "null_mx": False,
                            "a_fallback": False, "mail": None, "spf": "unknown", "spf_all": None,
                            "dmarc": "unknown", "dmarc_policy": None}
    mx = dns.lookup(domain, "MX")
    if mx.error == "nxdomain":
        info.update(exists=False, dns_error="nxdomain", mail=False)
    elif mx.error in _TRANSIENT:
        info.update(dns_error=mx.error)
    else:
        info["exists"] = True
        records = _parse_mx(mx.records)
        if records and all(host in ("", ".") for _, host in records):
            info.update(null_mx=True, mail=False)
        elif records:
            info.update(mx=[host for _, host in records if host not in ("", ".")][:5], mail=True)
        else:  # no MX: RFC 5321 implicit MX = the domain's own address
            a, aaaa = dns.lookup(domain, "A"), dns.lookup(domain, "AAAA")
            if a.records or aaaa.records:
                info.update(a_fallback=True, mail=True)
            elif a.error in _TRANSIENT and aaaa.error in _TRANSIENT:
                info.update(dns_error=a.error)
            else:
                info["mail"] = False
    if info["exists"] is not False:
        txt = dns.lookup(domain, "TXT")
        if txt.error in _TRANSIENT:
            info["spf"] = "unknown"
        else:
            spf = [r for r in txt.records if r.lower().startswith("v=spf1")]
            info["spf"] = "present" if spf else "missing"
            if spf:
                match = re.search(r"([~?+-])all\b", spf[0].lower())
                info["spf_all"] = match.group(0) if match else None
        dmarc = dns.lookup(f"_dmarc.{domain}", "TXT")
        if dmarc.error in _TRANSIENT:
            info["dmarc"] = "unknown"
        else:
            record = next((r for r in dmarc.records if r.lower().startswith("v=dmarc1")), None)
            info["dmarc"] = "present" if record else "missing"
            if record:
                match = re.search(r"\bp=([a-z]+)", record.lower())
                info["dmarc_policy"] = match.group(1) if match else None
    else:
        info.update(spf="missing", dmarc="missing")
    info["verdict"] = "invalid" if info["mail"] is False else ("ok" if info["mail"] else "unknown")
    if info["verdict"] == "invalid":
        info["invalid_reason"] = ("the domain does not exist" if info["exists"] is False else
                                  "the domain publishes a null MX (it accepts no email)" if info["null_mx"] else
                                  "the domain has no mail server")
    return info


# ---------------------------------------------------------------------------
# Layer 4: SMTP preflight (connection only, opt-in)
# ---------------------------------------------------------------------------

class SmtpPreflight:
    """Connect to a mail server, EHLO, read STARTTLS support, QUIT. No MAIL FROM, no
    RCPT TO: it learns whether the server talks, never whether a mailbox exists. A
    timeout (often an ISP blocking outbound port 25) is "unknown", not a failure."""

    def __init__(self, *, timeout: float = 8.0, connect: Optional[Callable[[str], Dict[str, Any]]] = None) -> None:
        self.timeout = timeout
        self._connect = connect or self._smtplib

    def _smtplib(self, host: str) -> Dict[str, Any]:
        import smtplib

        try:
            with smtplib.SMTP(timeout=self.timeout) as server:
                code, _ = server.connect(host, 25)
                if code != 220:
                    return {"smtp": "fail", "code": code, "detail": "the server refused the connection greeting"}
                code, _ = server.ehlo()
                if code != 250:
                    code, _ = server.helo()
                return {"smtp": "pass" if code == 250 else "fail", "code": code,
                        "starttls": bool(server.has_extn("starttls"))}
        except (socket.timeout, TimeoutError):
            return {"smtp": "unknown", "detail": "timed out (outbound port 25 may be blocked here)"}
        except ConnectionRefusedError:
            return {"smtp": "fail", "detail": "connection refused"}
        except (smtplib.SMTPException, OSError) as error:
            return {"smtp": "unknown", "detail": type(error).__name__}

    def check(self, mx_host: str) -> Dict[str, Any]:
        result = dict(self._connect(mx_host))
        result.setdefault("smtp", "unknown")
        result["host"] = mx_host
        return result


# ---------------------------------------------------------------------------
# Layers 5 and 6: catch-all and risk
# ---------------------------------------------------------------------------

def catch_all_from(provider: Optional[str], checks: Mapping[str, Any]) -> Optional[bool]:
    """What a verifier's answer says about the domain: catch-all (True), not (False), or unknown."""
    if provider != "emaillistverify":
        return None
    code = str((checks or {}).get("result_code") or "")
    if code in _ELV_CATCH_ALL:
        return True
    if code == "ok":
        return False
    return None


_RISKY_TLDS = frozenset({"tk", "ml", "ga", "cf", "gq", "xyz", "top", "click", "buzz", "zip", "mov"})


def risk_signals(email: str, *, first_name: str = "", last_name: str = "") -> Dict[str, Any]:
    from cloud.intel.email.providers import DISPOSABLE_DOMAINS
    from cloud.intel.vendor import email_rule

    local, _, domain = email.rpartition("@")
    collapsed = re.sub(r"[._+-]", "", local)
    return {
        "role": bool(email_rule.is_role_address(email) or email_rule.is_departmental_local(email, first_name,
                                                                                            last_name)),
        "disposable": domain in DISPOSABLE_DOMAINS,
        "free_provider": domain in FREE_EMAIL_DOMAINS,
        "no_reply": collapsed in ("noreply", "donotreply", "donotrespond"),
        "suspicious_tld": domain.rpartition(".")[2] in _RISKY_TLDS,
        "digit_heavy": sum(ch.isdigit() for ch in local) >= max(5, len(local) // 2),
    }


# ---------------------------------------------------------------------------
# Layer 7: contact / person
# ---------------------------------------------------------------------------

_CONTACT_HINTS = {
    "first_name": ("first name", "firstname", "first_name", "given name", "fname"),
    "last_name": ("last name", "lastname", "last_name", "surname", "family name", "lname"),
    "full_name": ("full name", "fullname", "full_name", "name", "contact name", "contact"),
    "company": ("company", "company name", "organization", "organisation", "account", "account name", "employer"),
    "title": ("title", "job title", "position", "role", "designation"),
    "website": ("website", "company website", "domain", "company domain", "url", "web"),
}


def detect_contact_columns(columns: Iterable[str]) -> Dict[str, str]:
    """Header -> contact field (first_name, last_name, full_name, company, title, website)."""
    found: Dict[str, str] = {}
    for column in columns:
        key = re.sub(r"[\s_-]+", " ", str(column).strip().lower())
        for field_name, hints in _CONTACT_HINTS.items():
            if field_name not in found and key in {h.replace("_", " ") for h in hints}:
                found[field_name] = column
                break
    return found


_COMPANY_NOISE = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "group", "the", "plc",
                  "gmbh", "sa", "ag", "bv", "pvt", "private", "holdings", "technologies", "technology", "solutions",
                  "services", "and"}
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}


def _domain_label(domain: str) -> str:
    """The registrable name of a domain: "acme-corp" for acme-corp.co.uk, "acme" for mail.acme.com."""
    labels = domain.lower().split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else labels[0]


def _host_of(website: str) -> str:
    text = re.sub(r"^[a-z]+://", "", (website or "").strip().lower())
    return text.split("/", 1)[0].split(":", 1)[0].removeprefix("www.")


def _names(contact: Mapping[str, Any]) -> Tuple[str, str]:
    first = str(contact.get("first_name") or "").strip()
    last = str(contact.get("last_name") or "").strip()
    if not (first or last) and contact.get("full_name"):
        parts = str(contact["full_name"]).split()
        first, last = (parts[0], parts[-1]) if len(parts) >= 2 else (parts[0] if parts else "", "")
    return first, last


def contact_signals(email: str, contact: Mapping[str, Any], *, public_evidence: Optional[Mapping[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Layer 7: does the address belong to this person at this company? ``yes``/``no``/``unknown``
    for each signal, plus a 0-100 ``contact_evidence_confidence`` and its label."""
    from cloud.intel.vendor import email_rule

    first, last = _names(contact)
    local, _, domain = email.rpartition("@")
    risk = risk_signals(email, first_name=first, last_name=last)
    fl, ll = re.sub(r"[^a-z]", "", first.lower()), re.sub(r"[^a-z]", "", last.lower())
    pattern = None
    if not (fl or ll):
        person = "unknown"
    elif fl and ll and email_rule.looks_generated(email, fl, ll):
        person, pattern = "yes", "name pattern"
    elif email_rule.name_explains_address(email, first, last):
        person, pattern = "yes", "contains the name"
    elif fl and ll and re.sub(r"[._+-]", "", local) in {fl[0] + ll[0], fl + ll[:1], fl[:1] + ll[:1]}:
        person, pattern = "yes", "initials"
    else:
        person = "no"
    company = str(contact.get("company") or "").strip()
    website_host = _host_of(str(contact.get("website") or ""))
    if website_host and (domain == website_host or domain.endswith("." + website_host)):
        company_match = "yes"
    elif not company:
        company_match = "unknown"
    elif risk["free_provider"]:
        company_match = "no"
    else:
        tokens = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if t not in _COMPANY_NOISE]
        compact = "".join(tokens)
        label = re.sub(r"[^a-z0-9]", "", _domain_label(domain))
        initials = "".join(t[0] for t in tokens)
        company_match = "yes" if label and compact and (
            label == compact or (len(label) >= 3 and (label in compact or compact.startswith(label)))
            or (len(compact) >= 4 and compact in label) or (len(tokens) >= 2 and label == initials)) else "no"
    evidence = (public_evidence or {}).get("public_email_evidence")
    score = 0
    if person == "yes":
        score += 45 if pattern == "name pattern" else 30
    if company_match == "yes":
        score += 30
    if not risk["role"]:
        score += 10
    if evidence is True:
        score += 15 if (public_evidence or {}).get("evidence_confidence") == "high" else 10
    if risk["free_provider"]:
        score -= 10
    if risk["role"]:
        score = min(score, 20)
    score = max(0, min(100, score))
    return {"person_name_match": person, "name_pattern": pattern, "company_match": company_match,
            "role_email": risk["role"], "contact_evidence_confidence": score,
            "contact_confidence_label": "high" if score >= 70 else "medium" if score >= 40 else "low"}


# ---------------------------------------------------------------------------
# Layer 8: public web evidence (opt-in)
# ---------------------------------------------------------------------------

_EMAIL_IN_TEXT = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,24}", re.I)
#: The company's own pages most likely to list people, in order. No search engines.
PUBLIC_PATHS = (("", "homepage"), ("/contact", "contact page"), ("/contact-us", "contact page"),
                ("/about", "about page"), ("/about-us", "about page"), ("/team", "team page"),
                ("/our-team", "team page"), ("/leadership", "leadership page"), ("/management", "leadership page"),
                ("/people", "team page"))


class PublicEvidenceFinder:
    """Scan a company's own public pages for published addresses, through the platform's
    :class:`~cloud.intel.core.http.SafeFetcher` (robots.txt respected, SSRF-safe, polite,
    size-capped; no CAPTCHA, login or paywall handling). Only hashes of the addresses and
    a few context words are kept."""

    def __init__(self, fetcher: Any = None, *, max_pages: int = 6) -> None:
        if fetcher is None:
            from cloud.intel.core.http import SafeFetcher

            fetcher = SafeFetcher(timeout=10.0, max_bytes=1_500_000)
        self.fetcher = fetcher
        self.max_pages = max_pages

    def scan(self, site: str) -> Dict[str, Any]:
        pages: List[Dict[str, Any]] = []
        found: Dict[str, List[Dict[str, Any]]] = {}
        fetched = 0
        for path, kind in PUBLIC_PATHS:
            if fetched >= self.max_pages:
                break
            url = f"https://{site}{path}"
            try:
                result = self.fetcher.fetch(url)
            except Exception as error:  # noqa: BLE001 - unsafe targets and network errors are results
                pages.append({"url": url, "type": kind, "status": 0, "error": type(error).__name__})
                if not path:
                    break  # the site itself is unreachable
                continue
            pages.append({"url": url, "type": kind, "status": result.status, "blocked": bool(result.blocked)})
            if not result.ok:
                if not path:
                    break
                continue
            fetched += 1
            text = re.sub(r"<[^>]+>", " ", result.text or "")
            text = re.sub(r"&#64;|&commat;", "@", text)
            for match in _EMAIL_IN_TEXT.finditer(text + " " + (result.text or "")):
                address = match.group(0).lower().strip(".")
                window = text[max(0, match.start() - 300):match.end() + 300] if match.start() < len(text) else ""
                words = sorted({w for w in re.findall(r"[a-z]{3,}", window.lower())})[:80]
                entries = found.setdefault(sha256(address), [])
                if not any(e["url"] == result.final_url for e in entries):
                    entries.append({"url": result.final_url, "type": kind, "context": words})
        return {"site": site, "pages": pages, "found": found, "pages_read": fetched,
                "blocked": sum(1 for p in pages if p.get("blocked"))}


def public_evidence_for(email: str, scan: Optional[Mapping[str, Any]], contact: Mapping[str, Any],
                        checked_at: Optional[str] = None) -> Dict[str, Any]:
    """Whether ``email`` itself appears on the scanned pages (True), was looked for and not
    found (False) or could not be looked for (None). Publication is not mailbox proof."""
    if not scan:
        return {"public_email_evidence": None, "reason": "not checked"}
    if not scan.get("pages_read"):
        return {"public_email_evidence": None, "reason": "the site could not be read (blocked or unreachable)"}
    entries = (scan.get("found") or {}).get(sha256(email)) or []
    if not entries:
        return {"public_email_evidence": False, "pages_read": scan["pages_read"], "checked_at": checked_at}
    first, last = _names(contact)
    names = {n for n in (first.lower(), last.lower()) if len(n) >= 3}
    best = max(entries, key=lambda e: bool(names & set(e.get("context") or [])))
    named = bool(names & set(best.get("context") or []))
    return {"public_email_evidence": True, "source_url": best["url"], "source_type": best["type"],
            "checked_at": checked_at, "evidence_confidence": "high" if named else "medium",
            "person_named_nearby": named}


# ---------------------------------------------------------------------------
# Layers 9-10 and the final result
# ---------------------------------------------------------------------------

def final_status(status: Optional[str], provider: Optional[str]) -> str:
    """VALID only when a mailbox-level verifier said so; INVALID when confirmed unusable;
    everything else (catch-all, role, free, disposable, unknown, transient) NOT_VERIFIED."""
    if status == "INVALID":
        return "INVALID"
    if status == "VALID" and provider in VERIFIERS:
        return "VALID"
    return "NOT_VERIFIED"


def mailbox_verification(status: Optional[str], provider: Optional[str]) -> str:
    if provider not in VERIFIERS:
        return "UNKNOWN"
    return {"VALID": "VERIFIED", "INVALID": "INVALID"}.get(status or "", "UNKNOWN")


def source_label(provider: Optional[str], checks: Optional[Mapping[str, Any]]) -> str:
    checks = checks or {}
    public = ((checks.get("evidence") or {}).get("public") or {}).get("public_email_evidence") is True
    if provider == "emaillistverify":
        if "builtin_status" in checks or "syntax" in checks or "mx" in checks:
            return "Built-in + EmailListVerify"
        return "EmailListVerify"
    if provider in VERIFIERS:
        return "Other authorized provider"
    return "Built-in + Public evidence" if public else "Built-in"


def _pf(value: Optional[bool]) -> str:
    return "PASS" if value is True else "FAIL" if value is False else "UNKNOWN"


def _yn(value: Optional[bool]) -> str:
    return "YES" if value is True else "NO" if value is False else "UNKNOWN"


def evidence_summary(status: Optional[str], provider: Optional[str], checks: Optional[Mapping[str, Any]]
                     ) -> Dict[str, str]:
    """The per-address evidence card (every value is a short word, safe to show and export)."""
    checks = checks or {}
    ev = checks.get("evidence") or {}
    fmt = ev.get("format") or {}
    dom = ev.get("domain") or {}
    risk = ev.get("risk") or {}
    contact = ev.get("contact") or {}
    public = ev.get("public") or {}
    smtp = ev.get("smtp") or {}
    technical = fmt.get("ok") if fmt else checks.get("syntax")
    mx = dom.get("mail") if dom else checks.get("mx")
    return {
        "technical": _pf(technical),
        "domain": _pf(dom.get("exists")) if dom else _pf(checks.get("mx")),
        "mx": _pf(mx),
        "spf": {"present": "PASS", "missing": "FAIL"}.get(dom.get("spf"), "UNKNOWN"),
        "dmarc": {"present": "PASS", "missing": "FAIL"}.get(dom.get("dmarc"), "UNKNOWN"),
        "smtp": {"pass": "PASS", "fail": "FAIL"}.get(smtp.get("smtp"), "UNKNOWN"),
        "catch_all": _yn(ev.get("catch_all")),
        "role": _yn(bool(risk.get("role", checks.get("role")))),
        "disposable": _yn(bool(risk.get("disposable", checks.get("disposable")))),
        "free_provider": _yn(bool(risk.get("free_provider", checks.get("free_provider")))),
        "person_match": {"yes": "YES", "no": "NO"}.get(contact.get("person_name_match"), "UNKNOWN"),
        "public_evidence": _yn(public.get("public_email_evidence")),
        "mailbox_verification": mailbox_verification(status, provider),
        "source": source_label(provider, checks),
    }


def cache_json(value: Any) -> Any:
    """Round-trip through JSON so cached results never hold non-serializable objects."""
    return json.loads(json.dumps(value, default=str))
