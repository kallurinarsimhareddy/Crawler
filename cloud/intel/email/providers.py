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
  key is server-side only. The result-code mapping follows the enum EmailListVerify
  publishes for ``GET /api/verifyEmail`` (``mapping_verified: True`` for those codes);
  a reply outside it is an error (``malformed_response``), never a stored result.

The existing job-board crawler had the only prior validation code (MX lookup,
free/role lists in ``contact_enrich.py``); its approach is reused here, its SMTP
probing is not.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional
from urllib.parse import urlencode

from cloud.intel.core.normalize import FREE_EMAIL_DOMAINS, normalize_email
from cloud.intel.providers.base import ProviderError, ProviderNotConfigured

__all__ = ["DISPOSABLE_DOMAINS", "EmailListVerifyProvider", "EmailProviderError", "FATAL_CODES", "elv_enabled", "normalize_elv_code", "EmailValidationProvider", "LocalValidator",
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

    from cloud.intel.email.engine import is_reserved_domain

    if is_reserved_domain(domain):
        return False
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    try:
        answers = resolver.resolve(domain, "MX")
        return any(str(r.exchange).strip(".") for r in answers)  # RFC 7505 null MX "." -> False
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        # RFC 5321 implicit MX: the domain's own address, IPv4 or IPv6.
        transient = False
        for rtype in ("A", "AAAA"):
            try:
                if resolver.resolve(domain, rtype):
                    return True
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.DNSException:
                transient = True
        return None if transient else False
    except dns.exception.DNSException:
        return None


#: LocalValidator's per-process domain cache: answers live MX_TTL seconds (the validator is
#: shared by every request and job in a long-lived API/worker process, so an answer must not
#: live forever), a transient failure TRANSIENT_TTL seconds (so one slow domain costs one
#: timeout per window, not one per row), and at most MX_CACHE_MAX domains are kept.
MX_TTL = 3600.0
TRANSIENT_TTL = 120.0
MX_CACHE_MAX = 100_000
_TRANSIENT = "transient"


class LocalValidator(EmailValidationProvider):
    name = "local"

    def __init__(self, *, resolver: Optional[Callable[[str], Optional[bool]]] = None,
                 smtp_probe: bool = False, clock: Optional[Callable[[], float]] = None) -> None:
        import threading

        self._resolver = resolver or dns_has_mail
        self._clock = clock or time.monotonic
        #: domain -> (True | False | "transient", expires at)
        self._mx_cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        if smtp_probe:
            raise ValueError("SMTP probing is disabled on this platform: it risks the worker IP's reputation. "
                             "Use a validation provider instead.")

    # --- the bounded, expiring domain cache -----------------------------------------------

    def _get(self, domain: str) -> Any:
        """The cached answer (True, False, "transient") or None when unknown/expired."""
        entry = self._mx_cache.get(domain)
        if entry is None:
            return None
        if entry[1] <= self._clock():
            self._mx_cache.pop(domain, None)
            return None
        return entry[0]

    def _put(self, domain: str, value: Any) -> None:
        ttl = TRANSIENT_TTL if value == _TRANSIENT else MX_TTL
        with self._lock:
            if len(self._mx_cache) >= MX_CACHE_MAX:
                now = self._clock()
                for key in [k for k, (_, exp) in self._mx_cache.items() if exp <= now]:
                    self._mx_cache.pop(key, None)
                while len(self._mx_cache) >= MX_CACHE_MAX:  # still full: drop the oldest insertions
                    self._mx_cache.pop(next(iter(self._mx_cache)))
            self._mx_cache[domain] = (value, self._clock() + ttl)

    def seed(self, domain: str, mail: Optional[bool]) -> None:
        """Reuse an answer the evidence layer already has (True / False / None = transient), so
        one job resolves each domain once and both layers agree on it."""
        if domain:
            self._put(domain, _TRANSIENT if mail is None else bool(mail))

    def prime_domains(self, domains, *, max_workers: int = 16) -> None:
        """Resolve many domains' mail records concurrently (same resolver and timeouts), so the
        per-address :meth:`check` calls hit the cache instead of waiting on DNS one by one.
        Transient failures are remembered only for TRANSIENT_TTL."""
        from concurrent.futures import ThreadPoolExecutor

        todo = sorted({d for d in domains if d and self._get(d) is None})
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(todo)))) as pool:
            for domain, result in zip(todo, pool.map(self._safe_resolve, todo)):
                self._put(domain, _TRANSIENT if result is None else result)

    def _safe_resolve(self, domain: str) -> Optional[bool]:
        try:
            return self._resolver(domain)
        except Exception:  # noqa: BLE001 - treated like a transient DNS failure
            return None

    def _mail_domain(self, domain: str) -> Optional[bool]:
        cached = self._get(domain)
        if cached is None:
            result = self._safe_resolve(domain)
            cached = _TRANSIENT if result is None else result
            self._put(domain, cached)
        return None if cached == _TRANSIENT else cached

    def check(self, email: str) -> ValidationResult:
        """Never raises: an unexpected failure in a layer is an UNKNOWN with the error recorded,
        so one odd address cannot fail the batch it is in."""
        try:
            return self._check(email)
        except Exception as error:  # noqa: BLE001
            normalized = normalize_email(email) or (email or "").strip().lower()
            return ValidationResult(normalized, "UNKNOWN", None, self.name,
                                    {"error": f"built-in check failed ({type(error).__name__})"}, decisive=False)

    def _check(self, email: str) -> ValidationResult:
        from cloud.intel.email.engine import is_reserved_domain
        from cloud.intel.vendor import email_rule

        raw = (email or "").strip()
        normalized = normalize_email(raw)
        checks: Dict[str, Any] = {"syntax": bool(normalized) and email_rule.is_valid_syntax(normalized)}
        if not checks["syntax"]:
            return ValidationResult(raw.lower(), "INVALID", 0.0, self.name, checks)
        local, _, domain = normalized.rpartition("@")
        checks["domain"] = domain
        if is_reserved_domain(domain):  # RFC 2606/6761: can never receive mail
            checks.update(placeholder=True, reserved_domain=True, mx=False)
            return ValidationResult(normalized, "INVALID", 0.0, self.name, checks)
        # Placeholder-looking (test@, user@, @company.com, @email.com ...): those can be real
        # mailboxes on real domains, so it is a risk signal, never INVALID on its own.
        checks["placeholder"] = email_rule.is_placeholder(normalized)
        checks["no_reply"] = re.sub(r"[._+-]", "", local) in ("noreply", "donotreply", "donotrespond")
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


