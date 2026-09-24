"""The provider-neutral AI interface.

The platform uses AI selectively — extraction, classification, entity matching,
research planning, summarisation, signal explanation, personalisation — and
never depends on it: every caller has a deterministic path, and
:class:`~cloud.intel.ai.rules.RulesProvider` (the default) makes that path the
one taken whenever no external provider is configured or the workspace has not
allowed its data to leave.

A provider answers two kinds of request:

* :meth:`AIProvider.complete_json` — a JSON object matching a JSON schema
  (validated here, not trusted);
* :meth:`AIProvider.complete_text` — free text.

Errors are typed so callers can decide: :class:`AIUnavailable` (fall back to
rules), :class:`AIRefused` (the model declined; do not retry blindly),
:class:`AIRetryable` (rate limit / 5xx / network; the task queue retries).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Mapping

__all__ = ["AIError", "AIProvider", "AIRefused", "AIRetryable", "AIUnavailable", "validate_against_schema"]


class AIError(Exception):
    """Base class for AI provider failures."""


class AIUnavailable(AIError):
    """No usable model for this call: not configured, not allowed, or rules-only."""


class AIRefused(AIError):
    """The model declined the request (e.g. a safety refusal)."""


class AIRetryable(AIError):
    """A transient failure worth retrying later: rate limit, overload, network."""


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
    #: Short identifier, e.g. ``"claude"``.
    name: str = "provider"
    #: Whether calls leave this process (send data to a third party).
    external: bool = True
    model: str = ""

    @abstractmethod
    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        """Return a JSON object that validates against ``schema``."""

    @abstractmethod
    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        """Return free text."""

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "external": self.external, "model": self.model}

    def _checked(self, value: Any, schema: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise AIError(f"{self.name} returned {type(value).__name__}, not a JSON object")
        problems = validate_against_schema(value, schema)
        if problems:
            raise AIError(f"{self.name} answer did not match the schema: {'; '.join(problems[:5])}")
        return value
