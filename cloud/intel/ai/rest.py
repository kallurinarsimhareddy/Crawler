"""Gemini and OpenAI-compatible providers over plain REST (``requests``).

Keys are read server-side only:

* Gemini: ``GEMINI_API_KEY`` (sent in the ``x-goog-api-key`` header, never the URL);
* OpenAI-compatible: ``OPENAI_COMPATIBLE_API_KEY`` + ``OPENAI_COMPATIBLE_BASE_URL``
  (any server implementing ``POST /chat/completions``, e.g. a self-hosted model).

Both request JSON output with the provider's own schema/JSON mode and the
platform re-validates the answer against the schema.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping, Optional

from cloud.intel.ai.base import AIError, AIProvider, AIRetryable, AIUnavailable

__all__ = ["GeminiProvider", "OpenAICompatibleProvider"]


def _post(session: Any, url: str, *, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    import requests

    try:
        response = session.post(url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as error:
        raise AIRetryable(f"cannot reach the AI provider: {error}") from error
    if response.status_code == 429 or response.status_code >= 500:
        raise AIRetryable(f"AI provider returned {response.status_code}")
    if response.status_code >= 400:
        raise AIError(f"AI provider rejected the request ({response.status_code}): {response.text[:300]}")
    try:
        return response.json()
    except ValueError as error:
        raise AIError("AI provider returned a non-JSON response") from error


def _loads(text: str, provider: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        raise AIError(f"{provider} returned invalid JSON") from error


class GeminiProvider(AIProvider):
    name = "gemini"
    external = True
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None, session: Any = None,
                 timeout: float = 120.0) -> None:
        import requests

        self.model = model or "gemini-2.5-pro"
        self._key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._key:
            raise AIUnavailable("GEMINI_API_KEY is not set on the server")
        self._session = session or requests.Session()
        self._timeout = timeout

    def _generate(self, system: str, prompt: str, config: Dict[str, Any]) -> str:
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": config}
        data = _post(self._session, f"{self.BASE}/{self.model}:generateContent",
                     headers={"x-goog-api-key": self._key}, body=body, timeout=self._timeout)
        try:
            candidate = data["candidates"][0]
        except (KeyError, IndexError) as error:
            raise AIError("Gemini returned no candidates") from error
        if candidate.get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            from cloud.intel.ai.base import AIRefused

            raise AIRefused(f"Gemini declined the request ({candidate.get('finishReason')})")
        return "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        text = self._generate(system, prompt, {"responseMimeType": "application/json",
                                                "maxOutputTokens": max_tokens})
        return self._checked(_loads(text, self.name), schema)

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        return self._generate(system, prompt, {"maxOutputTokens": max_tokens})


class OpenAICompatibleProvider(AIProvider):
    name = "openai_compatible"
    external = True

    def __init__(self, *, model: Optional[str] = None, api_key: Optional[str] = None,
                 base_url: Optional[str] = None, session: Any = None, timeout: float = 120.0) -> None:
        import requests

        self._key = api_key or os.environ.get("OPENAI_COMPATIBLE_API_KEY")
        self._base = (base_url or os.environ.get("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        self.model = model or os.environ.get("OPENAI_COMPATIBLE_MODEL") or ""
        if not self._key or not self._base or not self.model:
            raise AIUnavailable("OPENAI_COMPATIBLE_API_KEY, _BASE_URL and a model are required")
        if not self._base.startswith("https://") and not self._base.startswith("http://localhost"):
            raise AIUnavailable("OPENAI_COMPATIBLE_BASE_URL must be https")
        self._session = session or requests.Session()
        self._timeout = timeout

    def _chat(self, system: str, prompt: str, extra: Dict[str, Any]) -> str:
        body = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                  {"role": "user", "content": prompt}], **extra}
        data = _post(self._session, f"{self._base}/chat/completions",
                     headers={"Authorization": f"Bearer {self._key}"}, body=body, timeout=self._timeout)
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as error:
            raise AIError("provider returned no choices") from error
        if choice.get("finish_reason") == "content_filter":
            from cloud.intel.ai.base import AIRefused

            raise AIRefused("the provider's content filter declined the request")
        return choice.get("message", {}).get("content") or ""

    def complete_json(self, system: str, prompt: str, schema: Mapping[str, Any], *,
                      max_tokens: int = 4000) -> Dict[str, Any]:
        text = self._chat(system, prompt, {
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": dict(schema)}}})
        return self._checked(_loads(text, self.name), schema)

    def complete_text(self, system: str, prompt: str, *, max_tokens: int = 2000) -> str:
        return self._chat(system, prompt, {"max_tokens": max_tokens})