#: The result codes GET /api/verifyEmail documents (OpenAPI enum at
#: https://api.emaillistverify.com/api-doc-json, read 2026-10-01). ``error_credit`` is the
#: documented "not enough credits" answer and is handled as an error, not a result.
ELV_DOCUMENTED_CODES = frozenset({"ok", "unknown", "dead_server", "invalid_mx", "email_disabled", "antispam_system",
                                  "ok_for_all", "smtp_protocol", "invalid_syntax", "disposable", "spamtrap"})
#: EmailListVerify result code -> (status, score). The documented codes above plus a few
#: older/legacy spellings (accept_all, fail, incorrect, role) still seen in exports.
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
#: Provider reply -> (normalized error code, retryable). Anything unknown is "provider_error".
_ELV_ERRORS: Dict[str, tuple] = {
    "key_not_valid": ("auth", False),
    "missing_parameters": ("bad_request", False),
    "insufficient_credits": ("no_credits", False),
    "no_credits": ("no_credits", False),
    "error": ("provider_error", True),
    "error_credit": ("no_credits", False),
}
#: HTTP statuses worth another try because the request was NOT processed (so it was not
#: charged): rate limited, or the gateway had no upstream. 500 / 504 / a read timeout may
#: have been processed — and charged — upstream, so a paid check is never retried on those.
_RETRY_HTTP = frozenset({425, 429, 502, 503})
#: Account-level failures: once one happens, further calls in the run are pointless.
FATAL_CODES = frozenset({"auth", "no_credits", "bad_request"})
#: Failures after which the provider may already have charged for the check.
CHARGE_UNCERTAIN_CODES = frozenset({"timeout", "malformed_response", "provider_error"})


