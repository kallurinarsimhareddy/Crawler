"""Email validation providers.

``EmailValidationProvider.check(email) -> ValidationResult``. Two ship:

* :class:`LocalValidator` — free, always first. Syntax, domain existence and
  MX (dnspython, injectable resolver), disposable domains, role accounts
  (vendored ``zerocredit.email_rule``), free mailbox providers. **No SMTP
  probing by default**: RCPT-probing mail servers from a worker IP risks that
  IP's reputation and blocklisting, so ``smtp_probe`` exists only as an
  explicit opt-in flag and is not implemented against live servers here.
* :class:`EmailListVerifyProvider` — paid, used only when local checks cannot
  decide, only with ``allow_paid``, only against a ledger reservation. The API
  key is server-side only. The result-code mapping below follows
  EmailListVerify's published result codes as best known; it has **not** been
  exercised with a live key and is marked ``mapping_verified: False`` until it is.

The existing job-board crawler had the only prior validation code (MX lookup,
free/role lists in ``contact_enrich.py``); its approach is reused here, its SMTP
probing is not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional
from urllib.parse import urlencode

from cloud.intel.core.normalize import FREE_EMAIL_DOMAINS, normalize_email
from cloud.intel.providers.base import ProviderError, ProviderNotConfigured

__all__ = ["DISPOSABLE_DOMAINS", "EmailListVerifyProvider", "EmailValidationProvider", "LocalValidator",
           "ValidationResult", "dns_has_mail", "ELV_STATUS"]

STATUSES = ("VALID", "INVALID", "RISKY", "UNKNOWN", "DISPOSABLE", "ROLE", "FREE_PROVIDER")


def _load_disposable() -> FrozenSet[str]:
    path = Path(__file__).resolve().parent / "data" / "disposable_domains.txt"
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(l.strip().lower() for l in lines if l.strip() and not l.startswith("#"))


DISPOSABLE_DOMAINS = _load_disposable()


@dataclass
class ValidationResult:
    email: str
    status: str
    score: float
    provider: str
    checks: Dict[str, Any] = field(default_factory=dict)
    #: True when this result is final; False means a paid provider could decide better.
    decisive: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"email": self.email, "status": self.status, "score": self.score, "provider": self.provider,
                "checks": self.checks, "decisive": self.decisive}


class EmailValidationProvider:
    name = "provider"
    paid = False

    def check(self, email: str) -> ValidationResult:  # pragma: no cover - interface
        raise NotImplementedError

    def health(self, live: bool = False) -> Dict[str, Any]:
        return {"status": "ok", "detail": ""}


def dns_has_mail(domain: str, *, timeout: float = 5.0) -> Optional[bool]:
    """True if the domain has MX (or, failing that, A) records; False if it does
    not exist or has neither; None on a transient DNS failure (not cached)."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    try:
        answers = resolver.resolve(domain, "MX")
        return any(str(r.exchange).strip(".") for r in answers)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        try:
            return bool(resolver.resolve(domain, "A"))
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return False
        except dns.exception.DNSException:
            return None
    except dns.exception.DNSException:
        return None


