"""Claude, through the official ``anthropic`` Python SDK.

The SDK is an optional dependency (``cloud/intel/requirements-ai.txt``) and is
imported only when a Claude provider is actually built. The API key is read
server-side from ``ANTHROPIC_API_KEY``; it never reaches the browser, a
``VITE_*`` variable or the database.

Request shape (Messages API):

* model ``claude-opus-5`` unless ``CAREERCLOUD_AI_MODEL`` says otherwise;
* ``thinking={"type": "adaptive"}``;
* JSON answers via ``output_config={"format": {"type": "json_schema", "schema": ...}}``,
  then re-validated here;
* ``stop_reason == "refusal"`` is checked before any content is read.

Errors map onto the platform's typed errors: rate limits, 5xx and connection
failures are :class:`AIRetryable`; a bad request is :class:`AIError`; a refusal
is :class:`AIRefused`.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping, Optional

from cloud.intel.ai.base import AIError, AIProvider, AIRefused, AIRetryable, AIUnavailable

__all__ = ["ClaudeProvider", "DEFAULT_CLAUDE_MODEL"]

DEFAULT_CLAUDE_MODEL = "claude-opus-5"


class ClaudeProvider(AIProvider):
    name = "claude"
    external = True

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None, client: Any = None,
                 timeout: float = 120.0) -> None:
        self.model = model or DEFAULT_CLAUDE_MODEL
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AIUnavailable("ANTHROPIC_API_KEY is not set on the server")
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - depends on the install
            raise AIUnavailable("the anthropic SDK is not installed (cloud/intel/requirements-ai.txt)") from error
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=2)

    def _call(self, **kwargs: Any) -> Any:
        try:
            import anthropic
        except ImportError:  # pragma: no cover - fake clients in tests need no SDK types
            anthropic = None  # type: ignore[assignment]
        try:
            return self._client.messages.create(model=self.model, thinking={"type": "adaptive"}, **kwargs)
        except Exception as error:  # noqa: BLE001 - mapped to typed platform errors below
            if anthropic is not None:
                if isinstance(error, anthropic.RateLimitError):
                    raise AIRetryable(f"Claude rate limit: {error}") from error
                if isinstance(error, anthropic.BadRequestError):
                    raise AIError(f"Claude rejected the request: {error}") from error
                if isinstance(error, anthropic.APIStatusError):
                    if getattr(error, "status_code", 0) >= 500:
                        raise AIRetryable(f"Claude server error {error.status_code}") from error
                    raise AIError(f"Claude API error {getattr(error, 'status_code', '?')}: {error}") from error
                if isinstance(error, anthropic.APIConnectionError):
                    raise AIRetryable(f"cannot reach Claude: {error}") from error
            raise

    @staticmethod
    def _text(response: Any) -> str:
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            raise AIRefused(f"Claude declined the request{f' ({category})' if category else ''}")
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                return block.text
        raise AIError("Claude returned no text")

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        response = self._call(
            max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": dict(schema)}},
        )
        text = self._text(response)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise AIError(f"Claude returned invalid JSON: {error}") from error
        return self._checked(value, schema)

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        response = self._call(max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": prompt}])
        return self._text(response)