class EmailProviderError(ProviderError):
    """A normalized provider failure. ``code`` is one of auth, bad_request, no_credits,
    rate_limited, timeout, network, provider_error; ``retryable`` says whether trying
    again later could succeed. The message never contains the API key."""

    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        #: True when the provider may have processed (and charged for) the request anyway.
        self.charge_uncertain = False


def elv_enabled(registry: Any, ctx: Any) -> bool:
    """Paid EmailListVerify checks run only for a stored AND verified key. Uses the
    registry's ``enabled()`` when it has one; otherwise configured + status verified."""
    enabled = getattr(registry, "enabled", None)
    if callable(enabled):
        return bool(enabled(ctx, "emaillistverify"))
    if not registry.configured(ctx, "emaillistverify"):
        return False
    return (registry.connection(ctx, "emaillistverify") or {}).get("status") == "verified"


def normalize_elv_code(text: Optional[str]) -> str:
    """EmailListVerify answers with a bare result code; tolerate whitespace, case and
    ``code|detail`` / ``code:detail`` suffixes."""
    code = (text or "").strip().lower()
    for sep in ("|", ":", " "):
        code = code.split(sep, 1)[0]
    return code


class EmailListVerifyProvider(EmailValidationProvider):
    """The current EmailListVerify API (OpenAPI at https://api.emaillistverify.com/api-doc-json, read
    2026-09-29): the key travels in the ``x-api-key`` header, never the URL. The legacy
    ``apps.emaillistverify.com/api/getCredit?secret=`` endpoints now serve the web app's HTML.

    Paid mailbox-level validation, one credit per checked address by default
    (``cost_per_check`` comes from the connection settings when set). Transient
    failures (timeouts, connection errors, HTTP 429/5xx) are retried with exponential
    backoff; a permanent failure (bad key, no credits) raises at once."""

    name = "emaillistverify"
    paid = True
    BASE = "https://api.emaillistverify.com/api"

    def __init__(self, api_key: str, *, session: Any = None, timeout: float = 30.0, max_retries: int = 2,
                 backoff: float = 1.0, sleep: Optional[Callable[[float], None]] = None,
                 cost_per_check: float = 1.0) -> None:
        self._key = api_key or ""
        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff = backoff
        self._sleep = sleep or time.sleep
        self.cost_per_check = float(cost_per_check)
        self.calls = 0
        self.retries = 0
        self._last_call = 0.0

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    #: GET /api/verifyEmail allows 10 requests per second; stay just under it.
    MIN_INTERVAL = 0.11

    def _throttle(self) -> None:
        wait = self._last_call + self.MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)  # real time, independent of the (injectable) retry backoff sleep
        self._last_call = time.monotonic()

    def _get(self, path: str, params: Dict[str, str]) -> Any:
        """Retries only failures where the request cannot have been processed (and charged):
        connection errors before a reply, HTTP 425/429/502/503. A read timeout, HTTP 500 or 504
        may have been processed upstream, so it is raised at once with ``charge_uncertain``."""
        attempt = 0
        while True:
            self._throttle()
            try:
                response = self._session.get(f"{self.BASE}/{path}?{urlencode(params)}",
                                             headers={**self._headers(), "Accept": "text/html"}, timeout=self.timeout)
            except Exception as error:  # noqa: BLE001 - requests' exceptions, or a test double's
                name = type(error).__name__
                before_send = name in ("ConnectTimeout", "ConnectionError", "ConnectionRefusedError",
                                       "NewConnectionError", "NameResolutionError", "SSLError", "ProxyError")
                kind = "timeout" if "timeout" in name.lower() else "network"
                if before_send and attempt < self.max_retries:
                    attempt += 1
                    self.retries += 1
                    self._sleep(self.backoff * (2 ** (attempt - 1)))
                    continue
                raised = EmailProviderError(f"EmailListVerify {kind} error ({self._redact(name)})", code=kind,
                                            retryable=True)
                raised.charge_uncertain = not before_send
                raise raised from None
            if response.status_code in _RETRY_HTTP and attempt < self.max_retries:
                attempt += 1
                self.retries += 1
                self._sleep(self.backoff * (2 ** (attempt - 1)))
                continue
            return response

    def health(self, live: bool = False) -> Dict[str, Any]:
        if not self._key:
            return {"status": "not_configured", "detail": "an EmailListVerify API key is not connected"}
        if not live:
            return {"status": "configured_unverified", "detail": "key stored; not yet verified"}
        response = self._session.get(f"{self.BASE}/credits", headers=self._headers(), timeout=self.timeout)
        if response.status_code in (401, 403):
            return {"status": "error", "detail": f"EmailListVerify rejected the key (HTTP {response.status_code})"}
        try:
            payload = response.json() if response.status_code == 200 else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("onDemand"), dict):
            return {"status": "error", "detail": f"unexpected reply (HTTP {response.status_code})"}
        on_demand = int(payload["onDemand"].get("available") or 0)
        subscription = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else {}
        from_subscription = int(subscription.get("available") or 0)
        return {"status": "ok", "detail": "credit balance read (free call)", "credits": on_demand + from_subscription,
                "credits_on_demand": on_demand, "credits_subscription": from_subscription}

    def _headers(self) -> Dict[str, str]:
        return {"x-api-key": self._key, "Accept": "application/json"}

    def check(self, email: str) -> ValidationResult:
        if not self._key:
            raise ProviderNotConfigured("EmailListVerify is not connected for this workspace")
        started = time.monotonic()
        self.calls += 1
        response = self._get("verifyEmail", {"email": email})
        if response.status_code in (401, 403):  # documented: invalid key / account disabled
            raise EmailProviderError(f"EmailListVerify rejected the key (HTTP {response.status_code})", code="auth")
        if response.status_code == 429:
            raise EmailProviderError("EmailListVerify rate limit reached", code="rate_limited", retryable=True)
        if response.status_code == 400:
            raise EmailProviderError("EmailListVerify rejected the request (HTTP 400)", code="bad_request")
        if response.status_code != 200:
            error = EmailProviderError(f"EmailListVerify answered HTTP {response.status_code}",
                                       code="provider_error", retryable=response.status_code >= 500)
            error.charge_uncertain = response.status_code in (500, 504)
            raise error
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - undecodable body
            text = ""
        code = normalize_elv_code(text)
        if code in _ELV_ERRORS:
            error_code, retryable = _ELV_ERRORS[code]
            raise EmailProviderError(f"EmailListVerify refused the request ({code})", code=error_code,
                                     retryable=retryable)
        if code not in ELV_STATUS:
            # An HTML page, JSON, an empty body or a code nobody documented: not an answer. It is
            # never stored as a result (the address stays re-checkable); the call may have been
            # charged, so the caller counts it.
            error = EmailProviderError("EmailListVerify sent an unrecognized reply "
                                       f"({len(text)} characters)", code="malformed_response", retryable=True)
            error.charge_uncertain = True
            raise error
        status, score = ELV_STATUS[code]
        return ValidationResult(email, status, score, self.name,
                                {"result_code": code, "mapping_verified": code in ELV_DOCUMENTED_CODES,
                                 "latency_ms": round((time.monotonic() - started) * 1000, 1)},
                                raw={"result": code, "http_status": response.status_code})

    def check_many(self, emails: List[str], *, batch_size: int = 50) -> List[Any]:
        """Check addresses in batches: one ValidationResult or EmailProviderError per
        address, in order. After an account-level failure (bad key, no credits) the
        remaining addresses are not sent; they come back as that same error."""
        out: List[Any] = []
        fatal: Optional[EmailProviderError] = None
        for offset in range(0, len(emails), max(1, batch_size)):
            for email in emails[offset:offset + batch_size]:
                if fatal is not None:
                    out.append(fatal)
                    continue
                try:
                    out.append(self.check(email))
                except EmailProviderError as error:
                    out.append(error)
                    if error.code in FATAL_CODES:
                        fatal = error
        return out
