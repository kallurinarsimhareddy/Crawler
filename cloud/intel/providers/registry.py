"""Per-workspace provider connections and their credentials.

A workspace connects a provider (ZoomInfo, Seamless, EmailListVerify, a keyed
job source…) by storing credentials here. The rules:

* **Private to the workspace.** Rows live in ``provider_connections`` under
  RLS; another workspace can neither read nor use them, and connectors are
  built per request from the caller's own workspace row.
* **Encrypted at rest** with Fernet using ``CAREERCLOUD_PLATFORM_SECRETS_KEY``
  (``platform.config.secrets_key``). With no key configured, storing a secret is
  refused outright rather than stored in the clear.
* **Write-only through the API.** :meth:`ProviderRegistry.list_connections`
  returns status, settings and a 4-character ``secret_hint`` — never the
  ciphertext, never a value. Only server-side code calls :meth:`get_secrets`.
* **Admins only** may set or clear credentials.
* **Honest status.** ``not_configured`` → ``configured`` (stored, never
  checked) → ``verified`` (a real call succeeded) or ``error``. A provider is
  never reported working until a live check says so.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, NotFoundError, ValidationError, utcnow

__all__ = ["CATALOG", "ProviderRegistry", "SecretsUnavailable"]

log = logging.getLogger(__name__)


class SecretsUnavailable(ValidationError):
    """Secrets cannot be stored or read: no encryption key is configured."""

    status = 503


def _catalog() -> Dict[str, Dict[str, Any]]:
    from cloud.intel.sources.adapters import ADAPTERS

    catalog: Dict[str, Dict[str, Any]] = {}
    for name, cls in ADAPTERS.items():
        catalog[name] = {"provider": name, "kind": "source", "label": cls.label, "access_method": cls.access_method,
                         "requires": list(cls.requires), "requirement": cls.requirement, "paid": cls.paid}
    catalog["zoominfo"] = {
        "provider": "zoominfo", "kind": "enrichment", "label": "ZoomInfo", "access_method": "api",
        "requires": ["client_id", "client_secret"], "paid": True,
        "requirement": ("a ZoomInfo GTM API application (OAuth client_credentials: client_id + client_secret) on an "
                        "account entitled to the Data API. Browser-login mode exists only as an operator-attended "
                        "desktop tool and cannot run unattended in the cloud.")}
    catalog["seamless"] = {
        "provider": "seamless", "kind": "enrichment", "label": "Seamless.AI", "access_method": "api",
        "requires": ["api_key"], "paid": True,
        "requirement": "a Seamless.AI API key with API access enabled on the workspace's own Seamless account"}
    catalog["partner_api"] = {
        "provider": "partner_api", "kind": "enrichment", "label": "Authorized partner API", "access_method": "partner",
        "requires": ["api_key"], "paid": True,
        "requirement": ("an API key from a data partner the workspace holds a contract with, plus an https base_url "
                        "in the connection settings (JSON contract: see cloud/intel/providers/enrichment.py)")}
    catalog["emaillistverify"] = {
        "provider": "emaillistverify", "kind": "email_validation", "label": "EmailListVerify",
        "access_method": "api", "requires": ["api_key"], "paid": True,
        "requirement": "an EmailListVerify API key (apps.emaillistverify.com) with purchased credits"}
    # AI model providers: the key is stored encrypted per workspace and used server-side only;
    # without one, the server's environment variable (if any) is used.
    catalog["claude"] = {
        "provider": "claude", "kind": "ai", "label": "Claude (Anthropic)", "access_method": "api",
        "requires": ["api_key"], "paid": True,
        "requirement": "an Anthropic API key (or ANTHROPIC_API_KEY on the server)"}
    catalog["gemini"] = {
        "provider": "gemini", "kind": "ai", "label": "Gemini (Google)", "access_method": "api",
        "requires": ["api_key"], "paid": True,
        "requirement": "a Gemini API key (or GEMINI_API_KEY on the server)"}
    catalog["openai_compatible"] = {
        "provider": "openai_compatible", "kind": "ai", "label": "OpenAI-compatible", "access_method": "api",
        "requires": ["api_key"], "paid": True,
        "requirement": ("an API key for an OpenAI-compatible /chat/completions endpoint; set base_url and model in "
                        "the connection settings (https only), or OPENAI_COMPATIBLE_* on the server")}
    return catalog


CATALOG_LAZY: Dict[str, Dict[str, Any]] = {}


def catalog() -> Dict[str, Dict[str, Any]]:
    if not CATALOG_LAZY:
        CATALOG_LAZY.update(_catalog())
    return CATALOG_LAZY


CATALOG = catalog  # callable; the adapters module is imported on first use


class ProviderRegistry:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- encryption -----------------------------------------------------------

    def _fernet(self):
        key = self.platform.config.secrets_key
        if not key:
            raise SecretsUnavailable("provider credentials cannot be stored: CAREERCLOUD_PLATFORM_SECRETS_KEY is "
                                     "not configured on the server")
        from cryptography.fernet import Fernet

        try:
            return Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as error:
            raise SecretsUnavailable("CAREERCLOUD_PLATFORM_SECRETS_KEY is not a valid Fernet key") from error

    # --- catalogue and connections --------------------------------------------------

    def describe(self, provider: str) -> Dict[str, Any]:
        try:
            return dict(catalog()[provider])
        except KeyError:
            raise NotFoundError(f"unknown provider {provider!r}") from None

    def connection(self, ctx: Ctx, provider: str) -> Optional[Dict[str, Any]]:
        return self.store.first(ctx, "provider_connections", {"provider": provider})

    @staticmethod
    def _public(row: Optional[Mapping[str, Any]], info: Mapping[str, Any]) -> Dict[str, Any]:
        out = {**info, "status": "not_configured", "secret_hint": None, "settings": {}, "last_checked_at": None,
               "last_error": None, "connected": False}
        if row is not None:
            out.update({"status": row["status"], "secret_hint": row["secret_hint"],
                        "settings": {k: v for k, v in (row["settings"] or {}).items() if k != "verified"},
                        "last_checked_at": row["last_checked_at"], "last_error": row["last_error"],
                        "connected": bool(row["secret_ciphertext"]), "label": row["label"] or info.get("label")})
        if not info.get("requires") and out["status"] == "not_configured":
            out["status"] = "available"
        return out

    def list_connections(self, ctx: Ctx, *, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = {r["provider"]: r for r in self.store.all(ctx, "provider_connections")}
        return [self._public(rows.get(name), info) for name, info in catalog().items()
                if kind is None or info["kind"] == kind]

    def set_credentials(self, ctx: Ctx, provider: str, secrets: Mapping[str, Any], *,
                        settings: Optional[Mapping[str, Any]] = None, label: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_admin()
        info = self.describe(provider)
        clean = {str(k): str(v) for k, v in (secrets or {}).items() if v not in (None, "")}
        missing = [name for name in info["requires"] if name not in clean]
        if missing:
            raise ValidationError(f"{info['label']} needs: {', '.join(missing)}")
        unknown = set(clean) - set(info["requires"]) - {"feed_token"}
        if unknown:
            raise ValidationError(f"{info['label']} does not take: {', '.join(sorted(unknown))}")
        ciphertext = self._fernet().encrypt(json.dumps(clean, sort_keys=True).encode()).decode()
        last = clean[info["requires"][0]] if info["requires"] else ""
        hint = f"…{last[-4:]}" if len(last) >= 8 else "…"
        access = {"official_api": "api", "public_api": "public", "authorized_account": "partner"}.get(
            info["access_method"], info["access_method"])
        values = {"kind": info["kind"], "status": "configured", "access_method": access,
                  "secret_ciphertext": ciphertext, "secret_hint": hint, "settings": dict(settings or {}),
                  "label": label or info["label"], "last_error": None, "private": True}
        row = self.connection(ctx, provider)
        if row is None:
            row = self.store.insert(ctx, "provider_connections", {"provider": provider, **values})
        else:
            row = self.store.update(ctx, "provider_connections", row["id"], values)
        # Never the values; only which fields were set.
        audit(self.store, ctx, "provider.credentials_set", entity_type="provider_connections", entity_id=row["id"],
              summary=f"{info['label']} credentials stored", changes={"fields": sorted(clean)})
        return self._public(row, info)

    def clear_credentials(self, ctx: Ctx, provider: str) -> None:
        ctx.require_admin()
        row = self.connection(ctx, provider)
        if row is None:
            raise NotFoundError(f"{provider} is not connected")
        self.store.update(ctx, "provider_connections", row["id"],
                          {"secret_ciphertext": None, "secret_hint": None, "status": "not_configured"})
        audit(self.store, ctx, "provider.credentials_cleared", entity_type="provider_connections", entity_id=row["id"])

    def update_settings(self, ctx: Ctx, provider: str, settings: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_admin()
        info = self.describe(provider)
        row = self.connection(ctx, provider)
        clean = {k: v for k, v in dict(settings).items() if k != "verified"}
        if row is None:
            row = self.store.insert(ctx, "provider_connections", {
                "provider": provider, "kind": info["kind"], "status": "not_configured",
                "settings": clean, "label": info["label"]})
        else:
            row = self.store.update(ctx, "provider_connections", row["id"],
                                    {"settings": {**(row["settings"] or {}), **clean}})
        return self._public(row, info)

    def get_secrets(self, ctx: Ctx, provider: str) -> Dict[str, str]:
        """Server-side only: the decrypted credentials, or ``{}`` when not connected."""
        row = self.connection(ctx, provider)
        if row is None or not row["secret_ciphertext"]:
            return {}
        from cryptography.fernet import InvalidToken

        try:
            return json.loads(self._fernet().decrypt(row["secret_ciphertext"].encode()))
        except InvalidToken as error:
            raise SecretsUnavailable(f"{provider} credentials cannot be decrypted with the configured key") from error

    def _settings(self, ctx: Ctx, provider: str) -> Dict[str, Any]:
        row = self.connection(ctx, provider)
        settings = dict((row or {}).get("settings") or {})
        if row is not None and row["status"] == "verified":
            settings["verified"] = True
        return settings

    # --- building connectors -------------------------------------------------------

    def source_adapter(self, ctx: Ctx, name: str, *, fetcher: Any = None):
        from cloud.intel.sources.adapters import adapter_class

        cls = adapter_class(name)
        secrets = self.get_secrets(ctx, name) if cls.requires else {}
        return cls(credentials=secrets, settings=self._settings(ctx, name), fetcher=fetcher)

    def enrichment(self, ctx: Ctx, name: str, *, session: Any = None):
        secrets = self.get_secrets(ctx, name)
        settings = self._settings(ctx, name)
        if name == "zoominfo":
            from cloud.intel.providers.zoominfo import ZoomInfoConnector

            return ZoomInfoConnector(secrets, settings=settings, session=session)
        if name == "seamless":
            from cloud.intel.providers.seamless import SeamlessConnector

            return SeamlessConnector(secrets, settings=settings, session=session)
        if name == "partner_api":
            from cloud.intel.providers.enrichment import PartnerApiConnector

            return PartnerApiConnector(secrets, settings=settings)
        raise NotFoundError(f"{name} is not an enrichment provider")

    def configured(self, ctx: Ctx, name: str) -> bool:
        row = self.connection(ctx, name)
        return bool(row and row["secret_ciphertext"] and row["status"] in ("configured", "verified"))

    # --- verification --------------------------------------------------------------

    def verify(self, ctx: Ctx, provider: str, *, allow_paid: bool = False, session: Any = None,
               fetcher: Any = None) -> Dict[str, Any]:
        """Check a connection against the live provider, spending nothing unless allowed."""
        ctx.require_write()
        info = self.describe(provider)
        try:
            if info["kind"] == "source":
                adapter = self.source_adapter(ctx, provider, fetcher=fetcher)
                result = adapter.health()
                if result["status"] == "configured_unverified":
                    from cloud.intel.sources.base import SourceQuery

                    adapter.run(SourceQuery(keywords="engineer", limit=1))
                    result = {"status": "ok", "detail": "a live search succeeded"}
            elif info["kind"] == "enrichment":
                result = self.enrichment(ctx, provider, session=session).verify(allow_paid=allow_paid)
            elif provider == "emaillistverify":
                from cloud.intel.email.providers import EmailListVerifyProvider

                result = EmailListVerifyProvider(self.get_secrets(ctx, provider).get("api_key", ""),
                                                 session=session).health(live=True)
            elif info["kind"] == "ai":
                # Configuration only: an AI call costs tokens, so the live test is the separate,
                # explicit "Test connection" action in AI settings (POST /agent/ai/test).
                has_key = bool(self.get_secrets(ctx, provider).get("api_key"))
                result = {"status": "configured_unverified" if has_key else "not_configured",
                          "detail": "key saved; use Test connection in AI settings for a live check" if has_key
                          else "no key saved"}
            else:
                result = {"status": "error", "detail": "no verification available"}
        except Exception as error:  # noqa: BLE001 - verification failures are results
            result = {"status": "error", "detail": f"{type(error).__name__}: {error}"[:500]}
        status = {"ok": "verified", "not_configured": "not_configured", "configured_unverified": "configured",
                  "blocked": "error", "error": "error"}.get(result.get("status"), "error")
        row = self.connection(ctx, provider)
        if row is not None:
            self.store.update(ctx, "provider_connections", row["id"], {
                "status": status, "last_checked_at": utcnow(),
                "last_error": None if status in ("verified", "configured") else str(result.get("detail"))[:2000]})
        audit(self.store, ctx, "provider.verify", entity_type="provider_connections",
              entity_id=row["id"] if row else None, summary=f"{provider}: {result.get('status')}")
        return {"provider": provider, **result, "connection_status": status}
