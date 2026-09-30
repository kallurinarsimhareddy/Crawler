import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthProvider";
import { ErrorBanner } from "../components/Feedback";

export function Login() {
  const { mode, session, signIn, signUp, requestPasswordReset, configured } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const state = location.state as { from?: string; email?: string; create?: boolean } | null;
  const from = state?.from ?? "/";

  // Arriving from an invitation: the invited address, and "create an account" if asked.
  const [email, setEmail] = useState(state?.email ?? "");
  const [password, setPassword] = useState("");
  const [creating, setCreating] = useState(!!state?.create);
  const [forgot, setForgot] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (session) return <Navigate to={from} replace />;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "supabase" && forgot) {
        await requestPasswordReset(email.trim());
        setNotice("If an account exists for that email, a reset link is on its way. Open it on this device.");
      } else if (mode === "supabase" && creating) {
        const message = await signUp(email.trim(), password);
        if (message) setNotice(message);
        else navigate(from, { replace: true });
      } else {
        await signIn(email.trim(), password);
        navigate(from, { replace: true });
      }
    } catch (err) {
      setError(err as Error);
    } finally {
      setBusy(false);
    }
  }

  const dev = mode === "dev";

  return (
    <div className="auth-page">
      {import.meta.env.VITE_DEPLOY_ENV === "staging" && (
        <div className="env-banner env-banner--fixed" role="note">
          STAGING — test environment
        </div>
      )}
      <form className="card auth-card" onSubmit={onSubmit}>
        <div className="brand brand--center">
          <img src="/favicon.svg" alt="" width={32} height={32} />
          <span>SANA GTM</span>
        </div>
        <div>
          <h1 className="auth-title">{dev ? "Local development sign-in" : forgot ? "Reset your password" : creating ? "Create your account" : "Sign in"}</h1>
          <p className="muted small">
            {dev
              ? "Development mode: enter any email. Tokens are issued by your local API and never work in production."
              : forgot
                ? "Enter your account email and we'll send you a link to choose a new password."
                : "Use the email and password for your SANA GTM account."}
          </p>
        </div>

        {!configured && (
          <p className="alert alert--error" role="alert">
            Sign-in is not configured. Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY, or VITE_AUTH_MODE=dev for local development.
          </p>
        )}

        <div className="field">
          <label className="field__label" htmlFor="email">Email</label>
          <input id="email" className="input" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>

        {!dev && !forgot && (
          <div className="field">
            <label className="field__label" htmlFor="password">Password</label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete={creating ? "new-password" : "current-password"}
              required
              minLength={creating ? 8 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
        )}

        {error && <ErrorBanner error={error} />}
        {notice && <p className="alert alert--info">{notice}</p>}

        <button type="submit" className="button button--primary button--large" disabled={busy || !configured || !email}>
          {busy ? "Please wait…" : dev ? "Continue" : forgot ? "Send reset link" : creating ? "Create account" : "Sign in"}
        </button>

        {!dev && configured && !forgot && (
          <button type="button" className="link link-button" onClick={() => setCreating((value) => !value)}>
            {creating ? "Already have an account? Sign in" : "New here? Create an account"}
          </button>
        )}
        {!dev && configured && !creating && (
          <button
            type="button"
            className="link link-button"
            onClick={() => {
              setForgot((value) => !value);
              setError(null);
              setNotice(null);
            }}
          >
            {forgot ? "Back to sign in" : "Forgot your password?"}
          </button>
        )}
      </form>
    </div>
  );
}
