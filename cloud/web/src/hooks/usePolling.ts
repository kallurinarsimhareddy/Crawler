import { useCallback, useEffect, useRef, useState } from "react";

export interface Polled<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => void;
}

/**
 * Fetch now, then again every `intervalMs` while `active` is true.
 *
 * One request at a time: a slow API is not hit by a pile of overlapping polls,
 * and a response that arrives after the component unmounts or the key changes
 * is discarded. Polling pauses while the tab is hidden.
 */
export function usePolling<T>(
  load: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
  key: string,
  active: (data: T | null) => boolean = () => true,
): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  const loadRef = useRef(load);
  const activeRef = useRef(active);
  loadRef.current = load;
  activeRef.current = active;

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  // A new key is a different resource: forget the old one.
  useEffect(() => {
    setData(null);
    setError(null);
    setLoading(true);
  }, [key]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;

    const run = async () => {
      if (document.visibilityState === "hidden") {
        timer = window.setTimeout(run, intervalMs);
        return;
      }
      let latest: T | null = null;
      try {
        latest = await loadRef.current(controller.signal);
        if (controller.signal.aborted) return;
        setData(latest);
        setError(null);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err as Error);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
      if (!controller.signal.aborted && activeRef.current(latest)) {
        timer = window.setTimeout(run, intervalMs);
      }
    };

    void run();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [intervalMs, key, tick]);

  return { data, error, loading, refresh };
}
