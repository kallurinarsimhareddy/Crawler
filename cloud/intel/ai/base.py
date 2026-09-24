"""The provider-neutral AI interface.

The platform uses AI selectively — extraction, classification, entity matching,
research planning, summarisation, signal explanation, personalisation — and
never depends on it: every caller has a deterministic path, and
:class:`~cloud.intel.ai.rules.RulesProvider` (the default) makes that path the
one taken whenever no external provider is configured or the workspace has not
allowed its data to leave.

A provider answers:

* :meth:`AIProvider.generate` — free text, as an :class:`AIResult`;
* :meth:`AIProvider.structured_generate` — a JSON object matching a JSON schema
  (validated here, not trusted), as an :class:`AIResult`;
* :meth:`AIProvider.stream` — text chunks where the provider supports streaming
  (otherwise one chunk);
* :meth:`AIProvider.health_check` — configuration status, and optionally one
  tiny live request (never made implicitly).

Every call leaves :attr:`AIProvider.last_usage` — provider, model, prompt and
completion tokens, request id and an estimated cost where the price is known —
which the registry records per workspace. ``complete_json``/``complete_text``
remain as thin wrappers for existing callers.

Errors are typed so callers can decide: :class:`AIUnavailable` (fall back to
rules), :class:`AIRefused` (the model declined; do not retry blindly),
:class:`AIRetryable` (rate limit / 5xx / network; the task queue retries).
"""

from __future__ import annotations

from abc import ABC
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional

__all__ = ["AIError", "AIProvider", "AIQuotaExhausted", "AIRefused", "AIResult", "AIRetryable", "AIUnavailable", "AIUsage", "PRICES",
           "estimate_cost", "validate_against_schema"]

#: USD per million tokens (input, output) for models with published prices.
#: Anything not listed has no cost estimate unless the workspace configures one.
PRICES: Dict[str, tuple] = {
    "claude-opus-5": (5.0, 25.0), "claude-opus-5-5": (4.0, 20.0), "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0), "claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-8": (5.0, 25.0), "claude-sonnet-4-6": (3.0, 15.0),
}


def estimate_cost(model: str, prompt_tokens: Optional[int], completion_tokens: Optional[int],
                  prices: Optional[Mapping[str, tuple]] = None) -> Optional[float]:
    price = (prices or {}).get(model) or PRICES.get(model)
    if price is None or prompt_tokens is None or completion_tokens is None:
        return None
    return round((prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000, 6)


@dataclass
class AIUsage:
    provider: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    request_id: Optional[str] = None
    estimated_cost_usd: Optional[float] = None
    latency_ms: Optional[float] = None

    @property
    def total_tokens(self) -> Optional[int]:
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "total_tokens": self.total_tokens}


@dataclass
class AIResult:
    text: str = ""
    data: Optional[Dict[str, Any]] = None
    usage: Optional[AIUsage] = None
    raw_stop_reason: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


class AIError(Exception):
    """Base class for AI provider failures."""


class AIUnavailable(AIError):
    """No usable model for this call: not configured, not allowed, or rules-only."""


class AIRefused(AIError):
    """The model declined the request (e.g. a safety refusal)."""


class AIRetryable(AIError):
    """A transient failure worth retrying later: rate limit, overload, network."""


class AIQuotaExhausted(AIRetryable):
    """The provider's quota is used up (HTTP 429 RESOURCE_EXHAUSTED).

    ``retry_after_s`` is the provider's suggested wait when it gave one; ``daily`` is
    true when a per-day quota ran out (it resets at the provider's day boundary)."""

    def __init__(self, message: str, *, retry_after_s: Optional[float] = None, daily: bool = False) -> None:
        super().__init__(message)
        self.retry_after_s, self.daily = retry_after_s, daily


