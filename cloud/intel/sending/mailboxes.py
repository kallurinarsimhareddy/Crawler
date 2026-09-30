"""Connected sending mailboxes: Google Workspace/Gmail, Microsoft 365, SMTP relay, API relay.

**No mailbox passwords, ever.** A mailbox is connected by:

* ``google`` — OAuth 2.0 authorization code + PKCE against Google, scope
  ``gmail.send`` (plus ``openid email`` to learn the address). Needs the server's
  ``CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID`` / ``_SECRET``. Stores the refresh token.
* ``microsoft365`` — OAuth 2.0 code + PKCE against Microsoft identity platform,
  scopes ``Mail.Send offline_access User.Read``. Needs
  ``CAREERCLOUD_MS_OAUTH_CLIENT_ID`` / ``_SECRET`` (``_TENANT`` defaults to
  ``common``). Stores the refresh token.
* ``api`` — a transactional-email API key (SendGrid or Postmark) for a verified
  sender address.
* ``smtp`` — the server's own SMTP relay from ``CAREERCLOUD_SMTP_*`` environment
  settings; nothing secret is stored per mailbox.

Every secret is Fernet-encrypted with ``CAREERCLOUD_PLATFORM_SECRETS_KEY`` (same
key as provider credentials) and never leaves the server: :meth:`MailboxService.public`
strips it. Without OAuth client credentials the Google/Microsoft connectors report
``configured: False`` and refuse to start — they never pretend to connect.

Sending through a mailbox (:class:`MailboxSender`) is only constructed by the
sequence engine after every gate has passed (see :mod:`cloud.intel.gtm.sequences`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlencode

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ValidationError, utcnow
from cloud.intel.core.normalize import normalize_email
from cloud.intel.gtm.sequences import OutboundMessage, SenderProvider

__all__ = ["MailboxService", "MailboxSender", "PROVIDERS", "provider_config"]

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
MS_AUTH = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MS_SEND = "https://graph.microsoft.com/v1.0/me/sendMail"
MS_ME = "https://graph.microsoft.com/v1.0/me"

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "google": {"label": "Google Workspace / Gmail", "auth": "oauth",
               "scopes": ["openid", "email", "https://www.googleapis.com/auth/gmail.send"],
               "env": ["CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID", "CAREERCLOUD_GOOGLE_OAUTH_CLIENT_SECRET"]},
    "microsoft365": {"label": "Microsoft 365 / Outlook", "auth": "oauth",
                     "scopes": ["offline_access", "User.Read", "Mail.Send"],
                     "env": ["CAREERCLOUD_MS_OAUTH_CLIENT_ID", "CAREERCLOUD_MS_OAUTH_CLIENT_SECRET"]},
    "smtp": {"label": "SMTP relay (server)", "auth": "server_env", "scopes": [],
             "env": ["CAREERCLOUD_SMTP_HOST", "CAREERCLOUD_SMTP_FROM"]},
    "api": {"label": "Email API (SendGrid / Postmark)", "auth": "api_key", "scopes": [], "env": [],
            "vendors": ["sendgrid", "postmark"]},
}
OAUTH_STATE_MINUTES = 15


def provider_config(provider: str, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Whether the server can offer this provider, and what is missing if not."""
    env = env if env is not None else os.environ
    info = PROVIDERS[provider]
    missing = [name for name in info["env"] if not env.get(name)]
    return {"provider": provider, "label": info["label"], "auth": info["auth"], "scopes": list(info["scopes"]),
            "configured": not missing, "missing": missing, "vendors": info.get("vendors", [])}


def _redirect_uri(provider: str, env: Mapping[str, str]) -> str:
    base = (env.get("CAREERCLOUD_OAUTH_REDIRECT_BASE") or env.get("CAREERCLOUD_PUBLIC_API_URL") or "").rstrip("/")
    if not base:
        raise ValidationError("OAuth needs CAREERCLOUD_OAUTH_REDIRECT_BASE (the public API URL) on the server")
    return f"{base}/api/v1/oauth/{provider}/callback"


