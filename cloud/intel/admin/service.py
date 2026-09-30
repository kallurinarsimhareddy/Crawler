"""Workspace administration: members and roles, invitations, teams, ownership.

Roles (stored in ``workspace_members.role``) and what the UI calls them:

========  ==========  ==================================================
stored    shown as    rights
========  ==========  ==================================================
owner     Owner       everything; cannot be removed or demoted by others
admin     Admin       members, roles, providers, integrations, settings
manager   Manager     writes like a user, plus teams and record assignment
member    User        reads and writes records
viewer    Read-only   reads only (RLS refuses every write)
========  ==========  ==================================================

:func:`permissions_for` is the single matrix the UI renders and the server
enforces through ``Ctx.can_write`` / ``can_manage`` / ``can_admin``.

Invitations (admins only, also in RLS since migration 0009):

* The token is ``<workspace id>.<32 random bytes, url-safe>``. It is returned
  once — on create, resend or "copy link" — and stored only as a SHA-256 hash;
  issuing a new one replaces the hash, so earlier links stop working.
* The workspace id in the token lets the invitee — not yet a member, so RLS
  hides the workspace from them — be matched without scanning other workspaces.
  A token edited to name another workspace hashes to nothing there.
* Accepting needs the invitee's signed-in email to match, claims the row with
  its version (single use, even under concurrent accepts), then adds the
  membership (and team). Accepted and revoked rows get an unguessable hash.
* Email is sent only when SMTP is configured (``CAREERCLOUD_SMTP_HOST`` and
  ``CAREERCLOUD_SMTP_FROM``) and the app address is known; otherwise the admin
  copies the link, and the API says plainly that no email was sent.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import normalize_email

__all__ = ["AdminService", "ROLE_LABELS", "PERMISSIONS", "permissions_for", "hash_token", "SmtpInviteMailer",
           "invite_path", "invite_url"]

log = logging.getLogger(__name__)

ROLE_LABELS: Dict[str, str] = {"owner": "Owner", "admin": "Admin", "manager": "Manager", "member": "User",
                               "viewer": "Read-only"}
ASSIGNABLE_ROLES = ("admin", "manager", "member", "viewer")

_ALL = ("owner", "admin", "manager", "member", "viewer")
_WRITE = ("owner", "admin", "manager", "member")
_MANAGE = ("owner", "admin", "manager")
_ADMIN = ("owner", "admin")

#: key -> (label, group, roles that have it)
PERMISSIONS: Dict[str, tuple] = {
    "records.read": ("View companies, contacts, deals and intelligence", "Records", _ALL),
    "records.write": ("Create and edit records", "Records", _WRITE),
    "records.delete": ("Delete records", "Records", _WRITE),
    "crm.apply_proposals": ("Review and apply CRM proposals", "Records", _WRITE),
    "imports.run": ("Import files and internal data", "Data", _WRITE),
    "exports.run": ("Export data", "Data", _WRITE),
    "scraper.run": ("Run the AI scraper and research agent", "Data", _WRITE),
    "email.validate": ("Validate emails", "GTM", _WRITE),
    "campaigns.manage": ("Create and edit campaigns, sequences and templates", "GTM", _WRITE),
    "enrollments.approve": ("Approve sequence enrollments for sending", "GTM", _WRITE),
    "suppression.manage": ("Manage the suppression list", "GTM", _WRITE),
    "workflows.manage": ("Build and enable workflows", "Automation", _WRITE),
    "assign.owner": ("Assign record owners", "Team", _MANAGE),
    "teams.manage": ("Create teams and manage team members", "Team", _MANAGE),
    "audit.view": ("View the audit log", "Admin", _ALL),
    "audit.export": ("Export the audit log", "Admin", _MANAGE),
    "members.manage": ("Invite and remove members, change roles", "Admin", _ADMIN),
    "providers.configure": ("Connect providers, mailboxes and API keys", "Admin", _ADMIN),
    "integrations.configure": ("Configure integrations (Slack, webhooks, calendar)", "Admin", _ADMIN),
    "settings.workspace": ("Change workspace settings", "Admin", _ADMIN),
}

#: Entities whose ``owner_id`` can be bulk-assigned.
ASSIGNABLE = ("companies", "contacts", "opportunities", "campaigns")
INVITE_DAYS = 7


def permissions_for(role: str) -> Dict[str, bool]:
    return {key: role in roles for key, (_, _, roles) in PERMISSIONS.items()}


def permission_matrix() -> Dict[str, Any]:
    return {"roles": [{"role": r, "label": ROLE_LABELS[r]} for r in _ALL],
            "permissions": [{"key": k, "label": label, "group": group, "roles": list(roles)}
                            for k, (label, group, roles) in PERMISSIONS.items()]}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite_token(workspace_id: str) -> str:
    return f"{workspace_id}.{secrets.token_urlsafe(32)}"


def invite_path(token: str) -> str:
    """The web app's invite page. The token rides in the fragment, which browsers never
    send to a server, so it stays out of access logs, analytics and Referer headers."""
    return f"/invite#{token}"


def invite_url(app_url: str, token: str) -> str:
    return app_url.rstrip("/") + invite_path(token)


INVALID_INVITE = "This invitation link is not valid. Ask a workspace admin for a new one."
_STATUS_MESSAGES = {
    "pending": None,
    "expired": "This invitation has expired. Ask a workspace admin to resend it.",
    "revoked": "This invitation was revoked. Ask a workspace admin for a new one.",
    "accepted": "This invitation has already been used.",
}
#: Audit actions that carry a user's email as the actor label (the legacy name included).
_LABEL_ACTIONS = ("session.login", "invitation.accepted", "invitation.accept")


def _status(row: Mapping[str, Any], now: datetime) -> str:
    if row["status"] == "pending" and row["expires_at"] < now:
        return "expired"
    return str(row["status"])


def _full_name(first: Optional[str], last: Optional[str]) -> Optional[str]:
    return " ".join(p for p in (first, last) if p) or None


def _name_part(value: Optional[str], label: str) -> Optional[str]:
    text = " ".join(str(value or "").split())
    if len(text) > 100:
        raise ValidationError(f"{label} is too long (100 characters at most)")
    return text or None


def _invite_email(*, workspace: str, inviter: Optional[str], role: str, url: str,
                  expires_at: datetime) -> Tuple[str, str]:
    who = inviter or "A workspace admin"
    subject = f"You're invited to {workspace} on SANA GTM"
    body = (f"{who} invited you to join {workspace} on SANA GTM as {role}.\n\n"
            f"Accept the invitation:\n{url}\n\n"
            f"Sign in, or create an account, with this email address. The link works once and expires on "
            f"{expires_at:%B %d, %Y}.\n\nIf you did not expect this invitation, you can ignore this email.\n")
    return subject, body


class SmtpInviteMailer:
    """Sends invitation emails through the platform's SMTP settings, when they exist."""

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self.env = env

    def _env(self) -> Mapping[str, str]:
        return self.env if self.env is not None else os.environ

    @property
    def configured(self) -> bool:
        env = self._env()
        return bool(env.get("CAREERCLOUD_SMTP_HOST", "").strip() and env.get("CAREERCLOUD_SMTP_FROM", "").strip())

    def send(self, to: str, subject: str, body: str) -> Dict[str, Any]:
        from cloud.intel.gtm.sequences import SmtpSender

        try:
            return SmtpSender.from_env(self._env()).send({"to": to, "subject": subject, "body": body})
        except (OSError, ValidationError) as error:  # unreachable host, refused connection, bad settings
            return {"event": "failed", "detail": type(error).__name__}


