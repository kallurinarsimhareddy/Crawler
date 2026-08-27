"""When to try again, and how long to wait first.

Retrying everything is as wrong as retrying nothing. A read timeout is worth
another attempt; a 404 is not, and a CAPTCHA is a site telling us to stop —
retrying that is not merely useless, it is the behaviour that gets an IP
blocked for the companies that *would* have worked.

So a failure is **classified before it is acted on**, and the taxonomy is
:class:`utils.blocking.Block`, which the crawler already uses for its failure
reports. A second vocabulary here would drift from that one within a month.

    >>> policy = RetryPolicy()
    >>> policy.decide(Block.TIMEOUT, attempt=1).retry
    True
    >>> policy.decide(Block.CAPTCHA, attempt=1).retry
    False

Three properties matter at scale:

**Exponential growth.** A site that is briefly unwell should not be asked again
immediately, and a site that is badly unwell should be asked progressively less
often.

**Jitter.** Two hundred companies on one vendor fail together when that vendor
has a bad minute. Without jitter they all retry in the same second, which is
indistinguishable from an attack. The delay is spread instead.

**The server's own answer wins.** When a response carries ``Retry-After``, that
number replaces the computed delay in both directions — longer *and* shorter.
It is the one authoritative statement about when to come back.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Final, FrozenSet, Optional

from utils.blocking import Block, classify_text, cooldown_seconds, is_retryable

__all__ = ["RetryPolicy", "Verdict", "classify"]

#: Whether a blocker is worth another attempt is **not decided here**.
#: :func:`utils.blocking.is_retryable` already answers it, and its judgement is
#: tested and in production: it treats a Cloudflare challenge and a bot
#: challenge as transient — they often clear after ten minutes — while a
#: CAPTCHA, a 403 and a login wall are settled answers. Duplicating that table
#: would give the crawler two opinions that drift apart.
#:
#: One blocker is added on top: an unrecognised failure. `classify_text`
#: returns it for anything it cannot name, and an unknown failure is more often
#: a blip than a wall. The attempt cap bounds the cost of being wrong.
_ALSO_TRANSIENT: Final[FrozenSet[Block]] = frozenset({Block.UNRECOGNISED})

#: Failures where the site actively refused us, as opposed to merely failing.
#: Reported apart because the two need different work: a refusal needs a
#: different route in, a failure needs a retry. Some of these are still
#: retryable — a Cloudflare challenge is both a refusal and worth waiting out.
_REFUSALS: Final[FrozenSet[Block]] = frozenset({
    Block.FORBIDDEN,
    Block.CAPTCHA,
    Block.AWS_WAF,
    Block.CLOUDFLARE,
    Block.BOT_CHALLENGE,
    Block.AUTH_REQUIRED,
})


def classify(error: object) -> Block:
    """Name a failure.

    Args:
        error: An exception or its message.

    Returns:
        The blocker. :attr:`Block.UNRECOGNISED` when nothing matched, which is
        treated as transient — an unknown failure is more often a blip than a
        wall, and the attempt cap bounds the cost of being wrong.
    """
    return classify_text(str(error or ""))


@dataclass(frozen=True)
class Verdict:
    """What to do about one failed attempt.

    Attributes:
        retry: Whether to try this company again.
        delay: Seconds to wait first. Zero when not retrying.
        blocked: Whether the site refused us, as opposed to merely failing.
            Reported separately because the two need different work: a block
            needs a different route in, a failure needs a retry.
        reason: The classified blocker's label, for the queue and the report.
    """

    retry: bool
    delay: float
    blocked: bool
    reason: str


class RetryPolicy:
    """Decides the fate of a failed attempt.

    Args:
        max_attempts: Attempts allowed per company per run, including the
            first. Reached, a transient failure becomes a permanent one.
        base_delay: Seconds before the first retry.
        max_delay: Ceiling on the computed wait, before ``Retry-After``.
        jitter: Fraction of the delay to randomise, ``0``–``1``. At ``0.25`` a
            sixty-second wait lands somewhere in forty-five to seventy-five.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 30.0,
        max_delay: float = 1800.0,
        jitter: float = 0.25,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(0.0, float(max_delay))
        self.jitter = min(1.0, max(0.0, float(jitter)))

    def decide(
        self,
        blocker: Block,
        attempt: int,
        retry_after: Optional[float] = None,
    ) -> Verdict:
        """Judge one failure.

        Args:
            blocker: What went wrong, from :func:`classify`.
            attempt: Which attempt this was, counting from one.
            retry_after: Seconds the server asked us to wait, when it said.

        Returns:
            The verdict.
        """
        if blocker is Block.NONE:
            return Verdict(retry=False, delay=0.0, blocked=False, reason=blocker.value)

        blocked = blocker in _REFUSALS
        transient = is_retryable(blocker) or blocker in _ALSO_TRANSIENT

        if not transient or attempt >= self.max_attempts:
            return Verdict(retry=False, delay=0.0, blocked=blocked,
                           reason=blocker.value)

        return Verdict(
            retry=True,
            delay=self._delay_for(blocker, attempt, retry_after),
            blocked=False,
            reason=blocker.value,
        )

    def _delay_for(
        self,
        blocker: Block,
        attempt: int,
        retry_after: Optional[float],
    ) -> float:
        """How long to wait before the next attempt.

        Args:
            blocker: What went wrong.
            attempt: Which attempt just failed.
            retry_after: The server's own instruction, when it gave one.

        Returns:
            Seconds.
        """
        if retry_after is not None and retry_after >= 0:
            # Authoritative in both directions -- a server asking for five
            # seconds should not be left alone for twenty minutes either.
            return float(retry_after)

        # The floor comes from utils.blocking, which already knows a 429 needs
        # longer than a dropped connection. Backoff grows from there rather
        # than from a number invented here.
        floor = cooldown_seconds(blocker)
        delay = max(self.base_delay, floor) * (2 ** max(0, attempt - 1))

        delay = min(delay, self.max_delay)

        if self.jitter:
            spread = delay * self.jitter
            delay = delay + random.uniform(-spread, spread)

        return max(0.0, delay)
