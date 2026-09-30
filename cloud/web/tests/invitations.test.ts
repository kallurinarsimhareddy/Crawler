import { test } from "node:test";
import assert from "node:assert/strict";
import {
  EMPTY_INVITE,
  clearPendingInvite,
  extractToken,
  fullName,
  invitationActions,
  inviteBody,
  inviteLink,
  isInviteToken,
  memberLabel,
  pendingInvite,
  pendingInviteFor,
  sameEmail,
  savePendingInvite,
  statusLabel,
  tokenFromLocation,
  validateInvite,
} from "../src/platform/logic/invitations.ts";

const WS = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b";
const TOKEN = `${WS}.${"Ab_9-".repeat(9)}`;
const ROLES = ["admin", "manager", "member", "viewer"];

test("invite tokens: shape", () => {
  assert.equal(isInviteToken(TOKEN), true);
  assert.equal(isInviteToken(`${WS}.short`), false);
  assert.equal(isInviteToken(`not-a-uuid.${"x".repeat(43)}`), false);
  assert.equal(isInviteToken(`${WS}.${"x".repeat(43)}<script>`), false);
  assert.equal(isInviteToken(`${TOKEN}${"x".repeat(200)}`), false);
  assert.equal(isInviteToken(null), false);
});

test("the invite link carries the token in the fragment, never the path or query", () => {
  const link = inviteLink("https://sanagtm.pages.dev/", TOKEN);
  assert.equal(link, `https://sanagtm.pages.dev/invite#${TOKEN}`);
  const url = new URL(link);
  assert.equal(url.pathname, "/invite");
  assert.equal(url.search, "");
  assert.equal(url.hash, `#${TOKEN}`);
});

test("the invite page reads the token from the fragment or the older path form", () => {
  assert.equal(tokenFromLocation("/invite", `#${TOKEN}`), TOKEN);
  assert.equal(tokenFromLocation(`/invite/${TOKEN}`, ""), TOKEN);
  assert.equal(tokenFromLocation("/invite", ""), null);
  assert.equal(tokenFromLocation("/invite", "#garbage"), null);
  assert.equal(tokenFromLocation("/elsewhere/x", ""), null);
});

test("Join accepts a pasted link or a bare code", () => {
  assert.equal(extractToken(TOKEN), TOKEN);
  assert.equal(extractToken(`  https://sanagtm.pages.dev/invite#${TOKEN}  `), TOKEN);
  assert.equal(extractToken(`https://sanagtm.pages.dev/invite/${TOKEN}`), TOKEN);
  assert.equal(extractToken("hello"), null);
  assert.equal(extractToken(""), null);
});

test("invite form validation", () => {
  const ok = { ...EMPTY_INVITE, email: "ann@example.com" };
  assert.equal(validateInvite(ok, ROLES), null);
  assert.match(validateInvite({ ...ok, email: "" }, ROLES) ?? "", /Enter/);
  assert.match(validateInvite({ ...ok, email: "ann@example" }, ROLES) ?? "", /look right/);
  assert.match(validateInvite({ ...ok, role: "owner" }, ROLES) ?? "", /role/);
  assert.match(validateInvite({ ...ok, first_name: "x".repeat(101) }, ROLES) ?? "", /100/);
  assert.equal(EMPTY_INVITE.role, "member");
});

test("invite request body: trimmed, lower-cased, blanks dropped", () => {
  assert.deepEqual(inviteBody({ email: " Ann@Example.COM ", first_name: " Ann ", last_name: "", role: "viewer", team_id: "" }),
    { email: "ann@example.com", role: "viewer", first_name: "Ann" });
  assert.deepEqual(inviteBody({ email: "b@example.com", first_name: "", last_name: "Lee", role: "admin", team_id: "tm_1" }),
    { email: "b@example.com", role: "admin", last_name: "Lee", team_id: "tm_1" });
});

test("invitation status labels and row actions", () => {
  assert.equal(statusLabel("pending"), "Pending");
  assert.equal(statusLabel("revoked"), "Revoked");
  assert.equal(statusLabel("weird"), "weird");
  assert.deepEqual(invitationActions("pending"), { resend: true, revoke: true, copy: true });
  assert.deepEqual(invitationActions("expired"), { resend: true, revoke: true, copy: false });
  assert.deepEqual(invitationActions("accepted"), { resend: false, revoke: false, copy: false });
  assert.deepEqual(invitationActions("revoked"), { resend: false, revoke: false, copy: false });
});

test("names and emails", () => {
  assert.equal(fullName("Ann", "Lee"), "Ann Lee");
  assert.equal(fullName(null, "Lee"), "Lee");
  assert.equal(fullName("", undefined), "");
  const id = "12345678-aaaa-bbbb-cccc-1234567890ab";
  assert.equal(memberLabel({ name: "Ann Lee", email: "a@x.com", user_id: id }), "Ann Lee");
  assert.equal(memberLabel({ name: null, email: "a@x.com", user_id: id }), "a@x.com");
  assert.equal(memberLabel({ user_id: id }), "12345678…");
  assert.equal(sameEmail("Ann@Example.com ", "ann@example.com"), true);
  assert.equal(sameEmail("ann@example.com", "bob@example.com"), false);
  assert.equal(sameEmail(null, "ann@example.com"), false);
});

test("the pending invitation survives sign-in and is removed once used", () => {
  const data = new Map<string, string>();
  const storage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
  assert.equal(pendingInvite(storage), null);
  savePendingInvite("not a token", null, storage);
  assert.equal(pendingInvite(storage), null);
  savePendingInvite(TOKEN, null, storage);
  assert.equal(pendingInvite(storage), TOKEN);
  // before the invite page has looked the email up, nobody is redirected back to it
  assert.equal(pendingInviteFor("nia@example.com", storage), null);
  savePendingInvite(TOKEN, "Nia@Example.com", storage);
  assert.equal(pendingInviteFor("nia@example.com", storage), TOKEN);
  // another signed-in account (e.g. the admin testing the link) is never trapped on /invite
  assert.equal(pendingInviteFor("owner@example.com", storage), null);
  assert.equal(pendingInviteFor(null, storage), null);
  clearPendingInvite(storage);
  assert.equal(pendingInvite(storage), null);
  data.set("sana.pending-invite", "{not json");
  assert.equal(pendingInvite(storage), null);
  data.set("sana.pending-invite", TOKEN); // the earlier plain-token format is ignored, not trusted
  assert.equal(pendingInvite(storage), null);
  const broken = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => { throw new Error("blocked"); } };
  assert.doesNotThrow(() => savePendingInvite(TOKEN, "a@b.co", broken));
  assert.equal(pendingInvite(broken), null);
  assert.equal(pendingInviteFor("a@b.co", broken), null);
  assert.doesNotThrow(() => clearPendingInvite(broken));
  assert.equal(pendingInvite(null), null);
});
