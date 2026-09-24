"""Gemini and OpenAI-compatible providers over plain REST (``requests``).

Keys are server-side only — the workspace's encrypted provider connection, or
else the environment:

* Gemini: ``GEMINI_API_KEY`` (sent in the ``x-goog-api-key`` header, never the URL);
* OpenAI-compatible: ``OPENAI_COMPATIBLE_API_KEY`` + ``OPENAI_COMPATIBLE_BASE_URL``
  (+ ``OPENAI_COMPATIBLE_MODEL``) — any server implementing ``POST /chat/completions``,
  e.g. a self-hosted model. The base URL must be https (or localhost).

Structured output: Gemini is asked through **function calling** — one declared
function whose parameters are the schema, with the call forced (``mode: ANY``) —
and the function's arguments are the answer; OpenAI-compatible servers use JSON
mode. Either way the platform re-validates the answer. Token usage comes from the
provider's response (``usageMetadata`` / ``usage``; Gemini's thinking tokens count
as output); a cost estimate is made only when the workspace has configured a price
for the model — these providers' prices are not assumed. On Gemini's free tier
(``free_tier=True``) every call costs $0 by definition and is recorded as such.

HTTP 429 with ``RESOURCE_EXHAUSTED`` raises :class:`AIQuotaExhausted` (with the
provider's retry delay and whether a per-day quota ran out); the registry's
free-only mode turns that into "Free AI quota exhausted" and the rule-based path.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from cloud.intel.ai.base import (AIError, AIProvider, AIQuotaExhausted, AIRefused, AIResult, AIRetryable,
                                 AIUnavailable, AIUsage, estimate_cost)

__all__ = ["GeminiProvider", "OpenAICompatibleProvider"]


def _post(session: Any, url: str, *, headers: Dict[str, str], body: Dict[str, Any], timeout: float
          ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    import requests

    try:
        response = session.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as error:
        raise AIRetryable(f"cannot reach the AI provider: {type(error).__name__}") from error
    if response.status_code in (401, 403):
        raise AIUnavailable(f"the AI provider rejected the key ({response.status_code})")
    if response.status_code == 429:
        _raise_quota(response)
    if response.status_code >= 500:
        raise AIRetryable(f"AI provider returned {response.status_code}")
    if response.status_code >= 400:
        raise AIError(f"AI provider rejected the request ({response.status_code}): {response.text[:300]}")
    try:
        return response.json(), dict(getattr(response, "headers", {}) or {})
    except ValueError as error:
        raise AIError("AI provider returned a non-JSON response") from error


def _raise_quota(response: Any) -> None:
    """429: a used-up quota (RESOURCE_EXHAUSTED) or a plain rate limit. Never includes request data."""
    try:
        error = (response.json() or {}).get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    if error.get("status") != "RESOURCE_EXHAUSTED":
        raise AIRetryable("AI provider returned 429")
    retry_after, quota_ids = None, []
    for detail in error.get("details") or []:
        if not isinstance(detail, Mapping):
            continue
        delay = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay") or ""))
        if delay:
            retry_after = float(delay.group(1))
        quota_ids += [str(v.get("quotaId") or "") for v in detail.get("violations") or [] if isinstance(v, Mapping)]
    daily = any("perday" in q.lower() for q in quota_ids)
    raise AIQuotaExhausted(f"AI provider quota exhausted ({', '.join(q for q in quota_ids if q) or 'RESOURCE_EXHAUSTED'})"
                           [:300], retry_after_s=retry_after, daily=daily)


#: JSON-schema keywords Gemini's function-declaration schema (an OpenAPI subset) understands.
_GEMINI_SCHEMA_KEYS = {"description", "enum", "format", "minItems", "maxItems", "minimum", "maximum"}


def _gemini_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """Translate the platform's JSON schema into Gemini's subset: upper-case types,
    ``["string", "null"]`` -> ``nullable``, unsupported keywords dropped (the platform
    still validates the answer against the original schema)."""
    out: Dict[str, Any] = {k: v for k, v in schema.items() if k in _GEMINI_SCHEMA_KEYS}
    kind = schema.get("type")
    if isinstance(kind, list):
        out["nullable"] = "null" in kind
        kind = next((k for k in kind if k != "null"), "string")
    if kind:
        out["type"] = str(kind).upper()
    if isinstance(schema.get("properties"), Mapping):
        out["properties"] = {name: _gemini_schema(sub) for name, sub in schema["properties"].items()}
    if isinstance(schema.get("items"), Mapping):
        out["items"] = _gemini_schema(schema["items"])
    if schema.get("required"):
        out["required"] = list(schema["required"])
    return out


def _loads(text: str, provider: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise AIError(f"{provider} returned invalid JSON") from error


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class GeminiProvider(AIProvider):
    name = "gemini"
    external = True
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"
    DEFAULT_MODEL = "gemini-3.8-flash"
    #: The one function a structured request declares; its arguments are the answer.
    ANSWER_FUNCTION = "submit_answer"
    #: Thinking models spend output tokens on reasoning before the answer, so a small
    #: caller budget (e.g. 800) could leave nothing for the answer itself.
    MIN_OUTPUT_TOKENS = 4096

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None, session: Any = None,
                 timeout: float = 120.0, prices: Optional[Mapping[str, tuple]] = None, free_tier: bool = False) -> None:
        import requests

        self.model = model or self.DEFAULT_MODEL
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._key:
            raise AIUnavailable("GEMINI_API_KEY is not set on the server and no workspace key is saved")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._prices = dict(prices or {})
        self.free_tier = free_tier

    def _generate(self, system: str, prompt: str, *, max_tokens: int,
                  schema: Optional[Mapping[str, Any]] = None) -> AIResult:
        started = time.monotonic()
        body: Dict[str, Any] = {"systemInstruction": {"parts": [{"text": system}]},
                                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "generationConfig": {"maxOutputTokens": max(max_tokens, self.MIN_OUTPUT_TOKENS)}}
        if schema is not None:
            body["tools"] = [{"functionDeclarations": [{
                "name": self.ANSWER_FUNCTION, "description": "Return the answer. Always call this function.",
                "parameters": _gemini_schema(schema)}]}]
            body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY",
                                                            "allowedFunctionNames": [self.ANSWER_FUNCTION]}}
        data, headers = _post(self._session, f"{self.BASE}/{self.model}:generateContent",
                              headers={"x-goog-api-key": self._key}, body=body, timeout=self._timeout)
        meta = data.get("usageMetadata") or {}
        prompt_tokens = _int(meta.get("promptTokenCount"))
        answer_tokens, thinking_tokens = _int(meta.get("candidatesTokenCount")), _int(meta.get("thoughtsTokenCount"))
        completion_tokens = (None if answer_tokens is None and thinking_tokens is None
                             else (answer_tokens or 0) + (thinking_tokens or 0))
        cost = 0.0 if self.free_tier else estimate_cost(self.model, prompt_tokens, completion_tokens, self._prices)
        usage = AIUsage(provider=self.name, model=data.get("modelVersion") or self.model,
                        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                        request_id=data.get("responseId") or headers.get("x-request-id"),
                        estimated_cost_usd=cost, latency_ms=(time.monotonic() - started) * 1000)
        self.last_usage = usage
        try:
            candidate = data["candidates"][0]
        except (KeyError, IndexError) as error:
            raise AIError("Gemini returned no candidates") from error
        if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise AIRefused(f"Gemini declined the request ({candidate.get('finishReason')})")
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        result = AIResult(text=text, usage=usage, raw_stop_reason=candidate.get("finishReason"),
                          extra={"thinking_tokens": thinking_tokens, "free_tier": self.free_tier})
        if schema is not None:
            call = next((p["functionCall"] for p in parts if isinstance(p.get("functionCall"), Mapping)
                         and p["functionCall"].get("name") == self.ANSWER_FUNCTION), None)
            if call is not None:
                result.data, result.extra["structured_via"] = dict(call.get("args") or {}), "function_call"
            else:  # a model that answered in text anyway
                result.data, result.extra["structured_via"] = _loads(text, self.name), "json_text"
        return result


class OpenAICompatibleProvider(AIProvider):
    name = "openai_compatible"
    external = True

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None,
                 base_url: Optional[str] = None, session: Any = None, timeout: float = 120.0,
                 prices: Optional[Mapping[str, tuple]] = None) -> None:
        import requests

        self._key = api_key or os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        self._base = (base_url or os.environ.get("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("OPENAI_COMPATIBLE_MODEL") or ""
        if not self._key or not self._base or not self.model:
            raise AIUnavailable("OPENAI_COMPATIBLE_API_KEY, OPENAI_COMPATIBLE_BASE_URL and a model are required")
        if not self._base.startswith("https://") and not self._base.startswith("http://localhost"):
            raise AIUnavailable("OPENAI_COMPATIBLE_BASE_URL must be https")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._prices = dict(prices or {})

    def _generate(self, system: str, prompt: str, *, max_tokens: int,
                  schema: Optional[Mapping[str, Any]] = None) -> AIResult:
        started = time.monotonic()
        body: Dict[str, Any] = {"model": self.model, "max_tokens": max_tokens,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": dict(schema)}}
        data, headers = _post(self._session, f"{self._base}/chat/completions",
                              headers={"Authorization": f"Bearer {self._key}"}, body=body, timeout=self._timeout)
        meta = data.get("usage") or {}
        prompt_tokens, completion_tokens = _int(meta.get("prompt_tokens")), _int(meta.get("completion_tokens"))
        usage = AIUsage(provider=self.name, model=data.get("model") or self.model, prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens, request_id=data.get("id") or headers.get("x-request-id"),
                        estimated_cost_usd=estimate_cost(self.model, prompt_tokens, completion_tokens, self._prices),
                        latency_ms=(time.monotonic() - started) * 1000)
        self.last_usage = usage
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as error:
            raise AIError("provider returned no choices") from error
        if choice.get("finish_reason") == "content_filter":
            raise AIRefused("the provider's content filter declined the request")
        text = choice.get("message", {}).get("content") or ""
        result = AIResult(text=text, usage=usage, raw_stop_reason=choice.get("finish_reason"))
        if schema is not None:
            result.data = _loads(text, self.name)
        return result
