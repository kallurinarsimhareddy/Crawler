"""User invitations end to end: create, list, resend, copy link, revoke, preview and
accept (new and existing accounts), member roles/teams/removal, owner protection,
audit, SMTP and no-SMTP delivery, secret hygiene, rate limiting, workspace
isolation — through the service, the real FastAPI app, and PostgreSQL RLS."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from cloud.api.auth import DevTokenIssuer
from cloud.api.main import create_app
from cloud.api.settings import Settings
from cloud.intel.admin.service import SmtpInviteMailer, hash_token, invite_url
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow
from cloud.intel.platform import Platform, PlatformConfig
from cloud.intel.store.memory import MemoryStore
from cloud.shared.storage import LocalFileStorage
from cloud.tests._pg import drop_database, fresh_database
from cloud.worker.dispatcher import NullDispatcher

SECRET = "platform-invitation-tests-secret-0123456789abcdef"
APP = "http://localhost:5173"


class FakeMailer:
    def __init__(self, configured=True, event="sent"):
        self.configured, self.event, self.sent = configured, event, []

    def send(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return {"event": self.event, "detail": None if self.event == "sent" else "550 relay denied"}


def _secret(token: str) -> str:
    return token.partition(".")[2]


class InvitationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.owner = str(uuid.uuid4())
        ws = self.store.create_workspace(self.owner, "Rise GTM", f"rise-{uuid.uuid4().hex[:8]}")
        self.ws = ws["id"]
        self.ctx = Ctx(self.ws, self.owner, "owner", actor_label="owner@example.com")
        self.platform = Platform(self.store, config=PlatformConfig(secrets_key=Fernet.generate_key().decode()))
        self.admin = self.platform.service("admin")
        self.mailer = FakeMailer(configured=False)
        self.admin.mailer = self.mailer

    def _member(self, role, email=None):
        uid = str(uuid.uuid4())
        self.store.add_member(self.ctx, uid, role)
        return uid, Ctx(self.ws, uid, role, actor_label=email)

    def _accept(self, token, email, user_id=None):
        user_id = user_id or str(uuid.uuid4())
        return user_id, self.admin.accept_invitation(token, user_id=user_id, email=email)

    # 1, 5, 22: an admin invites; without SMTP the invitation exists and nobody claims an email went out
    def test_admin_invites_and_no_smtp_falls_back_to_a_link(self) -> None:
        _, admin_ctx = self._member("admin", "admin@example.com")
        team = self.admin.save_team(self.ctx, {"name": "Enterprise"})
        created = self.admin.invite(admin_ctx, " Ann.Lee@Example.com ", "manager", first_name=" Ann ",
                                    last_name="Lee", team_id=team["id"], app_url=APP)
        self.assertEqual((created["email"], created["role"], created["status"]), ("ann.lee@example.com", "manager",
                                                                                   "pending"))
        self.assertEqual((created["first_name"], created["last_name"], created["team_id"]), ("Ann", "Lee", team["id"]))
        self.assertEqual(created["invited_by_label"], "admin@example.com")
        self.assertFalse(created["email_sent"])
        self.assertEqual(created["delivery"], "link")
        self.assertIn("not configured", created["delivery_message"])
        self.assertEqual(self.mailer.sent, [])
        self.assertTrue(created["invite_path"].startswith("/invite#"))
        self.assertEqual(created["invite_url"], invite_url(APP, created["token"]))
        self.assertGreaterEqual(len(_secret(created["token"])), 43)  # 32 random bytes
        row = self.store.get(self.ctx, "workspace_invitations", created["id"])
        self.assertEqual(row["token_hash"], hash_token(created["token"]))
        self.assertNotIn(_secret(created["token"]), json.dumps(row, default=str))
        self.assertGreater(row["expires_at"], utcnow() + timedelta(days=6))

    def test_invite_validates_input(self) -> None:
        for bad in ("", "not-an-email", "a@b"):
            with self.assertRaises(ValidationError):
                self.admin.invite(self.ctx, bad)
        with self.assertRaises(ValidationError):
            self.admin.invite(self.ctx, "x@example.com", "owner")
        with self.assertRaises(ValidationError):
            self.admin.invite(self.ctx, "x@example.com", team_id="tm_" + "0" * 32)
        with self.assertRaises(ValidationError):
            self.admin.invite(self.ctx, "x@example.com", first_name="x" * 101)

    # 2: nobody below admin can invite, list, resend, copy or revoke
    def test_non_admins_cannot_touch_invitations(self) -> None:
        created = self.admin.invite(self.ctx, "a@example.com")
        for role in ("manager", "member", "viewer"):
            _, ctx = self._member(role)
            with self.assertRaises(ForbiddenError):
                self.admin.invite(ctx, f"{role}@example.com")
            for call in (lambda: self.admin.invitations(ctx),
                         lambda: self.admin.resend_invitation(ctx, created["id"]),
                         lambda: self.admin.invitation_link(ctx, created["id"]),
                         lambda: self.admin.revoke_invitation(ctx, created["id"])):
                with self.assertRaises(ForbiddenError):
                    call()
            # the store hides the table from them too (what RLS does in PostgreSQL)
            self.assertEqual(self.store.all(ctx, "workspace_invitations"), [])
            self.assertIsNone(self.store.find(ctx, "workspace_invitations", created["id"]))

    # 3: one active invitation per address; an expired one does not block a new one
    def test_duplicate_active_invitation_rejected(self) -> None:
        first = self.admin.invite(self.ctx, "dup@example.com")
        with self.assertRaises(ConflictError):
            self.admin.invite(self.ctx, "DUP@example.com", "viewer")
        self.store.update(self.ctx, "workspace_invitations", first["id"], {"expires_at": utcnow() - timedelta(hours=1)})
        second = self.admin.invite(self.ctx, "dup@example.com")
        self.assertEqual(self.store.get(self.ctx, "workspace_invitations", first["id"])["status"], "expired")
        self.assertEqual(second["status"], "pending")

    # 4: an address that already belongs to a member is refused
    def test_existing_member_rejected(self) -> None:
        created = self.admin.invite(self.ctx, "joined@example.com")
        self._accept(created["token"], "joined@example.com")
        with self.assertRaises(ConflictError):
            self.admin.invite(self.ctx, "joined@example.com")
        # a signed-in member (known from the session audit) is refused as well
        uid, member_ctx = self._member("member", "known@example.com")
        self.platform.store.insert(member_ctx.as_system("user"), "audit_log", {
            "action": "session.login", "actor_id": uid, "actor_label": "known@example.com", "actor_kind": "user"})
        with self.assertRaises(ConflictError):
            self.admin.invite(self.ctx, "known@example.com")

    # 6: the list shows what the Invitations tab needs, never a secret
    def test_invitations_listed(self) -> None:
        team = self.admin.save_team(self.ctx, {"name": "SDRs"})
        self.admin.invite(self.ctx, "one@example.com", "viewer", first_name="One", team_id=team["id"])
        late = self.admin.invite(self.ctx, "two@example.com")
        self.store.update(self.ctx, "workspace_invitations", late["id"], {"expires_at": utcnow() - timedelta(days=1)})
        rows = {r["email"]: r for r in self.admin.invitations(self.ctx)}
        self.assertEqual(rows["one@example.com"]["role_label"], "Read-only")
        self.assertEqual(rows["one@example.com"]["team_name"], "SDRs")
        self.assertEqual(rows["one@example.com"]["name"], "One")
        self.assertEqual(rows["one@example.com"]["invited_by_label"], "owner@example.com")
        self.assertEqual(rows["two@example.com"]["status"], "expired")
        for row in rows.values():
            self.assertNotIn("token_hash", row)
            self.assertNotIn("token", row)

    # 7: resend issues a new token and expiry; the old link dies; expired can be revived
    def test_resend(self) -> None:
        created = self.admin.invite(self.ctx, "re@example.com")
        self.store.update(self.ctx, "workspace_invitations", created["id"], {"expires_at": utcnow() - timedelta(days=1)})
        again = self.admin.resend_invitation(self.ctx, created["id"])
        self.assertNotEqual(again["token"], created["token"])
        self.assertEqual(again["status"], "pending")
        self.assertGreater(again["expires_at"], utcnow() + timedelta(days=6))
        self.assertFalse(again["email_sent"])
        with self.assertRaises(ValidationError):
            self.admin.preview_invitation(created["token"])
        self.assertEqual(self.admin.preview_invitation(again["token"])["status"], "pending")
        self.admin.revoke_invitation(self.ctx, created["id"])
        with self.assertRaises(ValidationError):
            self.admin.resend_invitation(self.ctx, created["id"])

    # 8: revoke
    def test_revoke(self) -> None:
        created = self.admin.invite(self.ctx, "rev@example.com")
        revoked = self.admin.revoke_invitation(self.ctx, created["id"])
        self.assertEqual((revoked["status"], revoked["revoked_by"]), ("revoked", self.owner))
        self.assertIsNotNone(revoked["revoked_at"])
        with self.assertRaises(ValidationError):  # the revoked token matches nothing any more
            self._accept(created["token"], "rev@example.com")
        with self.assertRaises(ValidationError):
            self.admin.revoke_invitation(self.ctx, created["id"])

    # 9: expired
    def test_expired_invitation_rejected(self) -> None:
        created = self.admin.invite(self.ctx, "late@example.com")
        self.store.update(self.ctx, "workspace_invitations", created["id"], {"expires_at": utcnow() - timedelta(minutes=1)})
        self.assertEqual(self.admin.preview_invitation(created["token"])["status"], "expired")
        with self.assertRaisesRegex(ValidationError, "expired"):
            self._accept(created["token"], "late@example.com")
        self.assertEqual(self.store.get(self.ctx, "workspace_invitations", created["id"])["status"], "expired")

    # 10: malformed, unknown, forged
    def test_invalid_tokens_rejected_alike(self) -> None:
        created = self.admin.invite(self.ctx, "x@example.com")
        secret = _secret(created["token"])
        bad = ["", "garbage", f"{uuid.uuid4()}.{secret}", f"{self.ws}.{'A' * 43}", f"{self.ws}.short",
               f"{self.ws}.{secret}x", "x" * 500, f"not-a-uuid.{secret}"]
        for token in bad:
            with self.assertRaises(ValidationError) as caught:
                self.admin.preview_invitation(token)
            self.assertIn("not valid", str(caught.exception))
            with self.assertRaises(ValidationError):
                self._accept(token, "x@example.com")

    # 11: single use
    def test_token_cannot_be_reused(self) -> None:
        created = self.admin.invite(self.ctx, "once@example.com")
        self._accept(created["token"], "once@example.com")
        for user in (str(uuid.uuid4()),):
            with self.assertRaises(ValidationError):
                self._accept(created["token"], "once@example.com", user)
        # even a concurrent accept that read the row before the claim loses
        created = self.admin.invite(self.ctx, "race@example.com")
        system, row = self.admin._by_token(created["token"])
        self._accept(created["token"], "race@example.com")
        with self.assertRaises(ConflictError):
            self.store.update(system, "workspace_invitations", row["id"], {"status": "accepted"},
                              expected_version=row["version"])

    # 12-16: acceptance creates the membership with the right role and team
    def test_acceptance_creates_membership_role_and_team(self) -> None:
        team = self.admin.save_team(self.ctx, {"name": "Enterprise"})
        created = self.admin.invite(self.ctx, "new@example.com", "viewer", first_name="Nia", last_name="Park",
                                    team_id=team["id"])
        preview = self.admin.preview_invitation(created["token"])
        self.assertEqual((preview["workspace_name"], preview["email"], preview["role_label"], preview["team_name"]),
                         ("Rise GTM", "new@example.com", "Read-only", "Enterprise"))
        with self.assertRaises(ForbiddenError):  # wrong account
            self._accept(created["token"], "other@example.com")
        uid, result = self._accept(created["token"], "New@Example.com")
        self.assertEqual((result["workspace_id"], result["role"], result["team_name"]), (self.ws, "viewer", "Enterprise"))
        self.assertEqual(self.store.membership(uid, self.ws)["role"], "viewer")
        members = {m["user_id"]: m for m in self.admin.members(self.ctx)}
        self.assertEqual((members[uid]["email"], members[uid]["name"]), ("new@example.com", "Nia Park"))
        self.assertEqual(members[uid]["teams"], ["Enterprise"])
        self.assertEqual(members[uid]["status"], "active")
        self.assertIsNotNone(members[uid]["joined_at"])
        row = self.store.get(self.ctx, "workspace_invitations", created["id"])
        self.assertEqual((row["status"], row["accepted_by"]), ("accepted", uid))
        with self.assertRaises(ConflictError):  # already a member: a second invite for them cannot be used
            other = self.admin.invite(self.ctx, "alias@example.com")
            self._accept(other["token"], "alias@example.com", uid)

    def test_a_deleted_team_does_not_block_acceptance(self) -> None:
        team = self.admin.save_team(self.ctx, {"name": "Gone"})
        created = self.admin.invite(self.ctx, "t@example.com", team_id=team["id"])
        self.admin.delete_team(self.ctx, team["id"])
        uid, result = self._accept(created["token"], "t@example.com")
        self.assertIsNone(result["team_name"])
        self.assertEqual(self.store.membership(uid, self.ws)["role"], "member")

    # 17: another workspace's admin sees and changes nothing here
    def test_cross_workspace_isolation(self) -> None:
        other_owner = str(uuid.uuid4())
        other = self.store.create_workspace(other_owner, "Other", f"other-{uuid.uuid4().hex[:8]}")
        other_ctx = Ctx(other["id"], other_owner, "owner")
        mine = self.admin.invite(self.ctx, "iso@example.com")
        self.assertEqual(self.admin.invitations(other_ctx), [])
        for call in (lambda: self.admin.revoke_invitation(other_ctx, mine["id"]),
                     lambda: self.admin.resend_invitation(other_ctx, mine["id"]),
                     lambda: self.admin.invitation_link(other_ctx, mine["id"])):
            with self.assertRaises(NotFoundError):
                call()
        # the same secret under the other workspace's id matches nothing
        forged = f"{other['id']}.{_secret(mine['token'])}"
        with self.assertRaises(ValidationError):
            self._accept(forged, "iso@example.com")
        uid, result = self._accept(mine["token"], "iso@example.com")
        self.assertEqual(result["workspace_id"], self.ws)
        self.assertIsNone(self.store.membership(uid, other["id"]))

    # 19, 20: audit trail with actor and target, no secrets anywhere
    def test_audit_trail_and_no_secret_leaks(self) -> None:
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Grab(level=logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)

        self.admin.mailer = FakeMailer(configured=True, event="failed")
        team = self.admin.save_team(self.ctx, {"name": "Ops"})
        created = self.admin.invite(self.ctx, "aud@example.com", team_id=team["id"], app_url=APP)
        resent = self.admin.resend_invitation(self.ctx, created["id"], app_url=APP)
        link = self.admin.invitation_link(self.ctx, created["id"])
        uid, _ = self._accept(link["token"], "aud@example.com")
        self.admin.change_role(self.ctx, uid, "manager")
        second = self.admin.save_team(self.ctx, {"name": "Sales"})
        self.admin.set_member_teams(self.ctx, uid, [second["id"]])
        self.admin.remove_member(self.ctx, uid)
        doomed = self.admin.invite(self.ctx, "gone@example.com")
        self.admin.revoke_invitation(self.ctx, doomed["id"])

        rows = self.store.all(self.ctx, "audit_log", order="created_at")
        by_action = {}
        for row in rows:
            by_action.setdefault(row["action"], []).append(row)
        for action in ("user.invited", "invitation.resent", "invitation.revoked", "invitation.accepted",
                       "user.role_changed", "user.team_changed", "user.removed"):
            self.assertIn(action, by_action, action)
            row = by_action[action][-1]
            self.assertEqual(row["workspace_id"], self.ws)
            self.assertTrue(row["entity_id"])
            self.assertIsNotNone(row["created_at"])
        self.assertEqual(by_action["user.invited"][0]["actor_id"], self.owner)
        self.assertEqual(by_action["invitation.accepted"][0]["actor_id"], uid)
        self.assertEqual(by_action["user.team_changed"][0]["changes"]["to"], [second["id"]])
        dumped = json.dumps(rows, default=str) + "\n".join(records)
        for result in (created, resent, link, doomed):
            self.assertNotIn(_secret(result["token"]), dumped)
            self.assertNotIn(hash_token(result["token"]), dumped)

    # 21: copy link issues a working link and retires the previous one
    def test_copy_invite_link(self) -> None:
        created = self.admin.invite(self.ctx, "copy@example.com")
        link = self.admin.invitation_link(self.ctx, created["id"])
        self.assertTrue(link["invite_path"].endswith(link["token"]))
        self.assertIsNone(link["invite_url"])  # the web app builds the full link from its own origin
        with self.assertRaises(ValidationError):
            self.admin.preview_invitation(created["token"])
        self.assertEqual(self.admin.preview_invitation(link["token"])["email"], "copy@example.com")
        self.store.update(self.ctx, "workspace_invitations", created["id"], {"expires_at": utcnow() - timedelta(days=1)})
        with self.assertRaisesRegex(ValidationError, "expired"):  # expired: resend instead
            self.admin.invitation_link(self.ctx, created["id"])

    # 22, 23: delivery paths
    def test_smtp_configured_sends_and_failures_fall_back(self) -> None:
        mailer = FakeMailer(configured=True)
        self.admin.mailer = mailer
        created = self.admin.invite(self.ctx, "mail@example.com", "manager", app_url=APP)
        self.assertTrue(created["email_sent"])
        self.assertEqual((created["delivery"], created["sent_count"]), ("email", 1))
        self.assertEqual(created["delivery_message"], "Invitation emailed to mail@example.com.")
        sent = mailer.sent[0]
        self.assertEqual(sent["to"], "mail@example.com")
        self.assertIn("Rise GTM", sent["subject"])
        self.assertIn(invite_url(APP, created["token"]), sent["body"])
        self.assertIn("Manager", sent["body"])
        # configured, but the app address is unknown: no email, said plainly
        no_url = self.admin.invite(self.ctx, "nourl@example.com")
        self.assertFalse(no_url["email_sent"])
        self.assertIn("CAREERCLOUD_PUBLIC_APP_URL", no_url["delivery_message"])
        # the SMTP server refuses: the invitation still exists, marked email_failed
        mailer.event = "failed"
        failed = self.admin.invite(self.ctx, "fail@example.com", app_url=APP)
        self.assertFalse(failed["email_sent"])
        self.assertEqual(failed["delivery"], "email_failed")
        self.assertIn("could not be sent", failed["delivery_message"])
        self.assertEqual(self.admin.preview_invitation(failed["token"])["status"], "pending")

    def test_smtp_mailer_uses_the_platform_smtp_settings(self) -> None:
        self.assertFalse(SmtpInviteMailer({}).configured)
        self.assertFalse(SmtpInviteMailer({"CAREERCLOUD_SMTP_HOST": "smtp.example.com"}).configured)
        env = {"CAREERCLOUD_SMTP_HOST": "smtp.example.com", "CAREERCLOUD_SMTP_FROM": "noreply@example.com",
               "CAREERCLOUD_SMTP_PORT": "2525"}
        mailer = SmtpInviteMailer(env)
        self.assertTrue(mailer.configured)
        delivered = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host, self.port = host, port

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                pass

            def login(self, *_):
                pass

            def send_message(self, msg):
                delivered.append((self.host, self.port, msg["To"], msg["Subject"]))

        with mock.patch("smtplib.SMTP", FakeSMTP):
            self.assertEqual(mailer.send("a@example.com", "Hi", "Body")["event"], "sent")
        self.assertEqual(delivered, [("smtp.example.com", 2525, "a@example.com", "Hi")])

        def refuse(*_a, **_k):
            raise ConnectionRefusedError("refused")

        with mock.patch("smtplib.SMTP", refuse):
            self.assertEqual(mailer.send("a@example.com", "Hi", "Body")["event"], "failed")

    # 24-26: owner protection, role changes, team changes, removal
    def test_owner_protection_role_team_and_removal(self) -> None:
        admin_uid, admin_ctx = self._member("admin")
        for call in (lambda: self.admin.remove_member(admin_ctx, self.owner),
                     lambda: self.admin.change_role(admin_ctx, self.owner, "viewer")):
            with self.assertRaises(ForbiddenError):
                call()
        uid, member_ctx = self._member("member")
        self.assertTrue(self.admin.change_role(admin_ctx, uid, "viewer")["changed"])
        self.assertFalse(self.admin.change_role(admin_ctx, uid, "viewer")["changed"])
        with self.assertRaises(ForbiddenError):
            self.admin.change_role(member_ctx, admin_uid, "viewer")
        team_a = self.admin.save_team(self.ctx, {"name": "A"})
        team_b = self.admin.save_team(self.ctx, {"name": "B"})
        _, manager_ctx = self._member("manager")
        self.assertEqual(self.admin.set_member_teams(manager_ctx, uid, [team_a["id"], team_b["id"]])["teams"],
                         ["A", "B"])
        self.assertEqual(self.admin.set_member_teams(manager_ctx, uid, [team_b["id"]])["teams"], ["B"])
        with self.assertRaises(ForbiddenError):
            self.admin.set_member_teams(member_ctx, uid, [])
        with self.assertRaises(ValidationError):
            self.admin.set_member_teams(manager_ctx, uid, ["tm_" + "f" * 32])
        with self.assertRaises(NotFoundError):
            self.admin.set_member_teams(manager_ctx, str(uuid.uuid4()), [])
        with self.assertRaises(ForbiddenError):
            self.admin.remove_member(manager_ctx, uid)
        self.assertTrue(self.admin.remove_member(admin_ctx, uid)["removed"])
        self.assertIsNone(self.store.membership(uid, self.ws))
        self.assertEqual(self.admin.teams(self.ctx)[1]["members"], [])


class InvitationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.platform = Platform(MemoryStore(), storage=LocalFileStorage(root / "platform"),
                                 config=PlatformConfig(files_dir=root / "platform",
                                                       secrets_key=Fernet.generate_key().decode()))
        self.platform.service("admin").mailer = FakeMailer(configured=False)
        self.issuer = DevTokenIssuer(SECRET)
        self.app = create_app(Settings(auth_mode="dev", results_dir=root / "results"),
                              storage=LocalFileStorage(root / "results"), token_verifier=self.issuer,
                              dispatcher=NullDispatcher(), platform=self.platform)
        self.alice = self._client("alice@example.com")
        self.bob = self._client("bob@example.com")
        self.carol = self._client("carol@example.com")
        self.anonymous = self._client(None)
        r = self.alice.post("/api/v1/workspaces", json={"name": "Alice GTM", "seed": False})
        self.assertEqual(r.status_code, 201, r.text)
        self.ws = r.json()["id"]
        self.base = f"/api/v1/w/{self.ws}"

    def _client(self, email):
        client = TestClient(self.app)
        if email:
            client.headers["Authorization"] = f"Bearer {self.issuer.issue(email)['access_token']}"
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def _invite(self, email, role="member", **extra):
        r = self.alice.post(self.base + "/admin/invitations", json={"email": email, "role": role, **extra},
                            headers={"Origin": APP})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    # 12, 13: a brand-new account and an account that already has a workspace both join
    def test_new_and_existing_accounts_accept(self) -> None:
        # carol already owns a workspace (an existing account); bob has none (a new sign-up)
        self.assertEqual(self.carol.post("/api/v1/workspaces", json={"name": "Carol Co", "seed": False}).status_code,
                         201)
        self.assertEqual(self.bob.get("/api/v1/workspaces").json()["items"], [])
        team = self.alice.post(self.base + "/admin/teams", json={"name": "Enterprise"}).json()
        bob_invite = self._invite("bob@example.com", "viewer", first_name="Bob", team_id=team["id"])
        carol_invite = self._invite("carol@example.com", "admin")
        self.assertEqual(bob_invite["invite_url"], f"{APP}/invite#{bob_invite['token']}")
        self.assertFalse(bob_invite["email_sent"])

        # the invite page, before sign-in
        preview = self.anonymous.post("/api/v1/invitations/preview", json={"token": bob_invite["token"]})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual((preview.json()["workspace_name"], preview.json()["email"], preview.json()["role_label"],
                          preview.json()["team_name"]), ("Alice GTM", "bob@example.com", "Read-only", "Enterprise"))
        self.assertEqual(self.anonymous.post("/api/v1/invitations/accept", json={"token": bob_invite["token"]})
                         .status_code, 401)
        self.assertEqual(self.carol.post("/api/v1/invitations/accept", json={"token": bob_invite["token"]})
                         .status_code, 403)

        r = self.bob.post("/api/v1/invitations/accept", json={"token": bob_invite["token"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["workspace_id"], r.json()["role"]), (self.ws, "viewer"))
        self.assertEqual([w["id"] for w in self.bob.get("/api/v1/workspaces").json()["items"]], [self.ws])
        self.assertEqual(self.bob.post("/api/v1/invitations/accept", json={"token": bob_invite["token"]})
                         .status_code, 422)

        r = self.carol.post("/api/v1/invitations/accept", json={"token": carol_invite["token"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.carol.get("/api/v1/workspaces").json()["items"]), 2)

        members = {m["email"]: m for m in self.alice.get(self.base + "/admin/members").json()["items"]}
        self.assertEqual(members["bob@example.com"]["role"], "viewer")
        self.assertEqual(members["bob@example.com"]["teams"], ["Enterprise"])
        self.assertEqual(members["carol@example.com"]["role"], "admin")
        me = next(m for m in self.alice.get(self.base + "/admin/members").json()["items"] if m["is_you"])
        self.assertTrue(me["is_owner"])

        # bob is read-only: no invitations, no member changes; carol (admin) can
        self.assertEqual(self.bob.get(self.base + "/admin/invitations").status_code, 403)
        self.assertEqual(self.bob.post(self.base + "/admin/invitations", json={"email": "z@example.com"}).status_code,
                         403)
        self.assertEqual(self.carol.get(self.base + "/admin/invitations").status_code, 200)

    # 7, 8, 21 through the API; 20: responses never carry a hash
    def test_list_resend_link_revoke_api(self) -> None:
        created = self._invite("dan@example.com")
        self.assertEqual(self._post_status(self.base + "/admin/invitations", {"email": "dan@example.com"}), 409)
        listed = self.alice.get(self.base + "/admin/invitations").json()["items"]
        self.assertEqual([i["email"] for i in listed], ["dan@example.com"])
        self.assertNotIn("token_hash", json.dumps(listed))
        r = self.alice.post(f"{self.base}/admin/invitations/{created['id']}/resend", headers={"Origin": APP})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("not configured", r.json()["delivery_message"])
        link = self.alice.post(f"{self.base}/admin/invitations/{created['id']}/link").json()
        for old in (created["token"], r.json()["token"]):
            self.assertEqual(self.anonymous.post("/api/v1/invitations/preview", json={"token": old}).status_code, 422)
        self.assertEqual(self.anonymous.post("/api/v1/invitations/preview", json={"token": link["token"]}).status_code,
                         200)
        self.assertEqual(self.alice.post(f"{self.base}/admin/invitations/{created['id']}/revoke").status_code, 200)
        r = self.anonymous.post("/api/v1/invitations/preview", json={"token": link["token"]})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("token_hash", self.alice.post(self.base + "/admin/invitations",
                                                       json={"email": "eve@example.com"}).text)

    def _post_status(self, path, body):
        return self.alice.post(path, json=body).status_code

    # 17: another workspace's admin, and a token pointed at another workspace
    def test_cross_workspace_api(self) -> None:
        created = self._invite("iso@example.com")
        other = self.carol.post("/api/v1/workspaces", json={"name": "Carol Co", "seed": False}).json()
        other_base = f"/api/v1/w/{other['id']}"
        self.assertEqual(self.carol.get(self.base + "/admin/invitations").status_code, 404)
        self.assertEqual(self.carol.get(other_base + "/admin/invitations").json()["items"], [])
        self.assertEqual(self.carol.post(f"{other_base}/admin/invitations/{created['id']}/revoke").status_code, 404)
        self.assertEqual(self.carol.post(f"{self.base}/admin/invitations/{created['id']}/revoke").status_code, 404)
        iso = self._client("iso@example.com")
        # the legacy workspace-scoped accept refuses a token for another workspace
        self.assertEqual(iso.post(f"{other_base}/invitations/accept", json={"token": created["token"]}).status_code,
                         422)
        forged = f"{other['id']}.{_secret(created['token'])}"
        self.assertEqual(iso.post("/api/v1/invitations/accept", json={"token": forged}).status_code, 422)
        self.assertEqual(iso.post("/api/v1/invitations/accept", json={"token": created["token"]}).status_code, 200)
        self.assertEqual([w["id"] for w in iso.get("/api/v1/workspaces").json()["items"]], [self.ws])

    # 24-26 through the API
    def test_member_management_api(self) -> None:
        bob_id = self.issuer.user_id_for("bob@example.com")
        alice_id = self.issuer.user_id_for("alice@example.com")
        token = self._invite("bob@example.com", "admin")["token"]
        self.assertEqual(self.bob.post("/api/v1/invitations/accept", json={"token": token}).status_code, 200)
        # an admin can never remove or demote the owner
        self.assertEqual(self.bob.delete(f"{self.base}/admin/members/{alice_id}").status_code, 403)
        self.assertEqual(self.bob.patch(f"{self.base}/admin/members/{alice_id}/role", json={"role": "viewer"})
                         .status_code, 403)
        r = self.alice.patch(f"{self.base}/admin/members/{bob_id}/role", json={"role": "manager"})
        self.assertEqual((r.status_code, r.json()["role"]), (200, "manager"))
        team = self.alice.post(self.base + "/admin/teams", json={"name": "Enterprise"}).json()
        r = self.bob.patch(f"{self.base}/admin/members/{bob_id}/teams", json={"team_ids": [team["id"]]})
        self.assertEqual((r.status_code, r.json()["teams"]), (200, ["Enterprise"]))
        self.assertEqual(self.bob.patch(f"{self.base}/admin/members/{bob_id}/teams", json={"team_ids": "x"})
                         .status_code, 422)
        self.assertEqual(self.bob.delete(f"{self.base}/admin/members/{alice_id}").status_code, 403)
        self.assertEqual(self.alice.delete(f"{self.base}/admin/members/{bob_id}").status_code, 200)
        self.assertEqual(self.bob.get(self.base + "/admin/members").status_code, 404)
        actions = {r["action"] for r in self.alice.get(self.base + "/audit/search", params={"limit": 200})
                   .json()["items"]}
        self.assertTrue({"user.invited", "invitation.accepted", "user.role_changed", "user.team_changed",
                         "user.removed"} <= actions)

    # brute force: the token-only endpoints are rate limited per client
    def test_preview_and_accept_are_rate_limited(self) -> None:
        codes = [self.anonymous.post("/api/v1/invitations/preview", json={"token": f"{self.ws}.{'x' * 43}"}).status_code
                 for _ in range(21)]
        self.assertEqual(codes[:20], [422] * 20)
        self.assertEqual(codes[20], 429)
        self.assertEqual(self.bob.post("/api/v1/invitations/accept", json={"token": "x"}).status_code, 429)


class InvitationRlsTests(unittest.TestCase):
    """18: PostgreSQL row-level security on workspace_invitations and workspace_members."""

    @classmethod
    def setUpClass(cls) -> None:
        from cloud.intel.store.postgres import PostgresStore

        cls.url = fresh_database()
        cls.pg = PostgresStore.from_url(cls.url, max_size=4)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.pg.close()
        drop_database(cls.url)

    def setUp(self) -> None:
        self.store = self.pg
        self.owner, self.admin_id, self.member_id, self.stranger = (str(uuid.uuid4()) for _ in range(4))
        ws = self.store.create_workspace(self.owner, "RLS", f"rls-{uuid.uuid4().hex[:8]}")
        other = self.store.create_workspace(self.stranger, "Other", f"oth-{uuid.uuid4().hex[:8]}")
        self.ws, self.other_ws = ws["id"], other["id"]
        self.owner_ctx = Ctx(self.ws, self.owner, "owner", actor_label="owner@example.com")
        self.store.add_member(self.owner_ctx, self.admin_id, "admin")
        self.store.add_member(self.owner_ctx, self.member_id, "member")
        self.platform = Platform(self.store, config=PlatformConfig(secrets_key=Fernet.generate_key().decode()))
        self.service = self.platform.service("admin")
        self.service.mailer = FakeMailer(configured=False)
        self.admin_ctx = Ctx(self.ws, self.admin_id, "admin", actor_label="admin@example.com")

    def _as_user(self, user_id, statement, params=()):
        import psycopg

        with psycopg.connect(self.url) as conn:
            with conn.transaction():
                conn.execute("select set_config('request.jwt.claims', %s, true)",
                             [json.dumps({"sub": user_id, "role": "authenticated"})])
                conn.execute("set local role authenticated")
                cursor = conn.execute(statement, params)
                return cursor.fetchall() if cursor.description else cursor.rowcount

    def test_admin_creates_member_cannot(self) -> None:
        import psycopg

        created = self.service.invite(self.admin_ctx, "pg@example.com", "manager")
        self.assertEqual(created["status"], "pending")
        with self.assertRaises(ForbiddenError):  # the service refuses before SQL...
            self.service.invite(Ctx(self.ws, self.member_id, "member"), "no@example.com")
        insert = ("insert into careercloud.workspace_invitations (id, workspace_id, email, token_hash, expires_at, "
                  "created_by) values (%s, %s, 'raw@example.com', 'h', now() + interval '1 day', %s)")
        with self.assertRaises(psycopg.Error):  # ...and RLS refuses the raw insert
            self._as_user(self.member_id, insert, (f"inv_{uuid.uuid4().hex}", self.ws, self.member_id))
        self._as_user(self.admin_id, insert, (f"inv_{uuid.uuid4().hex}", self.ws, self.admin_id))

    def test_read_isolation(self) -> None:
        self.service.invite(self.admin_ctx, "read@example.com")
        select = "select email, token_hash from careercloud.workspace_invitations"
        self.assertEqual({r[0] for r in self._as_user(self.admin_id, select)}, {"read@example.com"})
        self.assertEqual(self._as_user(self.owner, select)[0][0], "read@example.com")
        self.assertEqual(self._as_user(self.member_id, select), [])   # a plain member of the same workspace
        self.assertEqual(self._as_user(self.stranger, select), [])    # another workspace's owner
        self.assertEqual(self.store.all(Ctx(self.ws, self.member_id, "member"), "workspace_invitations"), [])

    def test_revoke(self) -> None:
        created = self.service.invite(self.admin_ctx, "revoke@example.com")
        update = "update careercloud.workspace_invitations set status = 'revoked' where id = %s"
        self.assertEqual(self._as_user(self.member_id, update, (created["id"],)), 0)
        self.assertEqual(self._as_user(self.stranger, update, (created["id"],)), 0)
        self.assertEqual(self.service.revoke_invitation(self.admin_ctx, created["id"])["status"], "revoked")
        with self.assertRaises(ValidationError):
            self.service.accept_invitation(created["token"], user_id=str(uuid.uuid4()), email="revoke@example.com")

    def test_acceptance_creates_membership(self) -> None:
        team = self.service.save_team(self.admin_ctx, {"name": "PG Team"})
        created = self.service.invite(self.admin_ctx, "joiner@example.com", "viewer", team_id=team["id"])
        joiner = str(uuid.uuid4())
        self.assertIsNone(self.store.membership(joiner, self.ws))
        result = self.service.accept_invitation(created["token"], user_id=joiner, email="joiner@example.com")
        self.assertEqual(result["role"], "viewer")
        self.assertEqual(self.store.membership(joiner, self.ws)["role"], "viewer")
        members = {m["user_id"]: m for m in self.service.members(self.owner_ctx)}
        self.assertEqual(members[joiner]["teams"], ["PG Team"])
        self.assertIsNotNone(members[joiner]["joined_at"])
        with self.assertRaises(ValidationError):
            self.service.accept_invitation(created["token"], user_id=str(uuid.uuid4()), email="joiner@example.com")
        # members of other roles can list members (names come from admin-only rows, read in system scope)
        viewer_view = {m["user_id"]: m for m in self.service.members(Ctx(self.ws, joiner, "viewer"))}
        self.assertEqual(viewer_view[joiner]["email"], "joiner@example.com")

    def test_membership_rows_only_admins_write(self) -> None:
        import psycopg

        insert = "insert into careercloud.workspace_members (workspace_id, user_id, role) values (%s, %s, 'admin')"
        with self.assertRaises(psycopg.Error):  # a member cannot add anyone
            self._as_user(self.member_id, insert, (self.ws, str(uuid.uuid4())))
        with self.assertRaises(psycopg.Error):  # nobody joins a workspace they are not admin of
            self._as_user(self.stranger, insert, (self.ws, self.stranger))
        delete = "delete from careercloud.workspace_members where workspace_id = %s and user_id = %s"
        self.assertEqual(self._as_user(self.admin_id, delete, (self.ws, self.owner)), 0)  # the owner stays

    def test_cross_workspace_token_rejected(self) -> None:
        created = self.service.invite(self.admin_ctx, "cross@example.com")
        forged = f"{self.other_ws}.{_secret(created['token'])}"
        with self.assertRaises(ValidationError):
            self.service.accept_invitation(forged, user_id=str(uuid.uuid4()), email="cross@example.com")
        stranger_ctx = Ctx(self.other_ws, self.stranger, "owner")
        self.assertEqual(self.service.invitations(stranger_ctx), [])
        with self.assertRaises(NotFoundError):
            self.service.revoke_invitation(stranger_ctx, created["id"])


if __name__ == "__main__":
    unittest.main()
