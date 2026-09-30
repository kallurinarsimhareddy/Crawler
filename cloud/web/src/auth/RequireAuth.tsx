import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { Loading } from "../components/Feedback";
import { pendingInviteFor } from "../platform/logic/invitations";
import { useAuth } from "./AuthProvider";

export function RequireAuth({ children }: { children: ReactNode }) {
  const { ready, session } = useAuth();
  const location = useLocation();
  if (!ready) return <div className="page"><Loading label="Checking your session…" /></div>;
  if (!session) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  // Signed in as the invited address with its invitation still waiting (e.g. after confirming
  // a new account): finish it first. Any other account is left alone.
  if (pendingInviteFor(session.email)) return <Navigate to="/invite" replace />;
  return <>{children}</>;
}
