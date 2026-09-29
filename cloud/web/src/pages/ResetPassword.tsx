import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { ErrorBanner } from "../components/Feedback";

/** Landing page of the emailed reset link: Supabase turns the link into a session, then the user picks a new password. */
export function ResetPassword() {
  const { ready, session, updatePassword } = useAuth();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError(new Error("The two passwords do not match."));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await updatePassword(password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err as Error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <form className="card auth-card" onSubmit={onSubmit}>
        <div className="brand brand--center">
          <img src="/favicon.svg" alt="" width={32} height={32} />
          <span>SANA GTM</span>
        </div>
        <h1 className="auth-title">Choose a new password</h1>

        {!ready && <p className="muted small">Checking your reset link…</p>}
        {ready && !session && (
          <p className="alert alert--error" role="alert">
            This reset link is invalid or has expired. <Link to="/login">Request a new one</Link>.
          </p>
        )}
        {ready && session && (
          <>
            <p className="muted small">Setting a new password for {session.email ?? "your account"}.</p>
            <div className="field">
              <label className="field__label" htmlFor="new-password">New password</label>
              <input id="new-password" className="input" type="password" autoComplete="new-password" required minLength={8}
                value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="confirm-password">Confirm new password</label>
              <input id="confirm-password" className="input" type="password" autoComplete="new-password" required minLength={8}
                value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </div>
            {error && <ErrorBanner error={error} />}
            <button type="submit" className="button button--primary button--large" disabled={busy || !password}>
              {busy ? "Please wait…" : "Update password"}
            </button>
          </>
        )}
      </form>
    </div>
  );
}
