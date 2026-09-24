import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { platform, ws, type Workspace, type WsClient } from "./api";

interface WorkspaceState {
  workspaces: Workspace[];
  current: Workspace | null;
  client: WsClient | null;
  loading: boolean;
  error: Error | null;
  select: (id: string) => void;
  create: (name: string) => Promise<void>;
  reload: () => void;
}

const Ctx = createContext<WorkspaceState | null>(null);
const STORAGE_KEY = "careercrawler.workspace";

function remembered(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function remember(id: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // private window or blocked storage: the selection just is not remembered
  }
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState<string | null>(remembered());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    platform
      .workspaces()
      .then((result) => {
        if (cancelled) return;
        setWorkspaces(result.items);
        setError(null);
      })
      .catch((err: Error) => !cancelled && setError(err))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [tick]);

  const current = useMemo(
    () => workspaces.find((w) => w.id === selected) ?? workspaces[0] ?? null,
    [workspaces, selected],
  );

  const select = useCallback((id: string) => {
    setSelected(id);
    remember(id);
  }, []);

  const create = useCallback(
    async (name: string) => {
      const created = await platform.createWorkspace(name);
      select(created.id);
      setTick((n) => n + 1);
    },
    [select],
  );

  const value = useMemo<WorkspaceState>(
    () => ({
      workspaces,
      current,
      client: current ? ws(current.id) : null,
      loading,
      error,
      select,
      create,
      reload: () => setTick((n) => n + 1),
    }),
    [workspaces, current, loading, error, select, create],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceState {
  const value = useContext(Ctx);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}

/** The bound client; pages under RequireWorkspace can rely on it. */
export function useWs(): WsClient {
  const { client } = useWorkspace();
  if (!client) throw new Error("no workspace selected");
  return client;
}