def validate_against_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> List[str]:
    """A small JSON-schema subset validator (type, required, properties, items, enum,
    additionalProperties=false). Returns problems; empty means valid.

    Enough to refuse a malformed model answer without adding a dependency.
    """
    problems: List[str] = []
    expected = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if isinstance(expected, list):
        kinds = expected
    elif expected:
        kinds = [expected]
    else:
        kinds = []
    if kinds:
        ok = False
        for kind in kinds:
            if kind == "integer":
                ok = ok or (isinstance(value, int) and not isinstance(value, bool))
            elif kind == "number":
                ok = ok or (isinstance(value, (int, float)) and not isinstance(value, bool))
            elif kind in types:
                ok = ok or isinstance(value, types[kind])
        if not ok:
            return [f"{path}: expected {'/'.join(kinds)}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        props: Dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}.{name}: required")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    problems.append(f"{path}.{name}: not allowed")
        for name, sub in props.items():
            if name in value:
                problems.extend(validate_against_schema(value[name], sub, f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for i, item in enumerate(value):
            problems.extend(validate_against_schema(item, schema["items"], f"{path}[{i}]"))
    return problems


class AIProvider(ABC):
    """Adapters implement ``_generate`` (and ``_stream`` when they can). Older
    adapters and test doubles that implement ``complete_json``/``complete_text``
    directly keep working: the new methods route through whichever exists."""

    #: Short identifier, e.g. ``"claude"``.
    name: str = "provider"
    #: Whether calls leave this process (send data to a third party).
    external: bool = True
    model: str = ""
    #: Usage of the most recent call (tokens, request id, estimated cost).
    last_usage: Optional[AIUsage] = None

    # --- what adapters implement -------------------------------------------------------

    def _generate(self, system: str, prompt: str, *, max_tokens: int,
                  schema: Optional[Mapping[str, Any]] = None) -> AIResult:
        raise AIUnavailable(f"{self.name} does not implement generation")

    def _stream(self, system: str, prompt: str, *, max_tokens: int) -> Iterator[str]:
        yield self.generate(system, prompt, max_tokens=max_tokens).text

    # --- the interface ----------------------------------------------------------------

    def generate(self, system: str, prompt: str, *, max_tokens: int = 2000) -> AIResult:
        if type(self).complete_text is not AIProvider.complete_text:  # a legacy adapter / test double
            return AIResult(text=self.complete_text(system, prompt, max_tokens=max_tokens), usage=self.last_usage)
        result = self._generate(system, prompt, max_tokens=max_tokens)
        self.last_usage = result.usage
        return result

    def structured_generate(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                            max_tokens: int = 4000) -> AIResult:
        if type(self).complete_json is not AIProvider.complete_json:
            return AIResult(data=self.complete_json(system, prompt, schema, max_tokens=max_tokens),
                            usage=self.last_usage)
        result = self._generate(system, prompt, max_tokens=max_tokens, schema=schema)
        self.last_usage = result.usage
        result.data = self._checked(result.data, schema)
        return result

    def stream(self, system: str, prompt: str, *, max_tokens: int = 2000) -> Iterator[str]:
        """Text chunks as they arrive (one chunk when the provider cannot stream)."""
        return self._stream(system, prompt, max_tokens=max_tokens)

    def health_check(self, *, live: bool = False) -> Dict[str, Any]:
        """Configuration status. ``live=True`` makes one tiny request — only when explicitly asked."""
        status: Dict[str, Any] = {"provider": self.name, "model": self.model, "configured": True, "live": False}
        if live:
            result = self.generate("Reply with the single word: ok", "ping", max_tokens=16)
            status.update(live=True, ok=bool(result.text), usage=result.usage.as_dict() if result.usage else None)
        return status

    # --- compatibility wrappers ----------------------------------------------------------

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        """Return a JSON object that validates against ``schema``."""
        return self.structured_generate(system, prompt, schema, max_tokens=max_tokens).data or {}

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        """Return free text."""
        return self.generate(system, prompt, max_tokens=max_tokens).text

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "external": self.external, "model": self.model}

    def _checked(self, value: Any, schema: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise AIError(f"{self.name} returned {type(value).__name__}, not a JSON object")
        problems = validate_against_schema(value, schema)
        if problems:
            raise AIError(f"{self.name} answer did not match the schema: {'; '.join(problems[:5])}")
        return value
