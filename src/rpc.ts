// Bridge to the Python worker. Every backend feature is reached through the
// single `rpc` callable in main.py; worker events arrive as "wfh_event".
import { addEventListener, callable, removeEventListener } from "@decky/api";
import { useEffect, useRef, useState } from "react";

const rpcCallable = callable<[method: string, params: Record<string, unknown>], unknown>("rpc");

export async function call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  return (await rpcCallable(method, params)) as T;
}

type Handler = (data: any) => void;
const handlers = new Map<string, Set<Handler>>();
let registered: ((name: string, data: unknown) => void) | null = null;

export function startEvents(): void {
  if (registered) return;
  registered = addEventListener<[name: string, data: unknown]>("wfh_event", (name, data) => {
    handlers.get(name)?.forEach((h) => {
      try {
        h(data);
      } catch (e) {
        console.error("[WFHelper] event handler failed", name, e);
      }
    });
  });
}

export function stopEvents(): void {
  if (registered) removeEventListener("wfh_event", registered);
  registered = null;
  handlers.clear();
}

export function onEvent(name: string, fn: Handler): () => void {
  if (!handlers.has(name)) handlers.set(name, new Set());
  handlers.get(name)!.add(fn);
  return () => handlers.get(name)?.delete(fn);
}

export function useEvent(name: string, fn: Handler): void {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => onEvent(name, (d) => ref.current(d)), [name]);
}

export interface RpcState<T> {
  data: T | undefined;
  error: string | undefined;
  loading: boolean;
  reload: () => void;
}

// Calls `method` on mount (and when deps change); refreshes every `pollMs` if set.
export function useRpc<T>(method: string, params: Record<string, unknown> = {}, deps: unknown[] = [], pollMs?: number): RpcState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    call<T>(method, params)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setError(undefined);
      })
      .catch((e) => alive && setError(String(e?.message ?? e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [method, JSON.stringify(params), tick, ...deps]);
  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(() => setTick((n) => n + 1), pollMs);
    return () => clearInterval(t);
  }, [pollMs]);
  return { data, error, loading, reload: () => setTick((n) => n + 1) };
}
