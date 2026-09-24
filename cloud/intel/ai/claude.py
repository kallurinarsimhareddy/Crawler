"""Claude, through the official ``anthropic`` Python SDK.

The SDK is an optional dependency (``cloud/intel/requirements-ai.txt``) and is
imported only when a Claude provider is actually built. The API key comes from
the workspace's encrypted provider connection or, failing that, the server's
``ANTHROPIC_API_KEY``; it never reaches the browser, a ``VITE_*`` variable or a
log line.

Request shape (Messages API):

* model ``claude-opus-5`` unless the workspace or ``CAREERCLOUD_AI_MODEL`` says otherwise;
* ``thinking={"type": "adaptive"}``;
* JSON answers via ``output_config={"format": {"type": "json_schema", "schema": ...}}``,
  then re-validated here;
* ``stop_reason == "refusal"`` is checked before any content is read;
* for ``claude-opus-5`` / ``claude-fable-5-1``, server-side refusal fallbacks
  (``fallbacks="default"`` with beta ``server-side-fallback-2026-07-01``) are on
  by default, so a policy decline is re-run on a fallback model inside the same
  call. Turn off with ``server_fallbacks=False``.

Usage: ``response.usage.input_tokens`` / ``output_tokens`` and the response's
request id are captured on every call, with a cost estimate from
:data:`~cloud.intel.ai.base.PRICES`.

Errors map onto the platform's typed errors: rate limits, 5xx and connection
failures are :class:`AIRetryable`; a bad key is :class:`AIUnavailable`; a bad
request is :class:`AIError`; a refusal is :class:`AIRefused`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator, Mapping, Optional

from cloud.intel.ai.base import (AIError, AIProvider, AIRefused, AIResult, AIRetryable, AIUnavailable, AIUsage,
                                 estimate_cost)

__all__ = ["ClaudeProvider", "DEFAULT_CLAUDE_MODEL"]

DEFAULT_CLAUDE_MODEL = "claude-opus-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})


class ClaudeProvider(AIProvider):
    name = "claude"
    external = True

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None, client: Any = None,
                 timeout: float = 120.0, server_fallbacks: Optional[bool] = None) -> None:
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.server_fallbacks = (self.model in _FALLBACK_MODELS) if server_fallbacks is None else server_fallbacks
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AIUnavailable("ANTHROPIC_API_KEY is not set on the server and no workspace key is saved")
        try:
            import anthropic
        except ImportError as error:  # pragma: no cover - depends on the install
            raise AIUnavailable("the anthropic SDK is not installed (cloud/intel/requirements-ai.txt)") from error
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=2)

    # --- transport ----------------------------------------------------------------------

    def _endpoint(self):
        beta = getattr(self._client, "beta", None)
        if self.server_fallbacks and beta is not None and hasattr(beta, "messages"):
            return beta.messages, {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
        return self._client.messages, {}

    def _call(self, **kwargs: Any) -> Any:
        try:
            import anthropic
        except ImportError:  # pragma: no cover - fake clients in tests need no SDK types
            anthropic = None  # type: ignore[assignment]
        endpoint, extra = self._endpoint()
        try:
            return endpoint.create(model=self.model, thinking={"type": "adaptive"}, **extra, **kwargs)
        except Exception as error:  # noqa: BLE001 - mapped to typed platform errors below
            if anthropic is not None:
                if isinstance(error, anthropic.RateLimitError):
                    raise AIRetryable(f"Claude rate limit: {error}") from error
                if isinstance(error, anthropic.AuthenticationError):
                    raise AIUnavailable("Claude rejected the API key (authentication failed)") from error
                if isinstance(error, anthropic.BadRequestError):
                    raise AIError(f"Claude rejected the request: {error}") from error
                if isinstance(error, anthropic.APIStatusError):
                    if getattr(error, "status_code", 0) >= 500:
                        raise AIRetryable(f"Claude server error {error.status_code}") from error
                    raise AIError(f"Claude API error {getattr(error, 'status_code', '?')}: {error}") from error
                if isinstance(error, anthropic.APIConnectionError):
                    raise AIRetryable(f"cannot reach Claude: {error}") from error
            raise

    def _usage(self, response: Any, started: float) -> AIUsage:
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "input_tokens", None)
        completion = getattr(usage, "output_tokens", None)
        model = getattr(response, "model", None) or self.model
        if not isinstance(model, str):
            model = self.model
        request_id = getattr(response, "_request_id", None) or getattr(response, "id", None)
        return AIUsage(provider=self.name, model=model,
                       prompt_tokens=prompt if isinstance(prompt, int) else None,
                       completion_tokens=completion if isinstance(completion, int) else None,
                       request_id=request_id if isinstance(request_id, str) else None,
                       estimated_cost_usd=estimate_cost(model, prompt if isinstance(prompt, int) else None,
                                                        completion if isinstance(completion, int) else None),
                       latency_ms=(time.monotonic() - started) * 1000)

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

    # --- the adapter --------------------------------------------------------------------

    def _generate(self, system: str, prompt: str, *, max_tokens: int,
                  schema: Optional[Mapping[str, Any]] = None) -> AIResult:
        started = time.monotonic()
        kwargs: dict = {"max_tokens": max_tokens, "system": system,
                        "messages": [{"role": "user", "content": prompt}]}
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": dict(schema)}}
        response = self._call(**kwargs)
        usage = self._usage(response, started)
        self.last_usage = usage
        text = self._text(response)
        result = AIResult(text=text, usage=usage, raw_stop_reason=getattr(response, "stop_reason", None))
        if schema is not None:
            try:
                result.data = json.loads(text)
            except json.JSONDecodeError as error:
                raise AIError(f"Claude returned invalid JSON: {error}") from error
        return result

    def _stream(self, system: str, prompt: str, *, max_tokens: int) -> Iterator[str]:
        stream_fn = getattr(self._client.messages, "stream", None)
        if stream_fn is None:  # a client without streaming (e.g. a test double): one chunk
            yield self.generate(system, prompt, max_tokens=max_tokens).text
            return
        started = time.monotonic()
        with stream_fn(model=self.model, thinking={"type": "adaptive"}, max_tokens=max_tokens, system=system,
                       messages=[{"role": "user", "content": prompt}]) as stream:
            for chunk in stream.text_stream:
                yield chunk
            final = stream.get_final_message()
        self.last_usage = self._usage(final, started)
        self._text(final)  # raises on a refusal once the stream has ended
