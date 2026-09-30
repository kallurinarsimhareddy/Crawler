// /invite#<token> — the page an invitation link opens. Public: it shows the
// invitation before sign-in, then accepts it automatically once the invitee is
// signed in with the invited address. The token is read from the fragment (never
// sent to a server by the browser), kept in this browser only until it is used,
// and removed from the address bar straight away.

import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { request } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import { ErrorBanner, Loading } from "../components/Feedback";
import { clearPendingInvite, pendingInvite, sameEmail, savePendingInvite, tokenFromLocation } from "../platform/logic/invitations";
import { fmtDate } from "../platform/ui";
import { rememberWorkspace } from "../platform/workspace";
import "../platform/styles/invite.css";

interface Preview {
  status: "pending" | "accepted" | "expired" | "revoked";
  message: string | null;
  email: string;
  first_name: string | null;
  role_label: string;
  team_name: string | null;
  workspace_name: string;
  invited_by: string | null;
  expires_at: string;
}

interface Accepted {
  workspace_id: string;
  workspace_name: string;
  role_label: string;
}

function initialToken(): string | null {
  const fromUrl = tokenFromLocation(window.location.pathname, window.location.hash);
  if (fromUrl) {
    savePendingInvite(fromUrl);
    // Keep the secret out of the address bar, history and screenshots.
    window.history.replaceState(window.history.state, "", "/invite");
    return fromUrl;
  }
  return pendingInvite();
}

export function Invite() {
  const { ready, session, signOut } = useAuth();
  const navigate = useNavigate();
  const [token] = useState(initialToken);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [acceptError, setAcceptError] = useState<Error | null>(null);
  const [joined, setJoined] = useState<Accepted | null>(null);
  const tried = useRef(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    request<Preview>("/api/v1/invitations/preview", { method: "POST", body: JSON.stringify({ token }) }, false)
      .then((result) => {
        if (cancelled) return;
        setPreview(result);
        // Remember whom it is for, so only that account is sent back here after signing in.
        if (result.status === "pending") savePendingInvite(token, result.email);
        else clearPendingInvite();
      })
      .catch((error: Error) => {
        if (cancelled) return;
        setLoadError(error);
        clearPendingInvite();
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const matches = !!session && !!preview && sameEmail(session.email, preview.email);

  const accept = async () => {
    if (!token) return;
    setAccepting(true);
    setAcceptError(null);
    try {
      const result = await request<Accepted>("/api/v1/invitations/accept", { method: "POST", body: JSON.stringify({ token }) });
      clearPendingInvite();
      rememberWorkspace(result.workspace_id);
      setJoined(result);
      window.setTimeout(() => navigate("/", { replace: true }), 1500);
    } catch (error) {
      setAcceptError(error as Error);
      if ((error as { status?: number }).status === 422) clearPendingInvite();
    } finally {
      setAccepting(false);
    }
  };

  // Signed in as the invited address: accept without another click.
  useEffect(() => {
    if (ready && matches && preview?.status === "pending" && !tried.current) {
      tried.current = true;
      void accept();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, matches, preview?.status]);

  const toLogin = (create: boolean) => {
    if (token) savePendingInvite(token, preview?.email ?? null);
    navigate("/login", { state: { from: "/invite", email: preview?.email, create } });
  };

  let body;
  if (!token) {
    body = (
      <>
        <h1 className="auth-title">Invitation link needed</h1>
        <p className="muted small">Open the invitation link you were sent. If it no longer works, ask a workspace admin to resend it.</p>
        <Link className="button button--primary" to="/">Go to SANA GTM</Link>
      </>
    );
  } else if (loadError) {
    body = (
      <>
        <h1 className="auth-title">This invitation cannot be used</h1>
        <ErrorBanner error={loadError} />
        <Link className="button button--ghost" to="/">Go to SANA GTM</Link>
      </>
    );
  } else if (!preview || !ready) {
    body = <Loading label="Checking your invitation…" />;
  } else if (joined) {
    body = (
      <>
        <h1 className="auth-title">You're in</h1>
        <p role="status">You joined <strong>{joined.workspace_name}</strong> as {joined.role_label}. Opening SANA GTM…</p>
        <Link className="button button--primary" to="/" replace>Open SANA GTM</Link>
      </>
    );
  } else {
    body = (
      <>
        <h1 className="auth-title">Join {preview.workspace_name}</h1>
        {preview.status !== "pending" ? (
          <p className="alert alert--error" role="alert">{preview.message}</p>
        ) : (
          <p className="muted small">
            {preview.invited_by ? `${preview.invited_by} invited you` : "You're invited"} to join this SANA GTM workspace.
          </p>
        )}
        <dl className="invite-facts">
          <dt>Workspace</dt>
          <dd>{preview.workspace_name}</dd>
          <dt>Invited email</dt>
          <dd className="invite-facts__email">{preview.email}</dd>
          <dt>Role</dt>
          <dd>{preview.role_label}</dd>
          {preview.team_name && (
            <>
              <dt>Team</dt>
              <dd>{preview.team_name}</dd>
            </>
          )}
          {preview.status === "pending" && (
            <>
              <dt>Expires</dt>
              <dd>{fmtDate(preview.expires_at)}</dd>
            </>
          )}
        </dl>
        {preview.status === "pending" && !session && (
          <div className="invite-actions">
            <p className="muted small">Sign in, or create an account, with <strong>{preview.email}</strong>. The invitation is accepted as soon as you are signed in.</p>
            <button type="button" className="button button--primary button--large" onClick={() => toLogin(false)}>Sign in to accept</button>
            <button type="button" className="button button--ghost" onClick={() => toLogin(true)}>Create an account</button>
          </div>
        )}
        {preview.status === "pending" && session && !matches && (
          <div className="invite-actions">
            <p className="alert alert--error" role="alert">
              You're signed in as {session.email ?? "another account"}, but this invitation is for {preview.email}. Sign out, then sign in as {preview.email}.
            </p>
            <button type="button" className="button button--primary" onClick={() => void signOut()}>Sign out</button>
            <button
              type="button"
              className="button button--ghost"
              onClick={() => {
                clearPendingInvite();
                navigate("/", { replace: true });
              }}
            >
              Not now
            </button>
          </div>
        )}
        {preview.status === "pending" && matches && (
          <div className="invite-actions">
            {accepting && <Loading label="Joining the workspace…" />}
            {acceptError && <ErrorBanner error={acceptError} />}
            {acceptError && <button type="button" className="button button--primary" disabled={accepting} onClick={() => void accept()}>Try again</button>}
          </div>
        )}
        {preview.status !== "pending" && <Link className="button button--ghost" to="/">Go to SANA GTM</Link>}
      </>
    );
  }

  return (
    <div className="auth-page">
      <div className="card auth-card invite-card">
        <div className="brand brand--center">
          <img src="/favicon.svg" alt="" width={32} height={32} />
          <span>SANA GTM</span>
        </div>
        {body}
      </div>
    </div>
  );
}