class AdminService:
    def __init__(self, platform: Any, mailer: Any = None) -> None:
        self.platform = platform
        self.store = platform.store
        #: ``configured`` and ``send(to, subject, body) -> {"event": "sent"|"failed", ...}``.
        self.mailer = mailer or getattr(platform, "invite_mailer", None) or SmtpInviteMailer()

    # --- members -------------------------------------------------------------------

    def _known(self, ctx: Ctx) -> Dict[str, Dict[str, Any]]:
        """user_id -> {"email", "name"}, learned from accepted invitations and signed-in audit rows.

        Invitations are admin-only (RLS), but every member may see who else is in the
        workspace, so they are read in the system scope of this same workspace."""
        known: Dict[str, Dict[str, Any]] = {}
        for row in self.store.all(ctx, "audit_log", {"action__in": list(_LABEL_ACTIONS)}, order="created_at",
                                  cap=2000):
            if row.get("actor_id") and row.get("actor_label"):
                known.setdefault(str(row["actor_id"]), {})["email"] = row["actor_label"]
        system = Ctx.for_system(ctx.workspace_id)
        for inv in self.store.all(system, "workspace_invitations", {"status": "accepted"}, order="created_at",
                                  cap=2000):
            if inv.get("accepted_by"):
                entry = known.setdefault(str(inv["accepted_by"]), {})
                entry.setdefault("email", inv["email"])
                name = _full_name(inv.get("first_name"), inv.get("last_name"))
                if name:
                    entry["name"] = name
        return known

    def members(self, ctx: Ctx) -> List[Dict[str, Any]]:
        known = self._known(ctx)
        teams = {t["id"]: t["name"] for t in self.store.all(ctx, "teams", cap=500)}
        by_user: Dict[str, List[str]] = {}
        for tm in self.store.all(ctx, "team_members", cap=5000):
            if tm["team_id"] in teams:
                by_user.setdefault(str(tm["user_id"]), []).append(tm["team_id"])
        out = []
        for m in self.store.list_members(ctx):
            uid = str(m["user_id"])
            info = known.get(uid, {})
            team_ids = sorted(by_user.get(uid, []), key=lambda t: teams[t].lower())
            out.append({"user_id": uid, "role": m["role"], "role_label": ROLE_LABELS.get(m["role"], m["role"]),
                        "email": info.get("email"), "name": info.get("name"),
                        "teams": [teams[t] for t in team_ids], "team_ids": team_ids,
                        "status": "active", "joined_at": m.get("created_at"),
                        "is_owner": m["role"] == "owner", "is_you": uid == ctx.user_id})
        order = {r: i for i, r in enumerate(_ALL)}
        return sorted(out, key=lambda m: (order.get(m["role"], 9), m["email"] or m["user_id"]))

    def _role_of(self, ctx: Ctx, user_id: str) -> Optional[str]:
        for m in self.store.list_members(ctx):
            if str(m["user_id"]) == user_id:
                return m["role"]
        return None

    def _admin_count(self, ctx: Ctx) -> int:
        return sum(1 for m in self.store.list_members(ctx) if m["role"] in _ADMIN)

    def change_role(self, ctx: Ctx, user_id: str, role: str) -> Dict[str, Any]:
        ctx.require_admin()
        user_id = str(uuid.UUID(str(user_id)))
        if role not in ASSIGNABLE_ROLES:
            raise ValidationError(f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
        current = self._role_of(ctx, user_id)
        if current is None:
            raise NotFoundError("not a member of this workspace")
        if current == "owner":
            raise ForbiddenError("the workspace owner's role cannot be changed")
        if current == role:
            return {"user_id": user_id, "role": role, "changed": False}
        if current in _ADMIN and role not in _ADMIN and self._admin_count(ctx) <= 1:
            raise ValidationError("a workspace needs at least one admin")
        self.store.add_member(ctx, user_id, role)
        audit(self.store, ctx, "user.role_changed", entity_type="workspace_members", entity_id=user_id[:40],
              summary=f"{ROLE_LABELS.get(current, current)} -> {ROLE_LABELS[role]}",
              changes={"user_id": user_id, "from": current, "to": role})
        return {"user_id": user_id, "role": role, "changed": True}

    def set_member_teams(self, ctx: Ctx, user_id: str, team_ids: Iterable[str]) -> Dict[str, Any]:
        """Make ``team_ids`` exactly the member's teams (joining and leaving as needed)."""
        ctx.require_manager()
        user_id = str(uuid.UUID(str(user_id)))
        if self._role_of(ctx, user_id) is None:
            raise NotFoundError("not a member of this workspace")
        wanted = list(dict.fromkeys(str(t) for t in team_ids))
        teams = {t["id"]: t["name"] for t in self.store.all(ctx, "teams", cap=500)}
        unknown = [t for t in wanted if t not in teams]
        if unknown:
            raise ValidationError("unknown team: " + ", ".join(unknown[:5]))
        current = {tm["team_id"]: tm for tm in self.store.all(ctx, "team_members", {"user_id": user_id}, cap=500)}
        for team_id, row in current.items():
            if team_id not in wanted:
                self.store.delete(ctx, "team_members", row["id"])
        for team_id in wanted:
            if team_id not in current:
                self.store.insert(ctx, "team_members", {"team_id": team_id, "user_id": user_id, "role": "member"})
        before = sorted(t for t in current if t in teams)
        if before != sorted(wanted):
            audit(self.store, ctx, "user.team_changed", entity_type="workspace_members", entity_id=user_id[:40],
                  summary=", ".join(teams[t] for t in wanted) or "no teams",
                  changes={"user_id": user_id, "from": before, "to": sorted(wanted)})
        return {"user_id": user_id, "team_ids": wanted, "teams": [teams[t] for t in wanted]}

    def remove_member(self, ctx: Ctx, user_id: str) -> Dict[str, Any]:
        ctx.require_admin()
        user_id = str(uuid.UUID(str(user_id)))
        current = self._role_of(ctx, user_id)
        if current is None:
            raise NotFoundError("not a member of this workspace")
        if current == "owner":
            raise ForbiddenError("the workspace owner cannot be removed")
        if current in _ADMIN and self._admin_count(ctx) <= 1:
            raise ValidationError("a workspace needs at least one admin")
        self.store.remove_member(ctx, user_id)
        for tm in self.store.all(ctx, "team_members", {"user_id": user_id}, cap=500):
            self.store.delete(ctx, "team_members", tm["id"])
        audit(self.store, ctx, "user.removed", entity_type="workspace_members", entity_id=user_id[:40],
              changes={"user_id": user_id, "role": current})
        return {"user_id": user_id, "removed": True}

    # --- invitations ---------------------------------------------------------------

    def _workspace_name(self, workspace_id: str) -> str:
        lookup = getattr(self.store, "system_membership", None)
        info = lookup(workspace_id) if callable(lookup) else None
        return str((info or {}).get("name") or "a SANA GTM workspace")

    def _team(self, ctx: Ctx, team_id: Optional[str]) -> Optional[Dict[str, Any]]:
        return self.store.find(ctx, "teams", team_id) if team_id else None

    def _issue(self, ctx: Ctx, row: Mapping[str, Any], app_url: Optional[str], *, send: bool,
               changes: Mapping[str, Any]) -> Dict[str, Any]:
        """Give ``row`` a fresh token (old links stop working), optionally email it, and
        return the invitation with the token and what happened to delivery."""
        token = new_invite_token(ctx.workspace_id)
        values: Dict[str, Any] = {**changes, "token_hash": hash_token(token)}
        delivery, email_sent = "link", False
        if not send:
            message = "A new invite link was created. Copy it and send it to the invitee; earlier links no longer work."
        elif not self.mailer.configured:
            message = ("Email delivery is not configured, so no email was sent. Copy the invite link and send it "
                       "to the invitee yourself.")
        elif not app_url:
            message = ("No email was sent: the app address is unknown (set CAREERCLOUD_PUBLIC_APP_URL). Copy the "
                       "invite link and send it to the invitee yourself.")
        else:
            outcome = self.mailer.send(row["email"], *_invite_email(
                workspace=self._workspace_name(ctx.workspace_id), inviter=ctx.actor_label,
                role=ROLE_LABELS[row["role"]], url=invite_url(app_url, token), expires_at=values.get(
                    "expires_at", row["expires_at"])))
            if outcome.get("event") == "sent":
                delivery, email_sent = "email", True
                message = f"Invitation emailed to {row['email']}."
                values.update({"sent_count": int(row.get("sent_count") or 0) + 1, "last_sent_at": utcnow()})
            else:
                delivery = "email_failed"
                log.warning("invitation email to workspace %s failed: %s", ctx.workspace_id,
                            str(outcome.get("detail") or "unknown error")[:200])
                message = "The invitation email could not be sent. Copy the invite link and send it yourself."
        if send:
            values["delivery"] = delivery
        updated = self.store.update(ctx, "workspace_invitations", row["id"], values)
        return {**self._public_invite(updated), "token": token, "invite_path": invite_path(token),
                "invite_url": invite_url(app_url, token) if app_url else None, "email_sent": email_sent,
                "delivery_message": message}

    def invite(self, ctx: Ctx, email: str, role: str = "member", *, first_name: Optional[str] = None,
               last_name: Optional[str] = None, team_id: Optional[str] = None, days: int = INVITE_DAYS,
               app_url: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_admin()
        address = normalize_email(email)
        if not address:
            raise ValidationError("a valid email address is required")
        if role not in ASSIGNABLE_ROLES:
            raise ValidationError(f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
        first, last = _name_part(first_name, "first name"), _name_part(last_name, "last name")
        team = None
        if team_id:
            team = self._team(ctx, str(team_id))
            if team is None:
                raise ValidationError("that team does not exist in this workspace")
        member_ids = {str(m["user_id"]) for m in self.store.list_members(ctx)}
        if any(info.get("email") == address for uid, info in self._known(ctx).items() if uid in member_ids):
            raise ConflictError(f"{address} is already a member of this workspace")
        now = utcnow()
        for pending in self.store.all(ctx, "workspace_invitations", {"email": address, "status": "pending"}, cap=50):
            if pending["expires_at"] >= now:
                raise ConflictError(f"an invitation for {address} is already pending; resend or revoke it instead")
            self.store.update(ctx, "workspace_invitations", pending["id"], {"status": "expired"})
        row = self.store.insert(ctx, "workspace_invitations", {
            "email": address, "role": role, "status": "pending", "first_name": first, "last_name": last,
            "team_id": team["id"] if team else None, "invited_by_label": ctx.actor_label,
            # a placeholder until _issue sets the real hash (never a usable token)
            "token_hash": hash_token(new_invite_token(ctx.workspace_id)),
            "expires_at": now + timedelta(days=max(1, min(int(days), 30)))})
        result = self._issue(ctx, row, app_url, send=True, changes={})
        audit(self.store, ctx, "user.invited", entity_type="workspace_invitations", entity_id=row["id"],
              summary=f"{address} invited as {ROLE_LABELS[role]}",
              changes={"email": address, "role": role, "team_id": row.get("team_id"), "delivery": result["delivery"]})
        return result

    def _pending_row(self, ctx: Ctx, invitation_id: str, *, allow_expired: bool) -> Dict[str, Any]:
        row = self.store.get(ctx, "workspace_invitations", invitation_id)
        status = _status(row, utcnow())
        if status == "pending" or (allow_expired and status == "expired"):
            return row
        raise ValidationError(f"this invitation is {status}")

    def resend_invitation(self, ctx: Ctx, invitation_id: str, *, days: int = INVITE_DAYS,
                          app_url: Optional[str] = None) -> Dict[str, Any]:
        """A new token and a fresh expiry for a pending or expired invitation; emailed when possible."""
        ctx.require_admin()
        row = self._pending_row(ctx, invitation_id, allow_expired=True)
        result = self._issue(ctx, row, app_url, send=True, changes={
            "status": "pending", "expires_at": utcnow() + timedelta(days=max(1, min(int(days), 30)))})
        audit(self.store, ctx, "invitation.resent", entity_type="workspace_invitations", entity_id=row["id"],
              summary=f"{row['email']}: {result['delivery']}", changes={"delivery": result["delivery"]})
        return result

    def invitation_link(self, ctx: Ctx, invitation_id: str) -> Dict[str, Any]:
        """A new copyable link for a pending invitation (tokens are stored hashed, so an old
        link cannot be shown again; issuing a new one retires it)."""
        ctx.require_admin()
        row = self._pending_row(ctx, invitation_id, allow_expired=False)
        result = self._issue(ctx, row, None, send=False, changes={})
        audit(self.store, ctx, "invitation.link_created", entity_type="workspace_invitations", entity_id=row["id"],
              summary=row["email"])
        return result

    @staticmethod
    def _public_invite(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in row.items() if k != "token_hash"}

    def invitations(self, ctx: Ctx) -> List[Dict[str, Any]]:
        ctx.require_admin()
        now = utcnow()
        teams = {t["id"]: t["name"] for t in self.store.all(ctx, "teams", cap=500)}
        out = []
        for row in self.store.all(ctx, "workspace_invitations", order="-created_at", cap=500):
            out.append({**self._public_invite(row), "status": _status(row, now),
                        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
                        "name": _full_name(row.get("first_name"), row.get("last_name")),
                        "team_name": teams.get(row.get("team_id") or "")})
        return out

    def revoke_invitation(self, ctx: Ctx, invitation_id: str) -> Dict[str, Any]:
        ctx.require_admin()
        row = self._pending_row(ctx, invitation_id, allow_expired=True)
        row = self.store.update(ctx, "workspace_invitations", invitation_id, {
            "status": "revoked", "revoked_at": utcnow(), "revoked_by": ctx.user_id,
            # the old hash could still match a leaked link; nothing can match this one
            "token_hash": hash_token(new_invite_token(ctx.workspace_id))})
        audit(self.store, ctx, "invitation.revoked", entity_type="workspace_invitations", entity_id=invitation_id,
              summary=row["email"])
        return self._public_invite(row)

    def _by_token(self, token: str) -> Tuple[Ctx, Dict[str, Any]]:
        """The invitation a token names. Every failure looks the same, so a guess learns nothing."""
        workspace_id, _, secret = str(token or "").partition(".")
        try:
            workspace_id = str(uuid.UUID(workspace_id))
        except ValueError:
            raise ValidationError(INVALID_INVITE) from None
        if len(secret) < 20 or len(token) > 200:
            raise ValidationError(INVALID_INVITE)
        system = Ctx.for_system(workspace_id)
        try:
            row = self.store.first(system, "workspace_invitations", {"token_hash": hash_token(token)})
        except NotFoundError:  # no such workspace
            row = None
        if row is None:
            raise ValidationError(INVALID_INVITE)
        return system, row

    def preview_invitation(self, token: str) -> Dict[str, Any]:
        """What the invite page shows before sign-in; only for someone holding the token."""
        system, row = self._by_token(token)
        team = self._team(system, row.get("team_id"))
        status = _status(row, utcnow())
        return {"status": status, "message": _STATUS_MESSAGES.get(status), "email": row["email"],
                "first_name": row.get("first_name"), "role": row["role"], "role_label": ROLE_LABELS[row["role"]],
                "team_name": team["name"] if team else None, "workspace_name": self._workspace_name(
                    system.workspace_id), "invited_by": row.get("invited_by_label"), "expires_at": row["expires_at"]}

    def accept_invitation(self, token: str, *, user_id: str, email: Optional[str]) -> Dict[str, Any]:
        """Called for a signed-in user who is not (yet) a member. No workspace ctx exists for them."""
        system, row = self._by_token(token)
        workspace_id = system.workspace_id
        status = _status(row, utcnow())
        if status == "expired" and row["status"] == "pending":
            self.store.update(system, "workspace_invitations", row["id"], {"status": "expired"})
        if status != "pending":
            raise ValidationError(_STATUS_MESSAGES[status])
        if normalize_email(email) != row["email"]:
            raise ForbiddenError(f"this invitation was sent to {row['email']}; sign in with that address to accept it")
        if self.store.membership(user_id, workspace_id) is not None:
            raise ConflictError("you are already a member of this workspace")
        # Claim the invitation first: of two concurrent accepts only one wins the version.
        try:
            self.store.update(system, "workspace_invitations", row["id"], {
                "status": "accepted", "accepted_at": utcnow(), "accepted_by": user_id,
                "token_hash": hash_token(new_invite_token(workspace_id))}, expected_version=row["version"])
        except ConflictError:
            raise ValidationError(_STATUS_MESSAGES["accepted"]) from None
        try:
            self.store.add_member(system, user_id, row["role"])
        except Exception:
            self.store.update(system, "workspace_invitations", row["id"], {
                "status": "pending", "accepted_at": None, "accepted_by": None, "token_hash": row["token_hash"]})
            raise
        team = self._team(system, row.get("team_id"))
        if team is not None and self.store.first(system, "team_members",
                                                 {"team_id": team["id"], "user_id": user_id}) is None:
            self.store.insert(system, "team_members", {"team_id": team["id"], "user_id": user_id, "role": "member"})
        member_ctx = Ctx(workspace_id, user_id, row["role"], actor_label=row["email"]).as_system("user")
        audit(self.store, member_ctx, "invitation.accepted", entity_type="workspace_invitations", entity_id=row["id"],
              summary=f"{row['email']} joined as {ROLE_LABELS[row['role']]}",
              changes={"role": row["role"], "team_id": team["id"] if team else None})
        return {"workspace_id": workspace_id, "workspace_name": self._workspace_name(workspace_id),
                "role": row["role"], "role_label": ROLE_LABELS[row["role"]], "team_name": team["name"] if team else None}

    # --- teams ---------------------------------------------------------------------

    def teams(self, ctx: Ctx) -> List[Dict[str, Any]]:
        members = self.store.all(ctx, "team_members", cap=5000)
        out = []
        for team in self.store.all(ctx, "teams", order="name", cap=500):
            out.append({**team, "members": [{"user_id": str(m["user_id"]), "role": m["role"], "id": m["id"]}
                                            for m in members if m["team_id"] == team["id"]]})
        return out

    def save_team(self, ctx: Ctx, values: Mapping[str, Any], team_id: Optional[str] = None) -> Dict[str, Any]:
        ctx.require_manager()
        clean = {k: values[k] for k in ("name", "description", "lead_user_id") if k in values}
        if "name" in clean:
            clean["name"] = str(clean["name"] or "").strip()
            if not clean["name"]:
                raise ValidationError("a team needs a name")
        if team_id:
            row = self.store.update(ctx, "teams", team_id, clean)
            audit(self.store, ctx, "team.update", entity_type="teams", entity_id=team_id, changes=clean)
        else:
            if not clean.get("name"):
                raise ValidationError("a team needs a name")
            row = self.store.insert(ctx, "teams", clean)
            audit(self.store, ctx, "team.create", entity_type="teams", entity_id=row["id"], summary=row["name"])
        return row

    def delete_team(self, ctx: Ctx, team_id: str) -> None:
        ctx.require_manager()
        self.store.get(ctx, "teams", team_id)
        for tm in self.store.all(ctx, "team_members", {"team_id": team_id}, cap=5000):
            self.store.delete(ctx, "team_members", tm["id"])
        self.store.delete(ctx, "teams", team_id)
        audit(self.store, ctx, "team.delete", entity_type="teams", entity_id=team_id)

    def add_team_member(self, ctx: Ctx, team_id: str, user_id: str, role: str = "member") -> Dict[str, Any]:
        ctx.require_manager()
        self.store.get(ctx, "teams", team_id)
        user_id = str(uuid.UUID(str(user_id)))
        if self._role_of(ctx, user_id) is None:
            raise ValidationError("only workspace members can join a team")
        existing = self.store.first(ctx, "team_members", {"team_id": team_id, "user_id": user_id})
        if existing is not None:
            return self.store.update(ctx, "team_members", existing["id"], {"role": role})
        row = self.store.insert(ctx, "team_members", {"team_id": team_id, "user_id": user_id, "role": role})
        audit(self.store, ctx, "team.member_add", entity_type="teams", entity_id=team_id,
              changes={"user_id": user_id, "role": role})
        return row

    def remove_team_member(self, ctx: Ctx, team_id: str, user_id: str) -> None:
        ctx.require_manager()
        row = self.store.first(ctx, "team_members", {"team_id": team_id, "user_id": str(uuid.UUID(str(user_id)))})
        if row is None:
            raise NotFoundError("not a member of this team")
        self.store.delete(ctx, "team_members", row["id"])
        audit(self.store, ctx, "team.member_remove", entity_type="teams", entity_id=team_id,
              changes={"user_id": str(user_id)})

    # --- ownership / assignment -------------------------------------------------------

    def assign(self, ctx: Ctx, entity: str, ids: Iterable[str], owner_id: Optional[str]) -> Dict[str, Any]:
        ctx.require_manager()
        if entity not in ASSIGNABLE:
            raise ValidationError(f"owners can be assigned on {', '.join(ASSIGNABLE)}")
        if owner_id:
            owner_id = str(uuid.UUID(str(owner_id)))
            if self._role_of(ctx, owner_id) is None:
                raise ValidationError("the owner must be a workspace member")
        ids = [str(i) for i in ids][:1000]
        updated, missing = 0, []
        for row_id in ids:
            if self.store.find(ctx, entity, row_id) is None:
                missing.append(row_id)
                continue
            self.store.update(ctx, entity, row_id, {"owner_id": owner_id})
            updated += 1
        audit(self.store, ctx, "admin.assign", entity_type=entity, summary=f"{updated} {entity} assigned",
              changes={"owner_id": owner_id, "count": updated})
        if owner_id and owner_id != ctx.user_id and updated:
            try:
                self.platform.service("notifications").notify(
                    ctx, title=f"{updated} {entity} assigned to you", kind="assignment", user_id=owner_id,
                    link=f"/{entity if entity != 'opportunities' else 'opportunities'}")
            except Exception:  # noqa: BLE001 - notifying is best effort
                pass
        return {"updated": updated, "missing": missing}

    def overview(self, ctx: Ctx) -> Dict[str, Any]:
        return {"you": {"user_id": ctx.user_id, "role": ctx.role, "role_label": ROLE_LABELS.get(ctx.role, ctx.role),
                        "permissions": permissions_for(ctx.role)},
                **permission_matrix()}
