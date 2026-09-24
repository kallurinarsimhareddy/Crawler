"""The default provider: no model at all.

Returned by :class:`~cloud.intel.ai.registry.AIRegistry` whenever external AI is
not configured or the workspace has not allowed its data to be sent out. It
raises :class:`AIUnavailable`, which every caller treats as "use the
deterministic path" — so the platform works identically offline, just without
the extra reach a model gives on unusual inputs.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from cloud.intel.ai.base import AIProvider, AIUnavailable

__all__ = ["RulesProvider"]


class RulesProvider(AIProvider):
    name = "rules"
    external = False
    model = "deterministic"

    def __init__(self, reason: str = "no external AI provider is configured") -> None:
        self.reason = reason

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        raise AIUnavailable(self.reason)

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        raise AIUnavailable(self.reason)

    def describe(self) -> Dict[str, Any]:
        return {**super().describe(), "reason": self.reason}
