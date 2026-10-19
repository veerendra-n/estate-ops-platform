import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

export interface Polled<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch `path` and re-fetch every `intervalMs` while the tab is visible (0 = once).
 * Changing `path` refetches immediately; stale responses for an old path are dropped.
 */
export function usePoll<T>(path: string | null, intervalMs = 3000): Polled<T> {
  const [state, setState] = useState<{ path: string | null; data?: T; error?: Error }>({ path });
  const [tick, setTick] = useState(0);
  const inflight = useRef(false);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    let timer: number | undefined;
    const ctrl = new AbortController();

    const run = async () => {
      if (inflight.current) return;
      inflight.current = true;
      try {
        const data = await api<T>(path, { signal: ctrl.signal });
        if (alive) setState({ path, data, error: undefined });
      } catch (e) {
        if (alive && !(e instanceof DOMException && e.name === "AbortError"))
          setState((s) => ({ path, data: s.path === path ? s.data : undefined, error: e as Error }));
      } finally {
        inflight.current = false;
      }
    };
    const schedule = () => {
      if (!intervalMs) return;
      window.clearInterval(timer);
      timer = window.setInterval(() => {
        if (!document.hidden) void run();
      }, intervalMs);
    };
    const onVisible = () => {
      if (!document.hidden) void run();
    };

    inflight.current = false;
    void run();
    schedule();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      ctrl.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [path, intervalMs, tick]);

  const current = state.path === path;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : undefined,
    loading: current ? state.data === undefined && state.error === undefined : true,
    reload,
  };
}

/** Current time, re-rendered every `ms` (drives SLA countdowns and "x ago" labels). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

/** A value persisted per viewer in localStorage (filters, theme). */
export function useStored<T extends string>(key: string, initial: T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      return (localStorage.getItem(key) as T | null) ?? initial;
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T) => {
      setValue(v);
      try {
        localStorage.setItem(key, v);
      } catch {
        /* ignore */
      }
    },
    [key],
  );
  return [value, set];
}
