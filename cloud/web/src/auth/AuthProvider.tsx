import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { SupabaseClient } from "@supabase/supabase-js";
import { api, configureAuth } from "../api/client";

export type AuthMode = "supabase" | "dev";

export interface Session {
  email: string | null;
  userId: string;
}

interface AuthState {
  mode: AuthMode;
  ready: boolean;
  session: Session | null;
  configured: boolean;
  signIn: (email: string, password?: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<string>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

const MODE: AuthMode = import.meta.env.VITE_AUTH_MODE === "dev" ? "dev" : "supabase";
const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL ?? "";
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY ?? "";

// Development tokens live in sessionStorage: gone when the tab closes, never
// shared across tabs, and only ever issued by a development API.
const DEV_KEY = "careercloud.dev-session";

interface DevStored {
  token: string;
  email: string;
  userId: string;
  expiresAt: number;
}

function readDev(): DevStored | null {
  try {
    const raw = sessionStorage.getItem(DEV_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as DevStored;
    return stored.expiresAt > Date.now() + 30_000 ? stored : null;
  } catch {
    return null;
  }
}

function writeDev(value: DevStored | null): void {
  try {
    if (value) sessionStorage.setItem(DEV_KEY, JSON.stringify(value));
    else sessionStorage.removeItem(DEV_KEY);
  } catch {
    // storage unavailable: the session simply lasts as long as the page
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  const [session, setSession] = useState<Session | null>(null);
  const supabase = useRef<SupabaseClient | null>(null);
  const devSession = useRef<DevStored | null>(null);
  const configured = MODE === "dev" || (SUPABASE_URL !== "" && SUPABASE_ANON_KEY !== "");

  const signOut = useCallback(async () => {
    if (MODE === "dev") {
      devSession.current = null;
      writeDev(null);
    } else if (supabase.current) {
      await supabase.current.auth.signOut();
    }
    setSession(null);
  }, []);

  useEffect(() => {
    let unsubscribe = () => {};
    configureAuth(
      async () => {
        if (MODE === "dev") return devSession.current?.token ?? null;
        const { data } = (await supabase.current?.auth.getSession()) ?? { data: { session: null } };
        return data.session?.access_token ?? null;
      },
      () => {
        void signOut();
      },
    );

    if (MODE === "dev") {
      const stored = readDev();
      devSession.current = stored;
      setSession(stored ? { email: stored.email, userId: stored.userId } : null);
      setReady(true);
      return;
    }
    if (!configured) {
      setReady(true);
      return;
    }
    void import("@supabase/supabase-js").then(({ createClient }) => {
      const client = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
        auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
      });
      supabase.current = client;
      void client.auth.getSession().then(({ data }) => {
        const user = data.session?.user;
        setSession(user ? { email: user.email ?? null, userId: user.id } : null);
        setReady(true);
      });
      const { data } = client.auth.onAuthStateChange((_event, next) => {
        const user = next?.user;
        setSession(user ? { email: user.email ?? null, userId: user.id } : null);
      });
      unsubscribe = () => data.subscription.unsubscribe();
    });
    return () => unsubscribe();
  }, [configured, signOut]);

  const signIn = useCallback(async (email: string, password?: string) => {
    if (MODE === "dev") {
      const issued = await api.devSession(email);
      const stored: DevStored = {
        token: issued.access_token,
        email: issued.email,
        userId: issued.user_id,
        expiresAt: Date.now() + issued.expires_in * 1000,
      };
      devSession.current = stored;
      writeDev(stored);
      setSession({ email: stored.email, userId: stored.userId });
      return;
    }
    if (!supabase.current) throw new Error("Sign-in is not configured.");
    const { error } = await supabase.current.auth.signInWithPassword({ email, password: password ?? "" });
    if (error) throw new Error(error.message);
  }, []);

  const signUp = useCallback(async (email: string, password: string) => {
    if (!supabase.current) throw new Error("Sign-up is not configured.");
    const { data, error } = await supabase.current.auth.signUp({ email, password });
    if (error) throw new Error(error.message);
    return data.session ? "" : "Check your email to confirm your account, then sign in.";
  }, []);

  const value = useMemo<AuthState>(
    () => ({ mode: MODE, ready, session, configured, signIn, signUp, signOut }),
    [ready, session, configured, signIn, signUp, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