class LocalValidator(EmailValidationProvider):
    name = "local"

    def __init__(self, *, resolver: Optional[Callable[[str], Optional[bool]]] = None,
                 smtp_probe: bool = False) -> None:
        self._resolver = resolver or dns_has_mail
        self._mx_cache: Dict[str, Optional[bool]] = {}
        if smtp_probe:
            raise ValueError("SMTP probing is disabled on this platform: it risks the worker IP's reputation. "
                             "Use a validation provider instead.")

    def _mail_domain(self, domain: str) -> Optional[bool]:
        if domain not in self._mx_cache:
            result = self._resolver(domain)
            if result is None:
                return None  # transient; do not cache
            self._mx_cache[domain] = result
        return self._mx_cache[domain]

    def check(self, email: str) -> ValidationResult:
        from cloud.intel.vendor import email_rule

        raw = (email or "").strip()
        normalized = normalize_email(raw)
        checks: Dict[str, Any] = {"syntax": bool(normalized) and email_rule.is_valid_syntax(normalized)}
        if not checks["syntax"]:
            return ValidationResult(raw.lower(), "INVALID", 0.0, self.name, checks)
        local, _, domain = normalized.rpartition("@")
        checks["domain"] = domain
        checks["placeholder"] = email_rule.is_placeholder(normalized)
        if checks["placeholder"]:
            return ValidationResult(normalized, "INVALID", 0.0, self.name, checks)
        checks["disposable"] = domain in DISPOSABLE_DOMAINS
        if checks["disposable"]:
            return ValidationResult(normalized, "DISPOSABLE", 5.0, self.name, checks)
        mail = self._mail_domain(domain)
        checks["mx"] = mail
        if mail is False:
            return ValidationResult(normalized, "INVALID", 0.0, self.name, checks)
        checks["role"] = email_rule.is_role_address(normalized)
        checks["free_provider"] = domain in FREE_EMAIL_DOMAINS
        if checks["role"]:
            return ValidationResult(normalized, "ROLE", 40.0, self.name, checks)
        if checks["free_provider"]:
            return ValidationResult(normalized, "FREE_PROVIDER", 35.0, self.name, checks)
        if mail is None:
            return ValidationResult(normalized, "UNKNOWN", 30.0, self.name, {**checks, "dns": "transient failure"},
                                    decisive=False)
        # Syntax and mail server check out, but only a mailbox-level check can say VALID.
        return ValidationResult(normalized, "UNKNOWN", 60.0, self.name,
                                {**checks, "mailbox": "not checked (no SMTP probing)"}, decisive=False)


#: EmailListVerify result code -> (status, score). Unverified mapping (see module doc).
ELV_STATUS: Dict[str, tuple] = {
    "ok": ("VALID", 95.0),
    "ok_for_all": ("RISKY", 55.0),
    "accept_all": ("RISKY", 55.0),
    "antispam_system": ("RISKY", 50.0),
    "smtp_protocol": ("RISKY", 45.0),
    "unknown": ("UNKNOWN", 40.0),
    "email_disabled": ("INVALID", 0.0),
    "dead_server": ("INVALID", 0.0),
    "invalid_mx": ("INVALID", 0.0),
    "invalid_syntax": ("INVALID", 0.0),
    "incorrect": ("INVALID", 0.0),
    "fail": ("INVALID", 0.0),
    "disposable": ("DISPOSABLE", 5.0),
    "spamtrap": ("INVALID", 0.0),
    "role": ("ROLE", 40.0),
}
_ELV_ERRORS = ("key_not_valid", "missing_parameters", "insufficient_credits", "no_credits", "error")


class EmailListVerifyProvider(EmailValidationProvider):
    name = "emaillistverify"
    paid = True
    BASE = "https://apps.emaillistverify.com/api"

    def __init__(self, api_key: str, *, session: Any = None, timeout: float = 30.0) -> None:
        self._key = api_key or ""
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self.timeout = timeout
        self.calls = 0

    def health(self, live: bool = False) -> Dict[str, Any]:
        if not self._key:
            return {"status": "not_configured", "detail": "an EmailListVerify API key is not connected"}
        if not live:
            return {"status": "configured_unverified", "detail": "key stored; not yet verified"}
        response = self._session.get(f"{self.BASE}/getCredit?{urlencode({'secret': self._key})}", timeout=self.timeout)
        text = (response.text or "").strip()
        if response.status_code == 200 and text.replace(".", "", 1).isdigit():
            return {"status": "ok", "detail": "credit balance read (free call)", "credits": float(text)}
        return {"status": "error", "detail": f"unexpected reply (HTTP {response.status_code})"}

    def check(self, email: str) -> ValidationResult:
        if not self._key:
            raise ProviderNotConfigured("EmailListVerify is not connected for this workspace")
        started = time.monotonic()
        self.calls += 1
        response = self._session.get(f"{self.BASE}/verifyEmail?{urlencode({'secret': self._key, 'email': email})}",
                                     timeout=self.timeout)
        code = (response.text or "").strip().lower()
        if response.status_code != 200 or code in _ELV_ERRORS:
            raise ProviderError(f"EmailListVerify refused the request ({code or response.status_code})")
        status, score = ELV_STATUS.get(code, ("UNKNOWN", 40.0))
        return ValidationResult(email, status, score, self.name,
                                {"result_code": code, "mapping_verified": False,
                                 "latency_ms": round((time.monotonic() - started) * 1000, 1)},
                                raw={"result": code})
