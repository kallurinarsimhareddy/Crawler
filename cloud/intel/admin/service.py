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

Invitations: the token is shown exactly once (on create) and stored only as a
SHA-256 hash. It embeds the workspace id so the invitee — not yet a member,
so RLS hides the workspace from them — can be matched without scanning other
workspaces. Accepting requires the invitee's signed-in email to match.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

from cloud.intel.core.audit import audit
from cloud.intel.core.context import ConflictError, Ctx, ForbiddenError, NotFoundError, ValidationError, utcnow
from cloud.intel.core.normalize import normalize_email

__all__ = ["AdminService", "ROLE_LABELS", "PERMISSIONS", "permissions_for", "hash_token"]

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


class AdminService:
    def __init__(self, platform: Any) -> None:
        self.platform = platform
        self.store = platform.store

    # --- members -------------------------------------------------------------------

    def _labels(self, ctx: Ctx) -> Dict[str, str]:
        """user_id -> email, learned from accepted invitations and signed-in audit rows."""
        labels: Dict[str, str] = {}
        for row in self.store.all(ctx, "audit_log", {"action__in": ["session.login", "invitation.accept"]},
                                  order="created_at", cap=2000):
            if row.get("actor_id") and row.get("actor_label"):
                labels[str(row["actor_id"])] = row["actor_label"]
        for inv in self.store.all(ctx, "workspace_invitations", {"status": "accepted"}, cap=2000):
            if inv.get("accepted_by"):
                labels.setdefault(str(inv["accepted_by"]), inv["email"])
        return labels

    def members(self, ctx: Ctx) -> List[Dict[str, Any]]:
        labels = self._labels(ctx)
        teams = {t["id"]: t["name"] for t in self.store.all(ctx, "teams", cap=500)}
        by_user: Dict[str, List[str]] = {}
        for tm in self.store.all(ctx, "team_members", cap=5000):
            if tm["team_id"] in teams:
                by_user.setdefault(str(tm["user_id"]), []).append(teams[tm["team_id"]])
        out = []
        for m in self.store.list_members(ctx):
            uid = str(m["user_id"])
            out.append({"user_id": uid, "role": m["role"], "role_label": ROLE_LABELS.get(m["role"], m["role"]),
                        "email": labels.get(uid), "teams": sorted(by_user.get(uid, [])),
                        "is_you": uid == ctx.user_id})
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
        audit(self.store, ctx, "admin.role_change", entity_type="workspace_members", entity_id=user_id[:40],
              summary=f"{ROLE_LABELS.get(current, current)} -> {ROLE_LABELS[role]}",
              changes={"user_id": user_id, "from": current, "to": role})
        return {"user_id": user_id, "role": role, "changed": True}

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
        audit(self.store, ctx, "admin.member_remove", entity_type="workspace_members", entity_id=user_id[:40],
              changes={"user_id": user_id, "role": current})
        return {"user_id": user_id, "removed": True}

    # --- invitations ---------------------------------------------------------------

    def invite(self, ctx: Ctx, email: str, role: str = "member", *, days: int = INVITE_DAYS) -> Dict[str, Any]:
        ctx.require_admin()
        address = normalize_email(email)
        if not address:
            raise ValidationError("a valid email address is required")
        if role not in ASSIGNABLE_ROLES:
            raise ValidationError(f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
        for pending in self.store.all(ctx, "workspace_invitations", {"email": address, "status": "pending"}, cap=50):
            self.store.update(ctx, "workspace_invitations", pending["id"], {"status": "revoked"})
        token = f"{ctx.workspace_id}.{secrets.token_urlsafe(24)}"
        row = self.store.insert(ctx, "workspace_invitations", {
            "email": address, "role": role, "status": "pending", "token_hash": hash_token(token),
            "expires_at": utcnow() + timedelta(days=max(1, min(int(days), 30)))})
        audit(self.store, ctx, "invitation.create", entity_type="workspace_invitations", entity_id=row["id"],
              summary=f"{address} invited as {ROLE_LABELS[role]}", changes={"email": address, "role": role})
        return {**self._public_invite(row), "token": token,
                "note": "Share this token with the invitee once; it is not stored and cannot be shown again."}

    @staticmethod
    def _public_invite(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in row.items() if k != "token_hash"}

    def invitations(self, ctx: Ctx) -> List[Dict[str, Any]]:
        now = utcnow()
        out = []
        for row in self.store.all(ctx, "workspace_invitations", order="-created_at", cap=500):
            if row["status"] == "pending" and row["expires_at"] < now:
                row = {**row, "status": "expired"}
            out.append(self._public_invite(row))
        return out

    def revoke_invitation(self, ctx: Ctx, invitation_id: str) -> Dict[str, Any]:
        ctx.require_admin()
        row = self.store.get(ctx, "workspace_invitations", invitation_id)
        if row["status"] != "pending":
            raise ValidationError(f"invitation is {row['status']}")
        row = self.store.update(ctx, "workspace_invitations", invitation_id, {"status": "revoked"})
        audit(self.store, ctx, "invitation.revoke", entity_type="workspace_invitations", entity_id=invitation_id)
        return self._public_invite(row)

    def accept_invitation(self, token: str, *, user_id: str, email: Optional[str]) -> Dict[str, Any]:
        """Called for a signed-in user who is not (yet) a member. No workspace ctx exists for them."""
        workspace_id, _, secret = str(token or "").partition(".")
        try:
            workspace_id = str(uuid.UUID(workspace_id))
        except ValueError:
            raise ValidationError("invalid invitation") from None
        if not secret:
            raise ValidationError("invalid invitation")
        system = Ctx.for_system(workspace_id)
        try:
            row = self.store.first(system, "workspace_invitations", {"token_hash": hash_token(token)})
        except NotFoundError:
            row = None
        if row is None or row["status"] != "pending":
            raise ValidationError("this invitation is not valid")
        if row["expires_at"] < utcnow():
            self.store.update(system, "workspace_invitations", row["id"], {"status": "expired"})
            raise ValidationError("this invitation has expired")
        if normalize_email(email) != row["email"]:
            raise ForbiddenError("this invitation was sent to a different email address")
        if self.store.membership(user_id, workspace_id) is not None:
            raise ConflictError("you are already a member of this workspace")
        self.store.add_member(system, user_id, row["role"])
        self.store.update(system, "workspace_invitations", row["id"], {
            "status": "accepted", "accepted_at": utcnow(), "accepted_by": user_id})
        member_ctx = Ctx(workspace_id, user_id, row["role"], actor_label=row["email"]).as_system("user")
        audit(self.store, member_ctx, "invitation.accept", entity_type="workspace_invitations", entity_id=row["id"],
              summary=f"{row['email']} joined as {ROLE_LABELS[row['role']]}")
        return {"workspace_id": workspace_id, "role": row["role"]}

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
