import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { Loading } from "../components/Feedback";
import { useAuth } from "./AuthProvider";

export function RequireAuth({ children }: { children: ReactNode }) {
  const { ready, session } = useAuth();
  const location = useLocation();
  if (!ready) return <div className="page"><Loading label="Checking your session…" /></div>;
  if (!session) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return <>{children}</>;
}
