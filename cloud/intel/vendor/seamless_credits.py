# VENDORED from CareerCrawler-seamless seamless/credits.py (uncommitted work on seamless-integration) on 2026-09-24 (pure logic, imports rewritten to cloud.intel.vendor).
# Keep behaviour identical to the original; its tests there remain the reference.
"""What a call actually costs, and the ceiling that refuses to exceed it.

This module exists because the rest of this package once believed something
false. Every docstring, the README and the planner all stated that search
endpoints were free and that only ``/companies/research`` and
``/contacts/research`` consumed credits. Measured against the live API on this
account, that is wrong::

    POST /search/companies  limit=1    balance 999 -> 998
    POST /search/contacts   limit=1    balance 998 -> 997
    POST /search/contacts   limit=5    balance 997 -> 996
    POST /search/contacts   limit=10   balance 996 -> 995
    POST /search/contacts   limit=25   balance 671 -> 668   (three credits)

A search costs one credit **per block of ten results requested** -- ``ceil(limit
/ 10)``. Asking for ten contacts costs exactly what asking for one costs, but
asking for eleven costs twice as much. Polling is genuinely free: two polls in a row
left the balance untouched.

The consequence for a bulk run is not small. A hundred and fifty companies with
four contacts each was budgeted at 750 credits under the old model and actually
costs 900; a five-hundred-company run budgeted at 500 actually costs about
3,500. A planner that under-counts does not merely mis-report -- it sails past
``--max-credits`` and dies mid-run on a 422, having spent the money anyway.

So the cost model lives in one place, and two rules hold above it:

**Cost is derived from the request, not declared by the caller.**
:func:`credit_cost_for` reads the path and the body. A method that forgets to
declare its price cannot exist, and a search endpoint added next year is
charged correctly without anybody remembering to update a table -- the same
reasoning that puts the ``Token`` header on the session rather than on each
method.

**The ceiling is checked before the request is sent, and again against
reality.** :meth:`CreditBudget.reserve` refuses a call that would breach the
ceiling, so the limit governs what is *spent* rather than what is *reported*.
:meth:`CreditBudget.observe` then reconciles against the balance the API itself
reports, so if this file's cost model is ever wrong again, the run stops on the
real number instead of on this module's opinion of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

__all__ = [
    "ASSUMED_SEARCH_LIMIT",
    "SEARCH_RESULTS_PER_CREDIT",
    "CREDITS_PER_POLL_CALL",
    "CREDITS_PER_RESEARCH_RECORD",
    "CREDITS_PER_SEARCH_CALL",
    "CreditBudget",
    "CreditCeilingReached",
    "credit_cost_for",
]

#: How many search results one credit buys. A search is billed per *block* of
#: results requested, not per call and not per result: ``limit`` 1, 5 and 10 all
#: cost one credit, and ``limit`` 11 costs two.
#:
#: This constant exists because getting it wrong cost real money. The first
#: measurement only probed ``limit`` 1, 5 and 10 -- all one credit -- and the
#: model was written as a flat one-per-call. A live slice run with ``limit=25``
#: then spent 13 credits against a ceiling of 12: three searches billed at three
#: credits each rather than one. Reconstructed against two independent runs::
#:
#:     limit=25 search + 4 research -> 7 credits   (ceil(25/10) + 4)
#:     3x limit=25 searches + 4 research -> 13     (3*ceil(25/10) + 4)
#:
#: Both fit ``ceil(limit / 10)`` exactly. Nothing else fits both.
SEARCH_RESULTS_PER_CREDIT: Final[int] = 10

#: The cost of the smallest possible search. Retained under its original name
#: because callers and tests refer to it; it is now the cost of *one block*
#: rather than of any call whatsoever.
CREDITS_PER_SEARCH_CALL: Final[int] = 1

#: What a search is assumed to request when its body carries no ``limit``.
#: Every search this client sends sets one explicitly -- there is a test that
#: asserts it -- so this only guards a hand-built request, and it guards it in
#: the safe direction by assuming a full block rather than none.
ASSUMED_SEARCH_LIMIT: Final[int] = SEARCH_RESULTS_PER_CREDIT

#: One credit per record submitted to a research endpoint. Submitting ten
#: search result ids in one call costs ten, not one -- research is priced per
#: record, search per block of ten results requested.
CREDITS_PER_RESEARCH_RECORD: Final[int] = 1

#: Polling costs nothing. Measured: two consecutive polls left the reported
#: balance unchanged. This is what makes the poll-until-settled loop safe to
#: run twenty times per request.
CREDITS_PER_POLL_CALL: Final[int] = 0


def credit_cost_for(path: str, body: Optional[Mapping[str, Any]] = None) -> int:
    """Work out what one request will cost before it is sent.

    Derived from the request itself rather than from a table of method names,
    so that an endpoint added later is priced by the shape of its path instead
    of by somebody remembering to register it. The order of the checks matters:
    ``/companies/research/poll`` contains ``/research`` and must be recognised
    as a poll first.

    Args:
        path: The API path, with or without the base URL.
        body: The JSON body, when there is one. Only the research endpoints
            read it, and only to count the records being submitted.

    Returns:
        The number of credits the call is expected to consume.
    """
    route = (path or "").split("?", 1)[0].rstrip("/")

    if route.endswith("/poll"):
        return CREDITS_PER_POLL_CALL

    if "/research" in route:
        return CREDITS_PER_RESEARCH_RECORD * _records_in(body)

    if "/search/" in route:
        return _search_cost(body)

    return 0


def _search_cost(body: Optional[Mapping[str, Any]]) -> int:
    """Price a search from the number of results it asks for.

    Args:
        body: The JSON body, whose ``limit`` is the requested page size.

    Returns:
        ``ceil(limit / 10)``, never less than one: even a search for a single
        record is billed a whole block.
    """
    raw = (body or {}).get("limit", ASSUMED_SEARCH_LIMIT)

    try:
        limit = int(raw)
    except (TypeError, ValueError):
        limit = ASSUMED_SEARCH_LIMIT

    limit = max(1, limit)
    blocks = -(-limit // SEARCH_RESULTS_PER_CREDIT)   # ceil, without float error
    return blocks * CREDITS_PER_SEARCH_CALL


def _records_in(body: Optional[Mapping[str, Any]]) -> int:
    """Count the records a research request submits.

    Args:
        body: The JSON body.

    Returns:
        How many records are being researched. At least one: a research call
        with nothing recognisable in it is assumed to cost something rather
        than nothing, because guessing low is the expensive direction to be
        wrong in.
    """
    if not body:
        return 1

    for field in ("searchResultIds", "contacts", "companies"):
        value = body.get(field)
        if isinstance(value, (list, tuple)):
            return max(1, len(value))

    return 1


class CreditCeilingReached(RuntimeError):
    """The run stopped because it reached the ceiling it was given.

    Not an error in any real sense -- it is the safety catch doing its job --
    but raised rather than returned so that no code path can carry on spending
    by forgetting to check a return value.
    """


@dataclass
class CreditBudget:
    """A hard ceiling on what one run may spend, enforced two ways.

    The two ways are not redundant. :attr:`reserved` is what this code believes
    it has committed, computed from :func:`credit_cost_for` before each call.
    :attr:`observed_spend` is what the account's own balance says has actually
    gone, computed from the ``X-PublicAPI-Credits`` header. The ceiling applies
    to whichever is larger, so a run is protected both from a plan that spends
    too much and from a cost model that turns out to be too optimistic.

    Args:
        ceiling: Credits this run may spend in total.

    Attributes:
        reserved: Credits committed by this run's own accounting.
        opening_balance: The first balance the API reported, or ``None`` before
            any response has been seen.
        latest_balance: The most recent balance the API reported.
    """

    ceiling: int
    reserved: int = 0
    opening_balance: Optional[int] = None
    latest_balance: Optional[int] = None

    @property
    def spent(self) -> int:
        """What this run has committed, by the larger of the two measures.

        Returns:
            The credits to treat as gone. Kept under the name the workflow and
            the tests already use.
        """
        observed = self.observed_spend
        return max(self.reserved, observed if observed is not None else 0)

    @property
    def observed_spend(self) -> Optional[int]:
        """What the account's own balance says this run has consumed.

        Returns:
            The drop from the first reported balance to the most recent, or
            ``None`` when the API has not reported a balance. Never negative: a
            balance that goes *up* mid-run means somebody topped the account up,
            which is not this run spending less than nothing.
        """
        if self.opening_balance is None or self.latest_balance is None:
            return None
        return max(0, self.opening_balance - self.latest_balance)

    @property
    def remaining(self) -> int:
        """How much of the ceiling is left.

        Returns:
            Credits still available under the ceiling, never below zero.
        """
        return max(0, self.ceiling - self.spent)

    def can_afford(self, credits: int) -> bool:
        """Whether a charge fits inside the ceiling.

        Args:
            credits: The cost being considered.

        Returns:
            Whether it fits.
        """
        return self.spent + max(0, int(credits)) <= self.ceiling

    def reserve(self, credits: int, what: str = "") -> None:
        """Commit a charge, before the request that incurs it is sent.

        Args:
            credits: The cost. Zero is always allowed, so a free endpoint is
                never the thing that stops a run.
            what: The call being paid for, for the refusal message.

        Raises:
            CreditCeilingReached: If it would exceed the ceiling. Raised
                *before* the request is made, so the ceiling limits what is
                spent rather than what is reported afterwards.
        """
        cost = max(0, int(credits))
        if cost == 0:
            return

        if not self.can_afford(cost):
            subject = f" for {what}" if what else ""
            raise CreditCeilingReached(
                f"Spending {cost} more credit(s){subject} would exceed the ceiling "
                f"of {self.ceiling} for this run ({self.spent} already committed). "
                "Raise --max-credits deliberately, or narrow --limit."
            )

        self.reserved += cost

    # Retained because the workflow and its tests have called it since the
    # first version of this package. Reservation is what it always meant.
    spend = reserve

    def observe(self, balance: Optional[int]) -> None:
        """Record the balance the API just reported.

        Args:
            balance: The value of ``X-PublicAPI-Credits``, or ``None`` when the
                response did not carry one. ``None`` is ignored rather than
                treated as zero -- a missing header says nothing about the
                account, and reading it as an empty balance would stop a
                perfectly solvent run.
        """
        if balance is None:
            return

        value = int(balance)
        if self.opening_balance is None:
            self.opening_balance = value
        self.latest_balance = value

    def describe(self) -> str:
        """Render the budget for a log line.

        Returns:
            A one-line summary naming both measures, because a divergence
            between them is exactly the thing worth noticing.
        """
        observed = self.observed_spend
        actual = "not reported" if observed is None else str(observed)
        return (
            f"{self.spent}/{self.ceiling} credits committed "
            f"(this run's accounting: {self.reserved}, account balance says: {actual})"
        )
