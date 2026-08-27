"""Ask a site's ``robots.txt`` for permission before discovery fetches anything.

Version 2 crawls boards a company published for candidates to read, from URLs
the operator supplied. Version 3 adds *discovery*, which goes looking at sites
nobody nominated, and that changes the etiquette: the crawler is now an
uninvited visitor and has to check whether it is welcome.

    >>> from utils.robots import RobotsCache
    >>> robots = RobotsCache()
    >>> robots.can_fetch("https://jobs.workable.com/api/v1/jobs?query=devops")
    True

**Fetch ``robots.txt`` with the crawler's real user agent.** This is the whole
reason this module exists rather than a two-line call to
:class:`urllib.robotparser.RobotFileParser`. That class fetches with
``Python-urllib/3.12``, which Cloudflare answers with a 403 — and
:meth:`~urllib.robotparser.RobotFileParser.read` turns any 403 into
``disallow_all = True``. Measured against the eight sources version 3 uses, the
stock implementation reported four of them as forbidden when their published
rules plainly allow us::

    jobs.workable.com/api/v1/jobs     stock: can_fetch=False    actual rules: allowed
    himalayas.app/jobs/api            stock: can_fetch=False    actual rules: allowed
    www.arbeitnow.com/api/...         stock: can_fetch=False    actual rules: allowed
    weworkremotely.com/...rss         stock: can_fetch=False    actual rules: allowed

A crawler that silently refuses every permitted source is worse than one that
never checked, because the failure looks like compliance. So the file is
fetched here, with the same session and user agent as everything else, and only
its *text* is handed to the standard parser.

**Wildcards are matched properly.** The stock parser's ``RuleLine.applies_to``
is a bare ``startswith``, so ``Disallow: /profile*`` never matches
``/profile/me`` and ``Disallow: /*.pdf$`` never matches anything at all. That
error runs in the permissive direction — it lets a crawler fetch paths the site
has plainly forbidden — so rule matching is implemented here to RFC 9309:
``*`` matches any run of characters, ``$`` anchors the end, and where several
rules match, the longest pattern wins with ``Allow`` taking ties.

**Content signals are honoured too.** Several sources publish a
``Content-Signal`` line — ``search=yes, ai-input=yes, ai-train=no``. Version 3
builds a job index and searches it, which is the ``search`` use; it does not
train models on the content. :meth:`RobotsCache.verdict` exposes the signals so
a caller can check any use it is unsure about.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Final, List, Optional, Tuple
from urllib.parse import urlsplit

from loguru import logger

from utils.http import USER_AGENT

__all__ = [
    "DEFAULT_TTL",
    "RobotsCache",
    "RobotsVerdict",
    "RuleSet",
]

#: How long a fetched ``robots.txt`` stays good, in seconds. A weekly run is
#: minutes long, so one fetch per host per run is the practical effect.
DEFAULT_TTL: Final[float] = 3600.0

#: Seconds to wait for ``robots.txt``. Short on purpose: a host too slow to
#: serve its own rules should not hold up the run.
_TIMEOUT: Final[Tuple[float, float]] = (5.0, 10.0)

#: Ceiling on the file itself. The largest legitimate ``robots.txt`` in the
#: wild is a few hundred kilobytes; anything past this is not rules.
_MAX_BYTES: Final[int] = 512 * 1024

#: What to do when ``robots.txt`` cannot be read at all.
_ON_ERROR_ALLOW: Final[str] = "allow"
_ON_ERROR_DENY: Final[str] = "deny"


@dataclass(frozen=True)
class RobotsVerdict:
    """What a host's ``robots.txt`` says about one URL.

    Attributes:
        allowed: Whether this URL may be fetched.
        reason: Why, in words, for the log and the run report.
        crawl_delay: Seconds the host asks callers to wait between requests,
            or ``None`` if it asks for nothing.
        content_signals: The ``Content-Signal`` declarations, e.g.
            ``{"search": "yes", "ai-train": "no"}``. Empty when none are given,
            which grants and restricts nothing.
        checked: Whether rules were actually read. ``False`` means the verdict
            came from the on-error policy rather than from the host.
    """

    allowed: bool
    reason: str
    crawl_delay: Optional[float] = None
    content_signals: Dict[str, str] = field(default_factory=dict)
    checked: bool = True

    def permits(self, use: str) -> Optional[bool]:
        """Whether a named content signal permits a use.

        Args:
            use: A signal name, e.g. ``"search"`` or ``"ai-train"``.

        Returns:
            ``True`` or ``False`` when the host declared that signal, and
            ``None`` when it did not — which, per the specification, neither
            grants nor restricts permission.
        """
        value = self.content_signals.get(use.strip().lower())
        if value is None:
            return None
        return value.strip().lower() == "yes"


@dataclass(frozen=True)
class _Rule:
    """One ``Allow`` or ``Disallow`` line, compiled for matching.

    Attributes:
        allow: Whether a match permits the fetch.
        pattern: The path pattern as published, kept for logging.
        matcher: ``pattern`` compiled with ``*`` and ``$`` given their RFC 9309
            meanings.
        weight: Length of the published pattern. RFC 9309 resolves competing
            rules by the most specific match, measured this way.
    """

    allow: bool
    pattern: str
    matcher: re.Pattern[str]
    weight: int


def _compile(pattern: str) -> Optional[re.Pattern[str]]:
    """Turn a robots path pattern into a regular expression.

    Args:
        pattern: The path from an ``Allow`` or ``Disallow`` line.

    Returns:
        The compiled matcher, anchored at the start of the path, or ``None``
        when the pattern is empty and so matches nothing.
    """
    if not pattern:
        return None

    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern

    # Escape everything, then restore the two metacharacters robots defines.
    expression = "".join(".*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", body))

    return re.compile(f"^{expression}$" if anchored_end else f"^{expression}")


@dataclass
class RuleSet:
    """The rules from one ``User-agent`` group, and how to apply them.

    Attributes:
        agents: The user agents this group addresses, lowercased.
        rules: Its ``Allow`` and ``Disallow`` lines.
        crawl_delay: Seconds the group asks callers to wait, if it says.
    """

    agents: List[str] = field(default_factory=list)
    rules: List[_Rule] = field(default_factory=list)
    crawl_delay: Optional[float] = None

    def allows(self, path: str) -> bool:
        """Whether this group permits a path.

        Args:
            path: Path and query of the URL, e.g. ``"/api/v1/jobs?q=x"``.

        Returns:
            ``True`` when permitted. A group with no rules permits everything,
            and where rules compete the longest pattern wins, with ``Allow``
            taking a tie — both as RFC 9309 specifies.
        """
        best: Optional[_Rule] = None

        for rule in self.rules:
            if not rule.matcher.match(path):
                continue
            if best is None or rule.weight > best.weight:
                best = rule
            elif rule.weight == best.weight and rule.allow:
                # A tie goes to Allow, so a site can carve an exception out of
                # a broader Disallow without ordering games.
                best = rule

        return True if best is None else best.allow


def _parse_groups(text: str) -> List[RuleSet]:
    """Read every ``User-agent`` group out of a ``robots.txt``.

    Args:
        text: The full contents of the file.

    Returns:
        One :class:`RuleSet` per group, in file order.
    """
    groups: List[RuleSet] = []
    current: Optional[RuleSet] = None
    # Consecutive User-agent lines address one shared group of rules.
    accepting_agents = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue

        name, _, value = line.partition(":")
        name = name.strip().lower()
        value = value.strip()

        if name == "user-agent":
            if current is None or not accepting_agents:
                current = RuleSet()
                groups.append(current)
                accepting_agents = True
            current.agents.append(value.lower())
            continue

        if current is None:
            # A rule before any User-agent line belongs to nothing. Ignored
            # rather than guessed at.
            continue

        accepting_agents = False

        if name in ("allow", "disallow"):
            matcher = _compile(value)
            if matcher is None:
                # "Disallow:" with an empty value is the idiom for "allow all",
                # and carries no rule of its own.
                continue
            current.rules.append(
                _Rule(allow=name == "allow", pattern=value, matcher=matcher, weight=len(value))
            )
        elif name == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                logger.debug("Ignoring unparsable crawl-delay {!r}", value)

    return groups


def _select_group(groups: List[RuleSet], user_agent: str) -> Optional[RuleSet]:
    """Choose the group that addresses this crawler.

    Args:
        groups: Every group in the file.
        user_agent: The crawler's user agent.

    Returns:
        The most specific matching group, the wildcard group if none names us,
        or ``None`` when the file addresses neither.
    """
    lowered = user_agent.lower()
    best: Optional[RuleSet] = None
    best_score = -1
    wildcard: Optional[RuleSet] = None

    for group in groups:
        for agent in group.agents:
            if agent == "*":
                if wildcard is None:
                    wildcard = group
                continue
            # A named group wins over the wildcard, and the longest name wins
            # among named groups -- the specific beats the general.
            if agent and agent in lowered and len(agent) > best_score:
                best, best_score = group, len(agent)

    return best or wildcard


def _parse_content_signals(text: str) -> Dict[str, str]:
    """Read ``Content-Signal`` declarations out of a ``robots.txt``.

    The standard parser drops unknown fields, so these are scanned separately.
    Only signals in the wildcard ``User-agent: *`` group are collected, since
    that is the group this crawler falls under.

    Args:
        text: The full contents of ``robots.txt``.

    Returns:
        Signal name to value, both lowercased. Empty when none are declared.
    """
    signals: Dict[str, str] = {}
    in_wildcard_group = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue

        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            in_wildcard_group = value == "*"
            continue

        if field_name == "content-signal" and in_wildcard_group:
            for declaration in value.split(","):
                name, _, setting = declaration.partition("=")
                if name.strip():
                    signals[name.strip().lower()] = setting.strip().lower()

    return signals


class RobotsCache:
    """Fetches, caches and applies ``robots.txt`` for the discovery crawler.

    Safe to share across worker threads: the cache is guarded, and a host is
    fetched at most once per TTL however many workers ask about it at once.

    Args:
        user_agent: The user agent to fetch as, and to match rules against.
            Defaults to the crawler's own, so the rules that are checked are
            the rules that will apply.
        session_factory: Builds the HTTP session used to fetch the file.
            Defaults to :func:`utils.http.build_session`. Injected so tests
            never touch the network.
        ttl: Seconds a fetched file stays good.
        on_error: What to do when the file cannot be read — ``"allow"`` or
            ``"deny"``. Defaults to ``"allow"``, matching the standard's
            treatment of an unreachable ``robots.txt`` as "no rules stated".
            Note that several sources front ``robots.txt`` itself with
            Cloudflare while serving their documented public API perfectly
            happily, so ``"deny"`` would refuse sources that permit us.
    """

    def __init__(
        self,
        user_agent: str = USER_AGENT,
        session_factory: Optional[Callable[[], object]] = None,
        ttl: float = DEFAULT_TTL,
        on_error: str = _ON_ERROR_ALLOW,
    ) -> None:
        self._user_agent = user_agent
        self._session_factory = session_factory
        self._ttl = max(0.0, float(ttl))
        self._on_error = on_error if on_error in (_ON_ERROR_ALLOW, _ON_ERROR_DENY) else _ON_ERROR_ALLOW
        self._lock = threading.Lock()
        # host -> (fetched_at, parser or None, content signals)
        self._cache: Dict[str, Tuple[float, Optional[RuleSet], Dict[str, str]]] = {}

    def _fetch(self, origin: str) -> Tuple[Optional[RuleSet], Dict[str, str]]:
        """Read and parse one host's ``robots.txt``.

        Args:
            origin: Scheme and host, e.g. ``"https://acme.com"``.

        Returns:
            ``(rules, signals)``. The rules are ``None`` when the file could
            not be read, which the caller resolves with the on-error policy.
        """
        url = f"{origin}/robots.txt"

        try:
            if self._session_factory is not None:
                session = self._session_factory()
            else:
                from utils.http import build_session

                session = build_session(retries=1)
        except Exception:  # noqa: BLE001 - a session failure must not end a run
            logger.opt(exception=True).debug("Could not build a session for {}", url)
            return None, {}

        try:
            response = session.get(
                url,
                headers={"User-Agent": self._user_agent, "Accept": "text/plain,*/*;q=0.8"},
                timeout=_TIMEOUT,
                allow_redirects=True,
            )
        except Exception as exc:  # noqa: BLE001 - unreachable rules are not fatal
            logger.debug("robots.txt unreadable for {}: {}", origin, exc)
            return None, {}
        finally:
            close = getattr(session, "close", None)
            if callable(close) and self._session_factory is None:
                close()

        status = getattr(response, "status_code", 0)

        # 4xx other than 401/403 means "no rules published", which the standard
        # treats as full permission. 401/403 on the *rules file* is not the
        # same as a rule forbidding us -- and in practice it is a bot-defence
        # product answering, not the site owner speaking -- so it is treated as
        # unreadable and resolved by the on-error policy.
        if status in (401, 403):
            logger.debug("robots.txt for {} answered HTTP {}; rules unknown", origin, status)
            return None, {}

        if 400 <= status < 500:
            logger.debug("robots.txt for {} answered HTTP {}; no rules published", origin, status)
            return RuleSet(agents=["*"]), {}

        if status >= 500 or status == 0:
            logger.debug("robots.txt for {} answered HTTP {}; rules unknown", origin, status)
            return None, {}

        text = getattr(response, "text", "") or ""
        if len(text) > _MAX_BYTES:
            text = text[:_MAX_BYTES]

        groups = _parse_groups(text)
        rules = _select_group(groups, self._user_agent) or RuleSet(agents=["*"])
        return rules, _parse_content_signals(text)

    def _rules_for(self, origin: str) -> Tuple[Optional[RuleSet], Dict[str, str]]:
        """Return the cached rules for a host, fetching them if needed.

        Args:
            origin: Scheme and host.

        Returns:
            ``(rules, signals)``, with ``rules`` ``None`` when unreadable.
        """
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(origin)
            if cached is not None and (now - cached[0]) < self._ttl:
                return cached[1], cached[2]

        # Fetched outside the lock: a slow host must not block every worker
        # asking about a different one. A duplicate fetch is cheap and rare.
        rules, signals = self._fetch(origin)

        with self._lock:
            self._cache[origin] = (time.monotonic(), rules, signals)

        return rules, signals

    def verdict(self, url: str) -> RobotsVerdict:
        """Decide whether one URL may be fetched.

        Args:
            url: The absolute URL the crawler wants to read.

        Returns:
            The verdict, including any crawl delay and content signals.
        """
        candidate = (url or "").strip()
        if not candidate:
            return RobotsVerdict(allowed=False, reason="no URL", checked=False)

        try:
            parts = urlsplit(candidate)
        except ValueError:
            return RobotsVerdict(allowed=False, reason="malformed URL", checked=False)

        if parts.scheme not in ("http", "https") or not parts.hostname:
            return RobotsVerdict(allowed=False, reason="not an http(s) URL", checked=False)

        origin = f"{parts.scheme}://{parts.netloc}"
        rules, signals = self._rules_for(origin)

        if rules is None:
            allowed = self._on_error == _ON_ERROR_ALLOW
            return RobotsVerdict(
                allowed=allowed,
                reason=f"robots.txt unreadable; on-error policy is {self._on_error}",
                content_signals=signals,
                checked=False,
            )

        # Rules are written against the path and query, not the whole URL.
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"

        allowed = rules.allows(target)

        return RobotsVerdict(
            allowed=allowed,
            reason="allowed by robots.txt" if allowed else "disallowed by robots.txt",
            crawl_delay=rules.crawl_delay,
            content_signals=signals,
        )

    def can_fetch(self, url: str) -> bool:
        """Whether one URL may be fetched.

        Args:
            url: The absolute URL.

        Returns:
            ``True`` when permitted.
        """
        return self.verdict(url).allowed

    def clear(self) -> None:
        """Forget every cached file, so the next call re-fetches."""
        with self._lock:
            self._cache.clear()