def _pkce() -> tuple:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _mime(from_address: str, display_name: Optional[str], message: Mapping[str, Any]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{display_name} <{from_address}>" if display_name else from_address
    msg["To"] = message["to"]
    msg["Subject"] = message["subject"]
    for key, value in (message.get("headers") or {}).items():
        msg[key] = value
    msg.set_content(message["body"])
    return msg


class _Http:
    """A tiny injectable HTTP layer (tests pass a fake with the same two methods)."""

    def __init__(self, timeout: float = 30.0) -> None:
        import requests

        self._session = requests.Session()
        self.timeout = timeout

    def post(self, url: str, **kw: Any):
        return self._session.post(url, timeout=self.timeout, **kw)

    def get(self, url: str, **kw: Any):
        return self._session.get(url, timeout=self.timeout, **kw)


class MailboxSender(SenderProvider):
    """Delivers through one connected mailbox. Built only when every sending gate passed."""

    delivers = True

    def __init__(self, mailbox: Mapping[str, Any], secret: Mapping[str, Any], *, http: Any = None,
                 env: Optional[Mapping[str, str]] = None) -> None:
        self.mailbox = dict(mailbox)
        self.secret = dict(secret)
        self.name = f"mailbox:{mailbox['provider']}"
        self._http = http
        self._env = env if env is not None else os.environ

    @property
    def http(self):
        if self._http is None:
            self._http = _Http()
        return self._http

    def _access_token(self) -> str:
        provider = self.mailbox["provider"]
        if provider == "google":
            data = {"client_id": self._env.get("CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID", ""),
                    "client_secret": self._env.get("CAREERCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", ""),
                    "refresh_token": self.secret.get("refresh_token", ""), "grant_type": "refresh_token"}
            url = GOOGLE_TOKEN
        else:
            tenant = self._env.get("CAREERCLOUD_MS_OAUTH_TENANT") or "common"
            data = {"client_id": self._env.get("CAREERCLOUD_MS_OAUTH_CLIENT_ID", ""),
                    "client_secret": self._env.get("CAREERCLOUD_MS_OAUTH_CLIENT_SECRET", ""),
                    "refresh_token": self.secret.get("refresh_token", ""), "grant_type": "refresh_token",
                    "scope": " ".join(PROVIDERS["microsoft365"]["scopes"])}
            url = MS_TOKEN.format(tenant=tenant)
        response = self.http.post(url, data=data)
        body = _json(response)
        if response.status_code != 200 or not body.get("access_token"):
            raise ValidationError(f"token refresh failed (HTTP {response.status_code}: "
                                  f"{str(body.get('error') or '')[:80]})")
        return body["access_token"]

    def check(self) -> Dict[str, Any]:
        """A connection test that sends nothing: refresh the OAuth token / confirm config."""
        provider = self.mailbox["provider"]
        if provider in ("google", "microsoft365"):
            self._access_token()
            return {"ok": True, "detail": "OAuth token refreshed (no email sent)"}
        if provider == "smtp":
            cfg = provider_config("smtp", self._env)
            return {"ok": cfg["configured"], "detail": "server SMTP relay configured" if cfg["configured"]
                    else f"missing {', '.join(cfg['missing'])}"}
        if not self.secret.get("api_key"):
            return {"ok": False, "detail": "no API key stored"}
        return {"ok": True, "detail": "API key stored (not verified with the vendor; verification sends nothing)"}

    def send(self, message: OutboundMessage) -> Dict[str, Any]:
        provider = self.mailbox["provider"]
        address, display = self.mailbox["address"], self.mailbox.get("display_name")
        try:
            if provider == "google":
                raw = base64.urlsafe_b64encode(_mime(address, display, message).as_bytes()).decode()
                response = self.http.post(GOOGLE_SEND, json={"raw": raw},
                                          headers={"Authorization": f"Bearer {self._access_token()}"})
                body = _json(response)
                if response.status_code in (200, 202):
                    return {"event": "sent", "provider_message_id": body.get("id"), "detail": None}
            elif provider == "microsoft365":
                payload = {"message": {"subject": message["subject"],
                                       "body": {"contentType": "Text", "content": message["body"]},
                                       "toRecipients": [{"emailAddress": {"address": message["to"]}}],
                                       "internetMessageHeaders": [
                                           {"name": f"x-{k}" if not k.lower().startswith("x-") else k, "value": v}
                                           for k, v in (message.get("headers") or {}).items()]},
                           "saveToSentItems": True}
                response = self.http.post(MS_SEND, json=payload,
                                          headers={"Authorization": f"Bearer {self._access_token()}"})
                if response.status_code == 202:
                    return {"event": "sent", "provider_message_id": response.headers.get("request-id"),
                            "detail": None}
                body = _json(response)
            elif provider == "api":
                return self._send_api(message)
            else:
                from cloud.intel.gtm.sequences import SmtpSender

                return SmtpSender.from_env(self._env).send(message)
            return {"event": "failed", "provider_message_id": None,
                    "detail": f"HTTP {response.status_code}: {json.dumps(body)[:300]}"}
        except ValidationError as error:
            return {"event": "failed", "provider_message_id": None, "detail": str(error)[:500]}

    def _send_api(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        vendor = (self.mailbox.get("settings") or {}).get("vendor", "sendgrid")
        key = self.secret.get("api_key", "")
        if vendor == "postmark":
            response = self.http.post("https://api.postmarkapp.com/email", headers={
                "X-Postmark-Server-Token": key, "Accept": "application/json"}, json={
                "From": self.mailbox["address"], "To": message["to"], "Subject": message["subject"],
                "TextBody": message["body"], "MessageStream": "outbound",
                "Headers": [{"Name": k, "Value": v} for k, v in (message.get("headers") or {}).items()]})
            body = _json(response)
            if response.status_code == 200 and body.get("ErrorCode", 0) == 0:
                return {"event": "sent", "provider_message_id": body.get("MessageID"), "detail": None}
        else:
            response = self.http.post("https://api.sendgrid.com/v3/mail/send", headers={
                "Authorization": f"Bearer {key}"}, json={
                "personalizations": [{"to": [{"email": message["to"]}]}],
                "from": {"email": self.mailbox["address"], "name": self.mailbox.get("display_name") or None},
                "subject": message["subject"], "content": [{"type": "text/plain", "value": message["body"]}],
                "headers": dict(message.get("headers") or {})})
            body = _json(response)
            if response.status_code == 202:
                return {"event": "sent", "provider_message_id": response.headers.get("X-Message-Id"),
                        "detail": None}
        return {"event": "failed", "provider_message_id": None,
                "detail": f"HTTP {response.status_code}: {json.dumps(body)[:300]}"}


def _json(response: Any) -> Dict[str, Any]:
    try:
        body = response.json()
        return body if isinstance(body, dict) else {"body": body}
    except Exception:  # noqa: BLE001
        return {}


class MailboxService:
    def __init__(self, platform: Any, *, env: Optional[Mapping[str, str]] = None) -> None:
        self.platform = platform
        self.store = platform.store
        self._env = env
        #: Tests inject a fake HTTP layer (post/get returning objects with status_code/json()).
        self.http: Any = None

    @property
    def env(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def _fernet(self):
        return self.platform.service("providers")._fernet()

    def _encrypt(self, data: Mapping[str, Any]) -> str:
        return self._fernet().encrypt(json.dumps(dict(data), sort_keys=True).encode()).decode()

    def _decrypt(self, text: Optional[str]) -> Dict[str, Any]:
        if not text:
            return {}
        return json.loads(self._fernet().decrypt(text.encode()))

    # --- views ------------------------------------------------------------------------

    @staticmethod
    def public(row: Mapping[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in row.items() if k != "secret_ciphertext"}
        out["has_secret"] = bool(row.get("secret_ciphertext"))
        return out

    def providers(self) -> List[Dict[str, Any]]:
        from cloud.intel.sending.events import CAPABILITIES

        return [{**provider_config(name, self.env), "events": CAPABILITIES.get(name, {})} for name in PROVIDERS]

    def list(self, ctx: Ctx) -> List[Dict[str, Any]]:
        return [self.public(r) for r in self.store.all(ctx, "mailboxes", order="address")]

    # --- connecting -------------------------------------------------------------------

    def start_oauth(self, ctx: Ctx, provider: str, *, redirect_to: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_admin()
        if provider not in ("google", "microsoft365"):
            raise ValidationError("OAuth is available for google and microsoft365")
        cfg = provider_config(provider, self.env)
        if not cfg["configured"]:
            raise ValidationError(f"{cfg['label']} is not configured on the server (missing "
                                  f"{', '.join(cfg['missing'])})")
        redirect_uri = _redirect_uri(provider, self.env)
        verifier, challenge = _pkce()
        state = f"{ctx.workspace_id}.{secrets.token_urlsafe(24)}"
        self.store.insert(ctx, "oauth_states", {
            "provider": provider, "purpose": "mailbox", "state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "code_verifier_ciphertext": self._encrypt({"verifier": verifier}),
            "redirect_to": (redirect_to or "/settings/sending")[:500],
            "expires_at": utcnow() + timedelta(minutes=OAUTH_STATE_MINUTES)})
        if provider == "google":
            url = GOOGLE_AUTH + "?" + urlencode({
                "client_id": self.env["CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID"], "redirect_uri": redirect_uri,
                "response_type": "code", "scope": " ".join(PROVIDERS["google"]["scopes"]), "state": state,
                "access_type": "offline", "prompt": "consent", "code_challenge": challenge,
                "code_challenge_method": "S256"})
        else:
            tenant = self.env.get("CAREERCLOUD_MS_OAUTH_TENANT") or "common"
            url = MS_AUTH.format(tenant=tenant) + "?" + urlencode({
                "client_id": self.env["CAREERCLOUD_MS_OAUTH_CLIENT_ID"], "redirect_uri": redirect_uri,
                "response_type": "code", "scope": " ".join(PROVIDERS["microsoft365"]["scopes"]),
                "state": state, "response_mode": "query", "code_challenge": challenge,
                "code_challenge_method": "S256"})
        audit(self.store, ctx, "mailbox.oauth_start", summary=f"{cfg['label']} authorization started")
        return {"authorize_url": url, "expires_in": OAUTH_STATE_MINUTES * 60}

    def complete_oauth(self, provider: str, state: str, code: str) -> Dict[str, Any]:
        """The public callback: the state proves which workspace asked; single use, 15 minutes."""
        workspace_id, _, _rest = (state or "").partition(".")
        try:
            ctx = Ctx.for_system(workspace_id)
        except (ValueError, TypeError):
            raise ValidationError("invalid OAuth state") from None
        row = self.store.first(ctx, "oauth_states", {"state_hash": hashlib.sha256(state.encode()).hexdigest()})
        if row is None or row["provider"] != provider:
            raise ValidationError("invalid OAuth state")
        if row.get("used_at") or row["expires_at"] < utcnow():
            raise ValidationError("this authorization link has expired; start again")
        self.store.update(ctx, "oauth_states", row["id"], {"used_at": utcnow()})
        verifier = self._decrypt(row["code_verifier_ciphertext"]).get("verifier", "")
        http = self.http or _Http()
        redirect_uri = _redirect_uri(provider, self.env)
        if provider == "google":
            response = http.post(GOOGLE_TOKEN, data={
                "client_id": self.env.get("CAREERCLOUD_GOOGLE_OAUTH_CLIENT_ID", ""),
                "client_secret": self.env.get("CAREERCLOUD_GOOGLE_OAUTH_CLIENT_SECRET", ""),
                "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
                "redirect_uri": redirect_uri})
        else:
            tenant = self.env.get("CAREERCLOUD_MS_OAUTH_TENANT") or "common"
            response = http.post(MS_TOKEN.format(tenant=tenant), data={
                "client_id": self.env.get("CAREERCLOUD_MS_OAUTH_CLIENT_ID", ""),
                "client_secret": self.env.get("CAREERCLOUD_MS_OAUTH_CLIENT_SECRET", ""),
                "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
                "redirect_uri": redirect_uri, "scope": " ".join(PROVIDERS["microsoft365"]["scopes"])})
        tokens = _json(response)
        if response.status_code != 200 or not tokens.get("refresh_token"):
            raise ValidationError(f"the provider did not grant offline access (HTTP {response.status_code})")
        address = self._address_from_tokens(provider, tokens, http)
        if not address:
            raise ValidationError("could not determine the mailbox address from the provider")
        granted = [s for s in str(tokens.get("scope") or "").split() if s]
        mailbox = self._upsert(ctx, provider, address, {
            "status": "connected", "scopes": granted or list(PROVIDERS[provider]["scopes"]),
            "secret_ciphertext": self._encrypt({"refresh_token": tokens["refresh_token"]}),
            "secret_hint": f"…{tokens['refresh_token'][-4:]}", "health": "unknown", "last_error": None})
        audit(self.store, ctx, "mailbox.connect", entity_type="mailboxes", entity_id=mailbox["id"],
              summary=f"{address} connected via {provider} OAuth")
        return {"mailbox": self.public(mailbox), "redirect_to": row.get("redirect_to") or "/settings/sending"}

    @staticmethod
    def _address_from_tokens(provider: str, tokens: Mapping[str, Any], http: Any) -> Optional[str]:
        if provider == "google" and tokens.get("id_token"):
            try:
                payload = tokens["id_token"].split(".")[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                return normalize_email(claims.get("email"))
            except (ValueError, IndexError):
                return None
        if provider == "microsoft365" and tokens.get("access_token"):
            response = http.get(MS_ME, headers={"Authorization": f"Bearer {tokens['access_token']}"})
            me = _json(response)
            return normalize_email(me.get("mail") or me.get("userPrincipalName"))
        return None

    def _upsert(self, ctx: Ctx, provider: str, address: str, values: Mapping[str, Any]) -> Dict[str, Any]:
        existing = self.store.first(ctx, "mailboxes", {"provider": provider, "address": address})
        if existing is None:
            is_default = self.store.count(ctx, "mailboxes", {"is_default": True}) == 0
            try:
                return self.store.insert(ctx, "mailboxes", {"provider": provider, "address": address,
                                                            "is_default": is_default, **values})
            except ConflictError:
                existing = self.store.first(ctx, "mailboxes", {"provider": provider, "address": address})
        return self.store.update(ctx, "mailboxes", existing["id"], dict(values))

    def connect_api(self, ctx: Ctx, *, address: str, api_key: str, vendor: str = "sendgrid",
                    display_name: Optional[str] = None, daily_limit: Optional[int] = None) -> Dict[str, Any]:
        ctx.require_admin()
        email = normalize_email(address)
        if not email:
            raise ValidationError("a valid sender address is required")
        if vendor not in PROVIDERS["api"]["vendors"]:
            raise ValidationError(f"vendor must be one of {', '.join(PROVIDERS['api']['vendors'])}")
        if not api_key or len(api_key.strip()) < 8:
            raise ValidationError("an API key is required (mailbox passwords are never accepted)")
        values: Dict[str, Any] = {"status": "connected", "display_name": display_name, "settings": {"vendor": vendor},
                                  "secret_ciphertext": self._encrypt({"api_key": api_key.strip()}),
                                  "secret_hint": f"…{api_key.strip()[-4:]}", "health": "unknown", "last_error": None}
        if daily_limit is not None:
            values["daily_limit"] = int(daily_limit)
        row = self._upsert(ctx, "api", email, values)
        audit(self.store, ctx, "mailbox.connect", entity_type="mailboxes", entity_id=row["id"],
              summary=f"{email} connected via {vendor} API", changes={"vendor": vendor})
        return self.public(row)

    def connect_smtp(self, ctx: Ctx, *, address: str, display_name: Optional[str] = None) -> Dict[str, Any]:
        """Register a sender that uses the server's SMTP relay. No per-mailbox secret is stored."""
        ctx.require_admin()
        email = normalize_email(address)
        if not email:
            raise ValidationError("a valid sender address is required")
        cfg = provider_config("smtp", self.env)
        values = {"display_name": display_name, "status": "connected" if cfg["configured"] else "pending",
                  "last_error": None if cfg["configured"] else
                  f"server SMTP relay not configured (missing {', '.join(cfg['missing'])})",
                  "health": "unknown"}
        row = self._upsert(ctx, "smtp", email, values)
        audit(self.store, ctx, "mailbox.connect", entity_type="mailboxes", entity_id=row["id"],
              summary=f"{email} registered on the server SMTP relay")
        return self.public(row)

    def disconnect(self, ctx: Ctx, mailbox_id: str) -> Dict[str, Any]:
        ctx.require_admin()
        self.store.get(ctx, "mailboxes", mailbox_id)
        row = self.store.update(ctx, "mailboxes", mailbox_id, {
            "status": "disconnected", "secret_ciphertext": None, "secret_hint": None, "is_default": False,
            "health": "unknown"})
        audit(self.store, ctx, "mailbox.disconnect", entity_type="mailboxes", entity_id=mailbox_id,
              summary=f"{row['address']} disconnected")
        return self.public(row)

    def update(self, ctx: Ctx, mailbox_id: str, changes: Mapping[str, Any]) -> Dict[str, Any]:
        ctx.require_admin()
        allowed = {"display_name", "daily_limit", "hourly_limit"}
        clean = {k: v for k, v in changes.items() if k in allowed}
        if "status" in changes:
            current = self.store.get(ctx, "mailboxes", mailbox_id)
            if changes["status"] == "paused" and current["status"] == "connected":
                clean["status"] = "paused"
            elif changes["status"] == "connected" and current["status"] == "paused":
                clean["status"] = "connected"
            else:
                raise ValidationError("status can only move between connected and paused here")
        bad = set(changes) - allowed - {"status"}
        if bad:
            raise ValidationError(f"cannot change {', '.join(sorted(bad))}")
        row = self.store.update(ctx, "mailboxes", mailbox_id, clean)
        audit(self.store, ctx, "mailbox.update", entity_type="mailboxes", entity_id=mailbox_id, changes=clean)
        return self.public(row)

    def delete(self, ctx: Ctx, mailbox_id: str) -> None:
        ctx.require_admin()
        row = self.store.get(ctx, "mailboxes", mailbox_id)
        self.store.delete(ctx, "mailboxes", mailbox_id)
        audit(self.store, ctx, "mailbox.delete", entity_type="mailboxes", entity_id=mailbox_id,
              summary=row["address"])

    def set_default(self, ctx: Ctx, mailbox_id: str) -> Dict[str, Any]:
        ctx.require_admin()
        row = self.store.get(ctx, "mailboxes", mailbox_id)
        if row["status"] != "connected":
            raise ValidationError("only a connected mailbox can be the default sender")
        for other in self.store.all(ctx, "mailboxes", {"is_default": True}):
            if other["id"] != mailbox_id:
                self.store.update(ctx, "mailboxes", other["id"], {"is_default": False})
        row = self.store.update(ctx, "mailboxes", mailbox_id, {"is_default": True})
        audit(self.store, ctx, "mailbox.default", entity_type="mailboxes", entity_id=mailbox_id,
              summary=f"{row['address']} is the default sender")
        return self.public(row)

    # --- testing -------------------------------------------------------------------------

    def sender(self, ctx: Ctx, mailbox: Mapping[str, Any]) -> MailboxSender:
        return MailboxSender(mailbox, self._decrypt(mailbox.get("secret_ciphertext")), http=self.http, env=self.env)

    def test(self, ctx: Ctx, mailbox_id: str, *, to: Optional[str] = None) -> Dict[str, Any]:
        """Connection test (sends nothing), or — with ``to`` — a test send, refused unless
        sending is allowed in this environment and the mailbox is connected."""
        ctx.require_admin()
        row = self.store.get(ctx, "mailboxes", mailbox_id)
        now = utcnow()
        if row["status"] != "connected":
            result = {"ok": False, "sent": False, "detail": f"mailbox is {row['status']}, not connected"}
        elif to is not None:
            recipient = normalize_email(to)
            if not recipient:
                raise ValidationError("a valid test recipient is required")
            if not self.platform.config.allow_email_sending:
                result = {"ok": False, "sent": False,
                          "detail": "test send refused: email sending is disabled in this environment "
                                    "(needs production + CAREERCLOUD_ALLOW_EMAIL_SENDING)"}
            else:
                blocked = self.platform.service("suppression").check(ctx, recipient)
                if blocked:
                    result = {"ok": False, "sent": False, "detail": f"recipient is suppressed ({blocked['reason']})"}
                else:
                    outcome = self.sender(ctx, row).send(OutboundMessage(
                        to=recipient, subject="SANA GTM test message",
                        body=f"This is a test message from {row['address']} via SANA GTM.", headers={}))
                    self.store.insert(ctx, "outbound_messages", {
                        "mailbox_id": mailbox_id, "to_email": recipient, "subject": "SANA GTM test message",
                        "body": "(test message)", "status": "sent" if outcome["event"] == "sent" else "failed",
                        "sent_at": now if outcome["event"] == "sent" else None, "provider": row["provider"],
                        "provider_message_id": outcome.get("provider_message_id"), "attempts": 1,
                        "error": outcome.get("detail"), "test": True})
                    result = {"ok": outcome["event"] == "sent", "sent": outcome["event"] == "sent",
                              "detail": outcome.get("detail") or "test message sent"}
        else:
            try:
                check = self.sender(ctx, row).check()
                result = {"ok": bool(check["ok"]), "sent": False, "detail": check["detail"]}
            except Exception as error:  # noqa: BLE001 - reported, never raised to the UI as a 500
                result = {"ok": False, "sent": False, "detail": str(error)[:300]}
        health = "healthy" if result["ok"] else ("failing" if row["status"] == "connected" else "unknown")
        updated = self.store.update(ctx, "mailboxes", mailbox_id, {
            "last_test_at": now, "last_test_result": {**result, "at": now.isoformat()}, "health": health,
            "last_error": None if result["ok"] else result["detail"][:2000]})
        audit(self.store, ctx, "mailbox.test", entity_type="mailboxes", entity_id=mailbox_id,
              changes={"ok": result["ok"], "sent": result["sent"], "test_send": to is not None})
        return {**result, "mailbox": self.public(updated)}

    # --- choosing a sender -------------------------------------------------------------------

    def _sent_last_hour(self, ctx: Ctx, mailbox_id: str, now: datetime) -> int:
        return self.store.count(ctx, "outbound_messages", {"mailbox_id": mailbox_id, "status": "sent",
                                                           "sent_at__gte": now - timedelta(hours=1)})

    def capacity(self, ctx: Ctx, mailbox: Mapping[str, Any], now: Optional[datetime] = None) -> int:
        now = now or utcnow()
        today = now.date()
        sent_today = mailbox["sent_today"] if mailbox.get("sent_day") == today else 0
        daily_left = max(0, int(mailbox["daily_limit"]) - int(sent_today))
        hourly_left = max(0, int(mailbox["hourly_limit"]) - self._sent_last_hour(ctx, mailbox["id"], now))
        return min(daily_left, hourly_left)

    def pick(self, ctx: Ctx, campaign: Optional[Mapping[str, Any]] = None, *, mailbox_id: Optional[str] = None,
             now: Optional[datetime] = None) -> tuple:
        """``(mailbox, reason)``: a connected mailbox with capacity (campaign senders in
        order of most remaining capacity, else the default), or ``(None, why not)``."""
        ids: List[str] = []
        if mailbox_id:
            ids = [mailbox_id]
        elif campaign and campaign.get("mailbox_ids"):
            ids = list(campaign["mailbox_ids"])
        candidates = [self.store.find(ctx, "mailboxes", i) for i in ids] if ids else \
            self.store.all(ctx, "mailboxes", {"is_default": True})
        candidates = [m for m in candidates if m and m["status"] == "connected"]
        if not candidates:
            return None, "no connected sending mailbox" + (" for this campaign" if ids else " (set a default)")
        ranked = sorted(((self.capacity(ctx, m, now), m) for m in candidates), key=lambda t: -t[0])
        capacity, best = ranked[0]
        if capacity <= 0:
            return None, "every sending mailbox is at its daily or hourly limit"
        return best, None

    def record_send(self, ctx: Ctx, mailbox: Mapping[str, Any], now: Optional[datetime] = None) -> None:
        today: date = (now or utcnow()).date()
        current = self.store.get(ctx, "mailboxes", mailbox["id"])
        sent = current["sent_today"] + 1 if current.get("sent_day") == today else 1
        self.store.update(ctx, "mailboxes", mailbox["id"], {"sent_today": sent, "sent_day": today})
