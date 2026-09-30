"""Phase 12-14: members/roles/invitations/teams/assignment, the audit log (redaction,
search, CSV export, session events), notifications and integrations — through the
services and the real FastAPI app, including workspace isolation."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.intel.admin.service import ROLE_LABELS, hash_token, permissions_for
from cloud.intel.core.audit import REDACTED, audit, redact
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, ValidationError, utcnow
from cloud.intel.integrations.service import IntegrationService, sign_body
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.shared.storage import LocalFileStorage
from cloud.worker.dispatcher import NullDispatcher

SECRET = "platform-admin-tests-secret-0123456789abcdef"


def _public(host, port, type=None, **_):  # getaddrinfo stub: every host resolves to a public address
    import socket

    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class FakeHTTP:
    def __init__(self, status=200, text="ok"):
        self.calls = []
        self.status, self.text = status, text

    def __call__(self, method, url, *, headers, data, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})

        class R:
            status_code = self.status
            text = self.text
        return R()


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.owner = str(uuid.uuid4())
        ws = self.store.create_workspace(self.owner, "Admin", f"admin-{uuid.uuid4().hex[:8]}")
        self.ws = ws["id"]
        self.ctx = Ctx(self.ws, self.owner, "owner", actor_label="owner@example.com")
        self.platform = Platform(self.store, config=PlatformConfig(secrets_key=Fernet.generate_key().decode()))
        self.admin = self.platform.service("admin")

    def _member(self, role):
        uid = str(uuid.uuid4())
        self.store.add_member(self.ctx, uid, role)
        return uid, Ctx(self.ws, uid, role)

    # --- roles -----------------------------------------------------------------
    def test_permission_matrix(self) -> None:
        self.assertEqual(ROLE_LABELS["member"], "User")
        self.assertEqual(ROLE_LABELS["viewer"], "Read-only")
        viewer = permissions_for("viewer")
        self.assertTrue(viewer["records.read"])
        self.assertFalse(viewer["records.write"])
        self.assertTrue(permissions_for("manager")["assign.owner"])
        self.assertFalse(permissions_for("manager")["members.manage"])
        self.assertTrue(permissions_for("admin")["members.manage"])
        self.assertFalse(permissions_for("member")["teams.manage"])

    def test_manager_role_writes_but_cannot_admin(self) -> None:
        _, manager = self._member("manager")
        self.assertTrue(manager.can_write and manager.can_manage)
        self.assertFalse(manager.can_admin)
        self.store.insert(manager, "companies", {"name": "Managed Co"})
        with self.assertRaises(ForbiddenError):
            self.admin.invite(manager, "x@example.com")

    def test_change_role_and_owner_and_last_admin_protection(self) -> None:
        uid, _ = self._member("member")
        self.assertEqual(self.admin.change_role(self.ctx, uid, "manager")["role"], "manager")
        roles = {m["user_id"]: m["role"] for m in self.admin.members(self.ctx)}
        self.assertEqual(roles[uid], "manager")
        with self.assertRaises(ForbiddenError):
            self.admin.change_role(self.ctx, self.owner, "viewer")
        with self.assertRaises(ForbiddenError):
            self.admin.remove_member(self.ctx, self.owner)
        with self.assertRaises(ValidationError):
            self.admin.change_role(self.ctx, uid, "owner")
        # A lone admin (with the owner absent from the count only if not admin) — owner counts as admin.
        admin_uid, admin_ctx = self._member("admin")
        self.admin.change_role(admin_ctx, admin_uid, "member")  # owner still an admin: allowed
        _, member_ctx = self._member("member")
        with self.assertRaises(ForbiddenError):
            self.admin.change_role(member_ctx, uid, "viewer")

    def test_last_admin_cannot_be_demoted(self) -> None:
        # Workspace whose owner row is replaced: simulate one admin + members only.
        uid, _ = self._member("admin")
        self.store._members[(self.ws, self.owner)] = "member"  # owner demoted out of band for the test
        with self.assertRaises(ValidationError):
            self.admin.change_role(Ctx.for_system(self.ws), uid, "member")
        with self.assertRaises(ValidationError):
            self.admin.remove_member(Ctx.for_system(self.ws), uid)

    def test_remove_member_drops_team_membership(self) -> None:
        uid, _ = self._member("member")
        team = self.admin.save_team(self.ctx, {"name": "SDRs"})
        self.admin.add_team_member(self.ctx, team["id"], uid)
        self.admin.remove_member(self.ctx, uid)
        self.assertIsNone(self.store.membership(uid, self.ws))
        self.assertEqual(self.admin.teams(self.ctx)[0]["members"], [])
        self.assertTrue(self.store.all(self.ctx, "audit_log", {"action": "user.removed"}))

    # --- invitations -----------------------------------------------------------
    def test_invitation_lifecycle(self) -> None:
        created = self.admin.invite(self.ctx, "New.Person@Example.com", "manager")
        token = created["token"]
        row = self.store.get(self.ctx, "workspace_invitations", created["id"])
        self.assertEqual(row["token_hash"], hash_token(token))
        self.assertNotIn("token_hash", created)
        self.assertNotIn(token, json.dumps([r["changes"] for r in self.store.all(self.ctx, "audit_log")]))
        self.assertTrue(all("token" not in i for i in self.admin.invitations(self.ctx)))
        newbie = str(uuid.uuid4())
        with self.assertRaises(ForbiddenError):
            self.admin.accept_invitation(token, user_id=newbie, email="someone.else@example.com")
        result = self.admin.accept_invitation(token, user_id=newbie, email="new.person@example.com")
        self.assertEqual(result["role"], "manager")
        self.assertEqual(self.store.membership(newbie, self.ws)["role"], "manager")
        with self.assertRaises(ValidationError):
            self.admin.accept_invitation(token, user_id=newbie, email="new.person@example.com")
        emails = {m["user_id"]: m["email"] for m in self.admin.members(self.ctx)}
        self.assertEqual(emails[newbie], "new.person@example.com")

    def test_invitation_expiry_revoke_and_bad_tokens(self) -> None:
        created = self.admin.invite(self.ctx, "late@example.com")
        self.store.update(self.ctx, "workspace_invitations", created["id"], {"expires_at": utcnow() - timedelta(days=1)})
        with self.assertRaises(ValidationError):
            self.admin.accept_invitation(created["token"], user_id=str(uuid.uuid4()), email="late@example.com")
        other = self.admin.invite(self.ctx, "rev@example.com")
        self.admin.revoke_invitation(self.ctx, other["id"])
        with self.assertRaises(ValidationError):
            self.admin.accept_invitation(other["token"], user_id=str(uuid.uuid4()), email="rev@example.com")
        for bad in ("", "garbage", f"{uuid.uuid4()}.nope", f"{self.ws}.forged"):
            with self.assertRaises(ValidationError):
                self.admin.accept_invitation(bad, user_id=str(uuid.uuid4()), email="rev@example.com")

    def test_existing_member_cannot_accept(self) -> None:
        uid, _ = self._member("member")
        created = self.admin.invite(self.ctx, "dup@example.com")
        with self.assertRaises(ConflictError):
            self.admin.accept_invitation(created["token"], user_id=uid, email="dup@example.com")

    # --- teams & assignment ------------------------------------------------------
    def test_teams_and_assignment(self) -> None:
        uid, member_ctx = self._member("member")
        with self.assertRaises(ForbiddenError):
            self.admin.save_team(member_ctx, {"name": "Nope"})
        team = self.admin.save_team(self.ctx, {"name": "Enterprise", "description": "big accounts"})
        self.admin.add_team_member(self.ctx, team["id"], uid, "lead")
        with self.assertRaises(ValidationError):
            self.admin.add_team_member(self.ctx, team["id"], str(uuid.uuid4()))
        self.assertEqual(self.admin.teams(self.ctx)[0]["members"][0]["role"], "lead")
        companies = [self.store.insert(self.ctx, "companies", {"name": f"Co {i}"}) for i in range(3)]
        with self.assertRaises(ForbiddenError):
            self.admin.assign(member_ctx, "companies", [c["id"] for c in companies], uid)
        result = self.admin.assign(self.ctx, "companies", [c["id"] for c in companies] + ["co_missing"], uid)
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["missing"], ["co_missing"])
        self.assertTrue(all(self.store.get(self.ctx, "companies", c["id"])["owner_id"] == uid for c in companies))
        with self.assertRaises(ValidationError):
            self.admin.assign(self.ctx, "companies", [companies[0]["id"]], str(uuid.uuid4()))
        with self.assertRaises(ValidationError):
            self.admin.assign(self.ctx, "audit_log", [], uid)
        # The assignee is notified.
        mine = self.platform.service("notifications").list(member_ctx)
        self.assertEqual(mine[0]["kind"], "assignment")
        self.admin.delete_team(self.ctx, team["id"])
        self.assertEqual(self.admin.teams(self.ctx), [])

    # --- audit -------------------------------------------------------------------
    def test_audit_redacts_secrets_recursively_and_labels_actor(self) -> None:
        audit(self.store, self.ctx, "test.secret", changes={
            "api_key": "sk-live-123", "nested": {"password": "hunter2", "ok": "visible", "list": [{"token": "t"}]},
            "Authorization": "Bearer abc", "fields": ["api_key"], "note": "Bearer xyz", "secret_hint": "…1234"})
        row = self.store.first(self.ctx, "audit_log", {"action": "test.secret"})
        changes = row["changes"]
        self.assertEqual(changes["api_key"], REDACTED)
        self.assertEqual(changes["nested"]["password"], REDACTED)
        self.assertEqual(changes["nested"]["ok"], "visible")
        self.assertEqual(changes["nested"]["list"][0]["token"], REDACTED)
        self.assertEqual(changes["Authorization"], REDACTED)
        self.assertEqual(changes["note"], REDACTED)
        self.assertEqual(changes["fields"], ["api_key"])
        self.assertEqual(changes["secret_hint"], "…1234")
        self.assertEqual(row["actor_label"], "owner@example.com")
        self.assertNotIn("hunter2", json.dumps(changes))
        self.assertEqual(redact({"author": "Ann"}), {"author": "Ann"})

    # --- notifications -------------------------------------------------------------
    def test_notifications_are_per_user(self) -> None:
        uid, member_ctx = self._member("member")
        _, viewer_ctx = self._member("viewer")
        svc = self.platform.service("notifications")
        svc.notify(self.ctx, title="For everyone")
        svc.notify(self.ctx, title="For member", user_id=uid, severity="warning")
        self.assertEqual({n["title"] for n in svc.list(member_ctx)}, {"For everyone", "For member"})
        self.assertEqual({n["title"] for n in svc.list(viewer_ctx)}, {"For everyone"})
        private = next(n for n in svc.list(member_ctx) if n["title"] == "For member")
        with self.assertRaises(ForbiddenError):
            svc.mark_read(viewer_ctx, private["id"])
        self.assertEqual(svc.unread_count(member_ctx), 2)
        self.assertEqual(svc.mark_all_read(member_ctx), 2)
        self.assertEqual(svc.unread_count(member_ctx), 0)
        svc.notify(self.ctx, title="Another")
        self.assertEqual(svc.unread_count(viewer_ctx), 1)
        svc.mark_all_read(viewer_ctx)  # a read-only member can still mark read


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.owner = str(uuid.uuid4())
        self.ws = self.store.create_workspace(self.owner, "Int", f"int-{uuid.uuid4().hex[:8]}")["id"]
        self.ctx = Ctx(self.ws, self.owner, "owner")
        self.platform = Platform(self.store, config=PlatformConfig(secrets_key=Fernet.generate_key().decode()))
        self.http = FakeHTTP()
        self.svc = IntegrationService(self.platform, http=self.http, resolver=_public)
        self.platform.override("integrations", self.svc)

    def test_not_configured_everywhere_by_default(self) -> None:
        items = {i["provider"]: i for i in self.svc.list(self.ctx)}
        self.assertEqual(set(items), {"slack", "webhook", "google_workspace", "microsoft365", "google_calendar",
                                      "outlook_calendar"})
        self.assertTrue(all(i["status"] == "not_configured" and not i["connected"] for i in items.values()))
        self.assertIn("needs", items["google_calendar"])
        delivery = self.svc.deliver(self.ctx, "slack", "notification", {"text": "hi"})
        self.assertEqual(delivery["status"], "skipped")
        self.assertIn("not configured", delivery["error"])
        self.assertEqual(self.http.calls, [])
        self.assertEqual(self.svc.test(self.ctx, "slack")["status"], "not_configured")

    def test_slack_webhook_configure_test_and_secret_never_returned(self) -> None:
        url = "https://hooks.slack.com/services/T000/B000/XXXXSECRETXXXX"
        with self.assertRaises(ValidationError):
            self.svc.configure(self.ctx, "slack", secrets={"webhook_url": "https://evil.example.com/x"})
        out = self.svc.configure(self.ctx, "slack", secrets={"webhook_url": url}, settings={"events": ["test"]})
        self.assertTrue(out["connected"])
        self.assertNotIn("XXXXSECRETXXXX", json.dumps(out, default=str))
        self.assertNotIn("XXXXSECRETXXXX", json.dumps([r["changes"] for r in self.store.all(self.ctx, "audit_log")]))
        result = self.svc.test(self.ctx, "slack")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.http.calls[0]["url"], url)
        self.assertEqual(self.svc.status(self.ctx, "slack")["status"], "verified")
        # Subscribed only to "test": other events are skipped, not delivered.
        self.assertEqual(self.svc.deliver(self.ctx, "slack", "workflow_failed", {})["status"], "skipped")
        with self.assertRaises(ValidationError):
            self.svc.configure(self.ctx, "slack", settings={"events": ["bogus"]})

    def test_slack_bot_token_error_is_reported(self) -> None:
        self.http.text = json.dumps({"ok": False, "error": "channel_not_found"})
        with self.assertRaises(ValidationError):
            self.svc.configure(self.ctx, "slack", secrets={"bot_token": "xoxb-1234567890"})
        self.svc.configure(self.ctx, "slack", secrets={"bot_token": "xoxb-1234567890"}, settings={"channel": "C1"})
        delivery = self.svc.deliver(self.ctx, "slack", "notification", {"text": "x"})
        self.assertEqual(delivery["status"], "failed")
        self.assertIn("channel_not_found", delivery["error"])
        self.assertEqual(self.http.calls[0]["headers"]["Authorization"], "Bearer xoxb-1234567890")

    def test_webhook_is_signed_https_only_and_ssrf_checked(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.configure(self.ctx, "webhook", settings={"url": "http://example.com/hook"})
        with self.assertRaises(Exception):
            self.svc.configure(self.ctx, "webhook", settings={"url": "https://127.0.0.1/hook"})
        out = self.svc.configure(self.ctx, "webhook", settings={"url": "https://example.com/hook"})
        secret = out["signing_secret"]
        self.assertNotIn("signing_secret", self.svc.status(self.ctx, "webhook"))
        delivery = self.svc.deliver(self.ctx, "webhook", "reply_received", {"email": "a@b.com"})
        self.assertEqual(delivery["status"], "delivered")
        call = self.http.calls[-1]
        ts = int(call["headers"]["X-SANA-Timestamp"])
        self.assertEqual(call["headers"]["X-SANA-Signature"], sign_body(secret, call["data"], ts)["X-SANA-Signature"])
        self.assertEqual(json.loads(call["data"])["event"], "reply_received")
        self.http.status = 500
        failed = self.svc.deliver(self.ctx, "webhook", "notification", {})
        self.assertEqual(failed["status"], "failed")
        self.http.status = 200
        self.assertEqual(self.svc.retry(self.ctx, failed["id"])["status"], "delivered")
        self.assertEqual(len(self.svc.broadcast(self.ctx, "notification", {"text": "x"})), 1)

    def test_oauth_clients_and_calendars_are_honest(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.configure(self.ctx, "google_workspace", secrets={"client_id": "abc"})
        self.svc.configure(self.ctx, "google_workspace", secrets={"client_id": "abc.apps", "client_secret": "s3cr3t-val"})
        self.assertEqual(self.svc.test(self.ctx, "google_workspace")["status"], "configured_unverified")
        self.assertEqual(self.http.calls, [])
        self.svc.configure(self.ctx, "google_calendar", secrets={"access_token": "ya29.token-value"})
        missing = self.svc.deliver(self.ctx, "google_calendar", "notification", {"title": "Call"})
        self.assertEqual(missing["status"], "failed")
        event = self.svc.deliver(self.ctx, "google_calendar", "notification", {
            "title": "Intro call", "start": "2026-10-01T15:00:00Z", "end": "2026-10-01T15:30:00Z",
            "attendees": ["jane@acme.com"]})
        self.assertEqual(event["status"], "delivered")
        self.assertIn("googleapis.com/calendar", self.http.calls[-1]["url"])

    def test_mock_mode_records_without_calling(self) -> None:
        self.platform.config.extra["integrations_mock"] = True
        self.svc.configure(self.ctx, "slack", secrets={"webhook_url": "https://hooks.slack.com/services/a/b/c"})
        self.assertEqual(self.svc.deliver(self.ctx, "slack", "notification", {})["status"], "mocked")
        self.assertEqual(self.http.calls, [])

    def test_non_admin_cannot_configure_and_disconnect_clears(self) -> None:
        member = str(uuid.uuid4())
        self.store.add_member(self.ctx, member, "member")
        with self.assertRaises(ForbiddenError):
            self.svc.configure(Ctx(self.ws, member, "member"), "webhook", settings={"url": "https://example.com/h"})
        self.svc.configure(self.ctx, "webhook", settings={"url": "https://example.com/h"})
        self.assertFalse(self.svc.disconnect(self.ctx, "webhook")["connected"])

    def test_no_secrets_key_is_a_clear_error(self) -> None:
        platform = Platform(self.store, config=PlatformConfig(secrets_key=None))
        svc = IntegrationService(platform, http=self.http, resolver=_public)
        with self.assertRaises(ValidationError) as caught:
            svc.configure(self.ctx, "slack", secrets={"webhook_url": "https://hooks.slack.com/services/a/b/c"})
        self.assertIn("SECRETS_KEY", str(caught.exception))

    def test_integration_task(self) -> None:
        from cloud.intel.integrations.service import run_integration_task

        class Reporter:
            def progress(self, *a, **k):
                pass
        self.svc.configure(self.ctx, "webhook", settings={"url": "https://example.com/h"})
        out = run_integration_task(self.platform, Ctx.for_system(self.ws), {"params": {"event": "notification"}},
                                   Reporter())
        self.assertEqual(out["deliveries"][0]["status"], "delivered")


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"),
                                 config=PlatformConfig(files_dir=root / "platform",
                                                       secrets_key=Fernet.generate_key().decode()))
        self.issuer = DevTokenIssuer(SECRET)
        app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                         storage=LocalFileStorage(root / "results"), token_verifier=self.issuer,
                         dispatcher=NullDispatcher(), platform=self.platform)
        self.alice = self._client(app, "alice@example.com")
        self.bob = self._client(app, "bob@example.com")
        self.mallory = self._client(app, "mallory@example.com")
        r = self.alice.post("/api/v1/workspaces", json={"name": "Alice GTM", "seed": False})
        self.assertEqual(r.status_code, 201, r.text)
        self.ws = r.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"

    def _client(self, app, email):
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {self.issuer.issue(email)['access_token']}"
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_invite_accept_roles_and_isolation(self) -> None:
        r = self.alice.post(self.base + "/admin/invitations", json={"email": "bob@example.com", "role": "viewer"})
        self.assertEqual(r.status_code, 201, r.text)
        token = r.json()["token"]
        self.assertEqual(self.bob.get(self.base + "/admin/members").status_code, 404)
        self.assertEqual(self.mallory.post(self.base + "/invitations/accept", json={"token": token}).status_code, 403)
        r = self.bob.post(self.base + "/invitations/accept", json={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["role"], "viewer")
        # Read-only: can read and log a session, cannot write, invite or configure.
        self.assertEqual(self.bob.get(self.base + "/admin/members").status_code, 200)
        self.assertEqual(self.bob.post(self.base + "/companies", json={"name": "X"}).status_code, 403)
        self.assertEqual(self.bob.post(self.base + "/admin/invitations", json={"email": "c@example.com"}).status_code,
                         403)
        self.assertEqual(self.bob.put(self.base + "/integrations/slack", json={"secrets": {}}).status_code, 403)
        self.assertEqual(self.bob.post(self.base + "/audit/session", json={"event": "login"}).status_code, 200)
        me = self.bob.get(self.base + "/admin/overview").json()["you"]
        self.assertEqual(me["role_label"], "Read-only")
        self.assertFalse(me["permissions"]["records.write"])
        # Promote to manager: can assign, cannot manage members.
        bob_id = self.issuer.user_id_for("bob@example.com")
        r = self.alice.patch(f"{self.base}/admin/members/{bob_id}", json={"role": "manager"})
        self.assertEqual(r.status_code, 200, r.text)
        company = self.bob.post(self.base + "/companies", json={"name": "Bob Co"}).json()
        r = self.bob.post(self.base + "/admin/assign", json={"entity": "companies", "ids": [company["id"]],
                                                            "owner_id": bob_id})
        self.assertEqual(r.json()["updated"], 1)
        alice_id = self.issuer.user_id_for("alice@example.com")
        self.assertEqual(self.bob.patch(f"{self.base}/admin/members/{alice_id}", json={"role": "viewer"}).status_code,
                         403)
        self.assertEqual(self.alice.patch(f"{self.base}/admin/members/{alice_id}",
                                          json={"role": "viewer"}).status_code, 403)
        members = {m["user_id"]: m for m in self.alice.get(self.base + "/admin/members").json()["items"]}
        self.assertEqual(members[bob_id]["email"], "bob@example.com")
        self.assertEqual(self.alice.delete(f"{self.base}/admin/members/{bob_id}").status_code, 200)
        self.assertEqual(self.bob.get(self.base + "/admin/members").status_code, 404)

    def test_audit_search_export_and_session(self) -> None:
        self.alice.post(self.base + "/audit/session", json={"event": "login"})
        self.alice.post(self.base + "/companies", json={"name": "Searchable Inc"})
        r = self.alice.get(self.base + "/audit/search", params={"action": "session"})
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["items"]
        self.assertEqual(rows[0]["action"], "session.login")
        self.assertEqual(rows[0]["actor_label"], "alice@example.com")
        r = self.alice.get(self.base + "/audit/search", params={"actor": "alice", "from": "2000-01-01",
                                                                "to": "2999-12-31"})
        self.assertGreaterEqual(r.json()["total"], 2)
        self.assertEqual(self.alice.get(self.base + "/audit/search", params={"from": "not-a-date"}).status_code, 422)
        csv_response = self.alice.get(self.base + "/audit/export.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertTrue(csv_response.text.startswith("created_at,actor"))
        self.assertIn("session.login", csv_response.text)
        self.assertEqual(self.alice.post(self.base + "/audit/session", json={"event": "hack"}).status_code, 422)
        self.assertEqual(self.mallory.get(self.base + "/audit/search").status_code, 404)

    def test_teams_notifications_and_integrations_api(self) -> None:
        r = self.alice.post(self.base + "/admin/teams", json={"name": "Enterprise"})
        self.assertEqual(r.status_code, 201, r.text)
        team = r.json()
        alice_id = self.issuer.user_id_for("alice@example.com")
        self.assertEqual(self.alice.post(f"{self.base}/admin/teams/{team['id']}/members",
                                         json={"user_id": alice_id}).status_code, 201)
        self.assertEqual(self.alice.get(self.base + "/admin/teams").json()["items"][0]["members"][0]["user_id"],
                         alice_id)
        from cloud.intel.core.context import Ctx as C

        self.platform.service("notifications").notify(C.for_system(self.ws), title="Import finished")
        self.assertEqual(self.alice.get(self.base + "/notifications/count").json()["unread"], 1)
        self.assertEqual(self.alice.post(self.base + "/notifications/read-all").json()["marked"], 1)
        items = self.alice.get(self.base + "/integrations").json()["items"]
        self.assertTrue(all(i["status"] == "not_configured" for i in items))
        r = self.alice.put(self.base + "/integrations/google_workspace",
                           json={"secrets": {"client_id": "id.apps", "client_secret": "sssssssssss"}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("sssssssssss", r.text)
        self.assertEqual(self.alice.post(self.base + "/integrations/google_workspace/test").json()["status"],
                         "configured_unverified")
        self.assertEqual(self.alice.get(self.base + "/integrations/nope").status_code, 404)
        self.assertEqual(self.mallory.get(self.base + "/integrations").status_code, 404)


if __name__ == "__main__":
    unittest.main()
