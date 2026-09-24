"""Gemini and OpenAI-compatible providers over plain REST (``requests``).

Keys are server-side only — the workspace's encrypted provider connection, or
else the environment:

* Gemini: ``GEMINI_API_KEY`` (sent in the ``x-goog-api-key`` header, never the URL);
* OpenAI-compatible: ``OPENAI_COMPATIBLE_API_KEY`` + ``OPENAI_COMPATIBLE_BASE_URL``
  (+ ``OPENAI_COMPATIBLE_MODEL``) — any server implementing ``POST /chat/completions``,
  e.g. a self-hosted model. The base URL must be https (or localhost).

Both request JSON output with the provider's own schema/JSON mode, and the
platform re-validates the answer. Token usage comes from the provider's response
(``usageMetadata`` / ``usage``); a cost estimate is made only when the workspace
has configured a price for the model — these providers' prices are not assumed.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from cloud.intel.ai.base import AIError, AIProvider, AIRefused, AIResult, AIRetryable, AIUnavailable, AIUsage, estimate_cost

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
    if response.status_code == 429 or response.status_code >= 500:
        raise AIRetryable(f"AI provider returned {response.status_code}")
    if response.status_code >= 400:
        raise AIError(f"AI provider rejected the request ({response.status_code}): {response.text[:300]}")
    try:
        return response.json(), dict(getattr(response, "headers", {}) or {})
    except ValueError as error:
        raise AIError("AI provider returned a non-JSON response") from error


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

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None, session: Any = None,
                 timeout: float = 120.0, prices: Optional[Mapping[str, tuple]] = None) -> None:
        import requests

        self.model = model or "gemini-2.5-pro"
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._key:
            raise AIUnavailable("GEMINI_API_KEY is not set on the server and no workspace key is saved")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._prices = dict(prices or {})

    def _generate(self, system: str, prompt: str, *, max_tokens: int,
                  schema: Optional[Mapping[str, Any]] = None) -> AIResult:
        started = time.monotonic()
        config: Dict[str, Any] = {"maxOutputTokens": max_tokens}
        if schema is not None:
            config["responseMimeType"] = "application/json"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
        data, headers = _post(self._session, f"{self.BASE}/{self.model}:generateContent",
                              headers={"x-goog-api-key": self._key}, body=body, timeout=self._timeout)
        meta = data.get("usageMetadata") or {}
        prompt_tokens, completion_tokens = _int(meta.get("promptTokenCount")), _int(meta.get("candidatesTokenCount"))
        usage = AIUsage(provider=self.name, model=data.get("modelVersion") or self.model,
                        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                        request_id=data.get("responseId") or headers.get("x-request-id"),
                        estimated_cost_usd=estimate_cost(self.model, prompt_tokens, completion_tokens, self._prices),
                        latency_ms=(time.monotonic() - started) * 1000)
        self.last_usage = usage
        try:
            candidate = data["candidates"][0]
        except (KeyError, IndexError) as error:
            raise AIError("Gemini returned no candidates") from error
        if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise AIRefused(f"Gemini declined the request ({candidate.get('finishReason')})")
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
        result = AIResult(text=text, usage=usage, raw_stop_reason=candidate.get("finishReason"))
        if schema is not None:
            result.data = _loads(text, self.name)
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
