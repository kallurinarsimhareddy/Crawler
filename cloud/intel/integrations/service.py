"""Workspace integrations: Slack, signed webhooks, Google Workspace, Microsoft 365
and their calendars.

Every integration is a ``provider_connections`` row (kind ``messaging``,
``webhook``, ``integration`` or ``calendar``) whose secrets are Fernet-encrypted
with the server's platform secrets key — the same scheme as provider API keys.
Secrets never leave the server; the API returns only a hint.

Live calls happen **only** when the integration is configured, and only on an
explicit action (Test) or a delivered event. Without configuration a delivery
is recorded as ``skipped`` with the reason — never as success. With
``CAREERCLOUD_INTEGRATIONS_MOCK=1`` (tests, demos) deliveries are recorded as
``mocked`` and nothing leaves the machine.

Google Workspace / Microsoft 365 store the OAuth *client* (client id/secret).
A user-level OAuth grant (access/refresh token) is what calendars need; until
one exists the calendar reports ``needs_oauth`` and creates nothing.

All outbound URLs go through :func:`cloud.intel.core.http.check_url` (public
http(s) only, no redirects followed), so a webhook cannot be pointed at
internal addresses.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import Ctx, NotFoundError, ValidationError, utcnow

__all__ = ["CATALOG", "IntegrationService", "run_integration_task", "sign_body"]

#: provider -> description. ``secrets`` are encrypted; ``settings`` are plain.
CATALOG: Dict[str, Dict[str, Any]] = {
    "slack": {
        "label": "Slack", "kind": "messaging", "category": "Messaging",
        "description": "Post workflow alerts, replies and notifications to a Slack channel.",
        "secrets": ["webhook_url", "bot_token"], "one_of": ["webhook_url", "bot_token"],
        "settings": ["channel", "events"],
        "requirement": "a Slack incoming-webhook URL (https://hooks.slack.com/…) or a bot token (xoxb-…) with "
                       "chat:write plus a channel id",
    },
    "webhook": {
        "label": "Webhooks", "kind": "webhook", "category": "Developer",
        "description": "Send signed JSON events (HMAC-SHA256) to your own HTTPS endpoint.",
        "secrets": ["signing_secret"], "one_of": [], "settings": ["url", "events"],
        "requirement": "a public HTTPS endpoint; a signing secret is generated when you do not supply one",
    },
    "google_workspace": {
        "label": "Google Workspace", "kind": "integration", "category": "Productivity",
        "description": "OAuth client for Gmail sending and Google Calendar.",
        "secrets": ["client_id", "client_secret"], "one_of": [], "required": ["client_id", "client_secret"],
        "settings": ["redirect_uri"],
        "requirement": "a Google Cloud OAuth client (Web application) with the Gmail send and Calendar scopes "
                       "approved on the OAuth consent screen",
    },
    "microsoft365": {
        "label": "Microsoft 365", "kind": "integration", "category": "Productivity",
        "description": "OAuth app for Outlook sending and Outlook Calendar (Microsoft Graph).",
        "secrets": ["client_id", "client_secret"], "one_of": [], "required": ["client_id", "client_secret"],
        "settings": ["tenant_id", "redirect_uri"],
        "requirement": "an Entra ID (Azure AD) app registration with Mail.Send, Calendars.ReadWrite and "
                       "offline_access delegated permissions",
    },
    "google_calendar": {
        "label": "Google Calendar", "kind": "calendar", "category": "Calendar",
        "description": "Create meetings for booked calls and tasks.",
        "secrets": ["access_token", "refresh_token"], "one_of": ["access_token", "refresh_token"],
        "settings": ["calendar_id"], "parent": "google_workspace",
        "requirement": "Google Workspace configured, then a user OAuth grant with the Calendar scope",
    },
    "outlook_calendar": {
        "label": "Outlook Calendar", "kind": "calendar", "category": "Calendar",
        "description": "Create meetings in Outlook via Microsoft Graph.",
        "secrets": ["access_token", "refresh_token"], "one_of": ["access_token", "refresh_token"],
        "settings": [], "parent": "microsoft365",
        "requirement": "Microsoft 365 configured, then a user OAuth grant with Calendars.ReadWrite",
    },
}

#: Events integrations can subscribe to (empty subscription = all).
EVENTS = ("notification", "reply_received", "email_bounced", "unsubscribed", "workflow_failed",
          "workflow_completed", "campaign_status", "import_completed", "scraper_completed", "task_assigned",
          "test")


def sign_body(secret: str, body: str, timestamp: Optional[int] = None) -> Dict[str, str]:
    """Headers for a signed webhook: ``X-SANA-Signature: t=<ts>,v1=<hex hmac of "<ts>.<body>">``."""
    ts = int(timestamp if timestamp is not None else time.time())
    digest = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-SANA-Timestamp": str(ts), "X-SANA-Signature": f"t={ts},v1={digest}"}


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class IntegrationService:
    def __init__(self, platform: Any, *, http: Optional[Callable[..., Any]] = None,
                 resolver: Optional[Callable[..., Any]] = None) -> None:
        self.platform = platform
        self.store = platform.store
        #: ``http(method, url, *, headers, data, timeout) -> response`` (tests inject a fake).
        self.http = http
        #: DNS resolver for the SSRF check (tests inject one).
        self.resolver = resolver

    # --- plumbing ------------------------------------------------------------------

    @property
    def mock_mode(self) -> bool:
        return os.environ.get("CAREERCLOUD_INTEGRATIONS_MOCK", "") in ("1", "true", "yes") or bool(
            (self.platform.config.extra or {}).get("integrations_mock"))

    def _fernet(self):
        return self.platform.service("providers")._fernet()

    @staticmethod
    def describe(provider: str) -> Dict[str, Any]:
        try:
            return CATALOG[provider]
        except KeyError:
            raise NotFoundError(f"unknown integration {provider!r}") from None

    def _row(self, ctx: Ctx, provider: str) -> Optional[Dict[str, Any]]:
        info = self.describe(provider)
        return self.store.first(ctx, "provider_connections", {"provider": provider, "kind": info["kind"]})

    def _secrets(self, ctx: Ctx, provider: str) -> Dict[str, str]:
        row = self._row(ctx, provider)
        if row is None or not row.get("secret_ciphertext"):
            return {}
        return json.loads(self._fernet().decrypt(row["secret_ciphertext"].encode()))

    def _post(self, url: str, *, headers: Mapping[str, str], body: str) -> Any:
        from cloud.intel.core.http import check_url

        url = check_url(url, resolver=self.resolver)
        if self.http is not None:
            return self.http("POST", url, headers=dict(headers), data=body, timeout=10)
        import requests

        return requests.post(url, headers=dict(headers), data=body.encode(), timeout=10, allow_redirects=False)

    # --- status -------------------------------------------------------------------

    def _public(self, provider: str, row: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        info = self.describe(provider)
        settings = dict((row or {}).get("settings") or {})
        connected = bool(row and row.get("secret_ciphertext") and row.get("status") in ("configured", "verified"))
        status = (row or {}).get("status") or "not_configured"
        if not connected and status in ("configured", "verified"):
            status = "not_configured"
        if provider == "webhook" and connected and not settings.get("url"):
            status = "not_configured"
        out = {"provider": provider, "label": info["label"], "kind": info["kind"], "category": info["category"],
               "description": info["description"], "requirement": info["requirement"],
               "secret_fields": info["secrets"], "setting_fields": info["settings"], "status": status,
               "connected": connected and status != "not_configured", "secret_hint": (row or {}).get("secret_hint"),
               "settings": settings, "last_checked_at": (row or {}).get("last_checked_at"),
               "last_error": (row or {}).get("last_error"), "parent": info.get("parent"),
               "live_calls": "only when configured, on Test or a subscribed event"}
        if info.get("parent") and not out["connected"]:
            out["needs"] = f"configure {CATALOG[info['parent']]['label']} and complete a user OAuth grant"
        return out

    def list(self, ctx: Ctx) -> List[Dict[str, Any]]:
        return [self._public(name, self._row(ctx, name)) for name in CATALOG]

    def status(self, ctx: Ctx, provider: str) -> Dict[str, Any]:
        return self._public(provider, self._row(ctx, provider))

    # --- configure / disconnect ---------------------------------------------------------

    def configure(self, ctx: Ctx, provider: str, *, secrets: Optional[Mapping[str, Any]] = None,
                  settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        ctx.require_admin()
        info = self.describe(provider)
        clean = {str(k): str(v).strip() for k, v in (secrets or {}).items() if v not in (None, "")}
        unknown = set(clean) - set(info["secrets"])
        if unknown:
            raise ValidationError(f"{info['label']} does not take: {', '.join(sorted(unknown))}")
        plain = {str(k): v for k, v in (settings or {}).items() if k in info["settings"]}
        if "events" in plain:
            events = plain["events"] if isinstance(plain["events"], list) else str(plain["events"]).split(",")
            events = [str(e).strip() for e in events if str(e).strip()]
            bad = [e for e in events if e not in EVENTS]
            if bad:
                raise ValidationError(f"unknown events: {', '.join(bad)}")
            plain["events"] = events
        row = self._row(ctx, provider)
        existing = self._secrets(ctx, provider) if row is not None else {}
        merged = {**existing, **clean}
        if provider == "webhook":
            url = plain.get("url") or (row or {}).get("settings", {}).get("url")
            if not url:
                raise ValidationError("a webhook needs a URL")
            from cloud.intel.core.http import check_url

            if not str(url).startswith("https://"):
                raise ValidationError("webhook URLs must use https")
            check_url(str(url), resolve=False)
            merged.setdefault("signing_secret", _secrets.token_urlsafe(32))
        if provider == "slack" and merged.get("webhook_url") and not merged["webhook_url"].startswith(
                "https://hooks.slack.com/"):
            raise ValidationError("a Slack webhook URL starts with https://hooks.slack.com/")
        if provider == "slack" and merged.get("bot_token") and not merged.get("webhook_url") and not (
                plain.get("channel") or ((row or {}).get("settings") or {}).get("channel")):
            raise ValidationError("a Slack bot token needs a channel id")
        for name in info.get("required", []):
            if not merged.get(name):
                raise ValidationError(f"{info['label']} needs: {name}")
        if info["one_of"] and not any(merged.get(k) for k in info["one_of"]):
            raise ValidationError(f"{info['label']} needs one of: {', '.join(info['one_of'])}")
        ciphertext = self._fernet().encrypt(json.dumps(merged, sort_keys=True).encode()).decode()
        last = next((merged[k] for k in info["secrets"] if merged.get(k)), "")
        hint = f"…{last[-4:]}" if len(last) >= 12 else "…"
        values = {"kind": info["kind"], "status": "configured", "access_method": "api", "label": info["label"],
                  "secret_ciphertext": ciphertext, "secret_hint": hint,
                  "settings": {**((row or {}).get("settings") or {}), **plain}, "last_error": None, "private": True}
        if row is None:
            row = self.store.insert(ctx, "provider_connections", {"provider": provider, **values})
        else:
            row = self.store.update(ctx, "provider_connections", row["id"], values)
        audit(self.store, ctx, "integration.configure", entity_type="provider_connections", entity_id=row["id"],
              summary=f"{info['label']} configured", changes={"fields": sorted(clean), "settings": plain})
        out = self._public(provider, row)
        if provider == "webhook" and "signing_secret" not in clean and "signing_secret" not in existing:
            out["signing_secret"] = merged["signing_secret"]
            out["note"] = "Save this signing secret now; it is not shown again."
        return out

    def disconnect(self, ctx: Ctx, provider: str) -> Dict[str, Any]:
        ctx.require_admin()
        row = self._row(ctx, provider)
        if row is None:
            raise NotFoundError(f"{provider} is not configured")
        row = self.store.update(ctx, "provider_connections", row["id"], {
            "secret_ciphertext": None, "secret_hint": None, "status": "not_configured", "last_error": None})
        audit(self.store, ctx, "integration.disconnect", entity_type="provider_connections", entity_id=row["id"],
              summary=f"{self.describe(provider)['label']} disconnected")
        return self._public(provider, row)

    # --- delivery -----------------------------------------------------------------

    def _subscribed(self, row: Mapping[str, Any], event: str) -> bool:
        events = (row.get("settings") or {}).get("events") or []
        return event == "test" or not events or event in events

    def _send(self, ctx: Ctx, provider: str, event: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        """Perform the call. Returns ``{status, response_code, error, target}``."""
        creds = self._secrets(ctx, provider)
        row = self._row(ctx, provider) or {}
        settings = row.get("settings") or {}
        text = str(payload.get("text") or payload.get("title") or f"SANA GTM: {event}")[:3000]
        if provider == "slack":
            if creds.get("webhook_url"):
                target = "slack incoming webhook"
                response = self._post(creds["webhook_url"], headers={"Content-Type": "application/json"},
                                      body=json.dumps({"text": text}))
            else:
                target = f"slack channel {settings.get('channel')}"
                response = self._post("https://slack.com/api/chat.postMessage",
                                      headers={"Content-Type": "application/json; charset=utf-8",
                                               "Authorization": f"Bearer {creds.get('bot_token', '')}"},
                                      body=json.dumps({"channel": settings.get("channel"), "text": text}))
                if response.status_code == 200:
                    try:
                        reply = json.loads(response.text or "{}")
                    except ValueError:
                        reply = {}
                    if not reply.get("ok"):
                        return {"status": "failed", "response_code": 200, "target": target,
                                "error": f"Slack refused: {reply.get('error') or 'unknown error'}"}
        elif provider == "webhook":
            url = settings.get("url")
            target = url
            body = json.dumps({"event": event, "workspace_id": ctx.workspace_id, "sent_at": utcnow().isoformat(),
                               "data": payload}, sort_keys=True, default=str)
            headers = {"Content-Type": "application/json", "User-Agent": "SANA-GTM-Webhooks/1.0",
                       "X-SANA-Event": event, **sign_body(creds["signing_secret"], body)}
            response = self._post(url, headers=headers, body=body)
        elif provider in ("google_calendar", "outlook_calendar"):
            return self._calendar_event(ctx, provider, creds, settings, payload)
        else:
            return {"status": "skipped", "response_code": None, "target": None,
                    "error": f"{self.describe(provider)['label']} is an OAuth client, not a delivery target"}
        code = int(getattr(response, "status_code", 0) or 0)
        if 200 <= code < 300:
            return {"status": "delivered", "response_code": code, "target": target, "error": None}
        return {"status": "failed", "response_code": code, "target": target, "error": f"HTTP {code}"}

    def _calendar_event(self, ctx: Ctx, provider: str, creds: Mapping[str, str], settings: Mapping[str, Any],
                        payload: Mapping[str, Any]) -> Dict[str, Any]:
        token = creds.get("access_token")
        if not token:
            return {"status": "skipped", "response_code": None, "target": provider,
                    "error": "no user OAuth access token yet (refresh needs the OAuth grant flow)"}
        start, end = payload.get("start"), payload.get("end")
        if not start or not end:
            return {"status": "failed", "response_code": None, "target": provider,
                    "error": "a calendar event needs start and end (ISO 8601)"}
        summary = str(payload.get("title") or "Meeting")[:300]
        if provider == "google_calendar":
            cal = str(settings.get("calendar_id") or "primary")
            url = f"https://www.googleapis.com/calendar/v3/calendars/{cal}/events"
            body = {"summary": summary, "description": payload.get("description"),
                    "start": {"dateTime": start}, "end": {"dateTime": end},
                    "attendees": [{"email": e} for e in payload.get("attendees") or []]}
        else:
            url = "https://graph.microsoft.com/v1.0/me/events"
            body = {"subject": summary, "body": {"contentType": "text", "content": payload.get("description") or ""},
                    "start": {"dateTime": start, "timeZone": "UTC"}, "end": {"dateTime": end, "timeZone": "UTC"},
                    "attendees": [{"emailAddress": {"address": e}, "type": "required"}
                                  for e in payload.get("attendees") or []]}
        response = self._post(url, headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                              body=json.dumps(body))
        code = int(getattr(response, "status_code", 0) or 0)
        if 200 <= code < 300:
            return {"status": "delivered", "response_code": code, "target": url.split("/v")[0], "error": None}
        return {"status": "failed", "response_code": code, "target": url.split("/v")[0],
                "error": "token expired or refused (reconnect the calendar)" if code == 401 else f"HTTP {code}"}

    def deliver(self, ctx: Ctx, provider: str, event: str, payload: Optional[Mapping[str, Any]] = None
                ) -> Dict[str, Any]:
        """Deliver one event to one integration and record the outcome."""
        info = self.describe(provider)
        payload = dict(payload or {})
        system = ctx if ctx.system else ctx.as_system()
        row = self._row(ctx, provider)
        status = self._public(provider, row)
        record = {"provider": provider, "event": str(event)[:100], "attempts": 0,
                  "payload": {k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                              for k, v in list(payload.items())[:30]}}
        if not status["connected"]:
            record.update(status="skipped", error=f"{info['label']} is not configured")
        elif row is not None and not self._subscribed(row, event):
            record.update(status="skipped", error=f"not subscribed to {event}")
        elif self.mock_mode:
            record.update(status="mocked", attempts=1, error=None, target=f"{provider} (mock mode)")
        else:
            try:
                outcome = self._send(ctx, provider, event, payload)
            except Exception as error:  # noqa: BLE001 - a failed delivery is a result
                outcome = {"status": "failed", "response_code": None, "target": None,
                           "error": f"{type(error).__name__}: {error}"[:500]}
            record.update(attempts=1, **{k: v for k, v in outcome.items() if k in
                                         ("status", "response_code", "error", "target")})
        if record.get("target"):
            record["target"] = str(record["target"])[:500]
        return self.store.insert(system, "integration_deliveries", record)

    def broadcast(self, ctx: Ctx, event: str, payload: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
        """Deliver an event to every configured integration subscribed to it."""
        out = []
        for provider in ("slack", "webhook"):
            row = self._row(ctx, provider)
            if row and self._public(provider, row)["connected"] and self._subscribed(row, event):
                out.append(self.deliver(ctx, provider, event, payload))
        return out

    def test(self, ctx: Ctx, provider: str) -> Dict[str, Any]:
        """An explicit live check. OAuth clients are not callable without a user grant, so they
        report ``configured_unverified`` instead of pretending."""
        ctx.require_admin()
        info = self.describe(provider)
        status = self.status(ctx, provider)
        row = self._row(ctx, provider)
        if not status["connected"]:
            result = {"status": "not_configured", "detail": f"{info['label']} is not configured"}
        elif info["kind"] == "integration":
            result = {"status": "configured_unverified",
                      "detail": "OAuth client stored. It is verified when a user completes the OAuth grant "
                                "(connect a mailbox or calendar)."}
        else:
            payload = {"text": "SANA GTM test message: this integration is connected.", "title": "SANA GTM test"}
            if info["kind"] == "calendar":
                result = {"status": "configured_unverified",
                          "detail": "calendar grant stored; an event is created only for a real meeting"}
            else:
                delivery = self.deliver(ctx, provider, "test", payload)
                ok = delivery["status"] in ("delivered", "mocked")
                result = {"status": "ok" if ok else "error", "detail": delivery.get("error") or delivery["status"],
                          "delivery_id": delivery["id"], "mocked": delivery["status"] == "mocked"}
        if row is not None:
            new_status = {"ok": "verified", "error": "error"}.get(result["status"], row["status"])
            self.store.update(ctx, "provider_connections", row["id"], {
                "status": new_status if row.get("secret_ciphertext") else "not_configured",
                "last_checked_at": utcnow(),
                "last_error": result["detail"][:2000] if result["status"] == "error" else None})
        audit(self.store, ctx, "integration.test", entity_type="provider_connections",
              entity_id=row["id"] if row else None, summary=f"{provider}: {result['status']}")
        return {"provider": provider, **result}

    def retry(self, ctx: Ctx, delivery_id: str) -> Dict[str, Any]:
        ctx.require_write()
        old = self.store.get(ctx, "integration_deliveries", delivery_id)
        if old["status"] not in ("failed", "skipped"):
            raise ValidationError(f"a {old['status']} delivery is not retried")
        return self.deliver(ctx, old["provider"], old["event"], old.get("payload") or {})

    def deliveries(self, ctx: Ctx, *, provider: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        filters = {"provider": provider} if provider else {}
        return self.store.list(ctx, "integration_deliveries", filters, order="-created_at",
                               limit=max(1, min(limit, 200))).rows


def run_integration_task(platform: Any, ctx: Ctx, task: Mapping[str, Any], reporter: Any) -> Dict[str, Any]:
    """``{"provider", "event", "payload"}`` delivers to one integration; ``{"event", "payload"}``
    broadcasts to every subscribed one; ``{"delivery_id"}`` retries a failed delivery."""
    service: IntegrationService = platform.service("integrations")
    params = task.get("params") or {}
    if params.get("delivery_id"):
        rows = [service.retry(ctx, params["delivery_id"])]
    elif params.get("provider"):
        rows = [service.deliver(ctx, params["provider"], str(params.get("event") or "notification"),
                                params.get("payload") or {})]
    else:
        rows = service.broadcast(ctx, str(params.get("event") or "notification"), params.get("payload") or {})
    reporter.progress(f"{len(rows)} deliver(ies)", done=len(rows), total=len(rows))
    return {"deliveries": [{"id": r["id"], "provider": r["provider"], "status": r["status"]} for r in rows]}
