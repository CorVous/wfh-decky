// Dev-only stand-in for @decky/api used by the page preview harness (never packaged).
//
// callable("rpc")(method, params) POSTs to preview/bridge.py, which runs the real Python
// worker in-process. Worker events (rpc.emit) are polled from /events and delivered to
// addEventListener("wfh_event", ...) listeners, so useEvent() refreshes work too.
// window.__wfh keeps counters the screenshot runner waits on and the RPC errors it reports.

type Listener = (...args: any[]) => void;

interface WfhDebug {
  pending: number;
  calls: number;
  rpcErrors: { method: string; error: string }[];
  toasts: { title: string; body?: string }[];
  lastActivity: number;
}

const dbg: WfhDebug = ((window as any).__wfh ??= { pending: 0, calls: 0, rpcErrors: [], toasts: [], lastActivity: Date.now() });

async function rpcPost(method: string, params: unknown): Promise<unknown> {
  dbg.pending++;
  dbg.calls++;
  dbg.lastActivity = performance.now();
  try {
    const r = await fetch("/rpc", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ method, params: params ?? {} }) });
    const j = await r.json();
    if (j.error !== undefined) {
      dbg.rpcErrors.push({ method, error: String(j.error) });
      console.warn(`[rpc-error] ${method}: ${j.error}`);
      throw new Error(String(j.error));
    }
    return j.result;
  } finally {
    dbg.pending--;
    dbg.lastActivity = performance.now();
  }
}

export function callable<Args extends any[] = [], Return = void>(route: string): (...args: Args) => Promise<Return> {
  return (async (...args: any[]) => {
    if (route === "rpc") return rpcPost(args[0], args[1]);
    throw new Error(`preview: backend callable "${route}" is not bridged`);
  }) as any;
}

export async function call<Args extends any[] = [], Return = void>(route: string, ...args: Args): Promise<Return> {
  return callable<Args, Return>(route)(...args);
}

// ---- events -----------------------------------------------------------------------------------

const listeners = new Map<string, Set<Listener>>();
let seq = -1;
let timer: ReturnType<typeof setTimeout> | null = null;

async function poll(): Promise<void> {
  timer = null;
  try {
    const r = await fetch(`/events?since=${Math.max(seq, 0)}`);
    const j = await r.json();
    if (seq < 0) {
      seq = j.seq; // skip the backlog from before the page loaded
    } else {
      for (const [n, name, data] of j.events as [number, string, unknown][]) {
        seq = Math.max(seq, n);
        listeners.get("wfh_event")?.forEach((fn) => {
          try {
            fn(name, data);
          } catch (e) {
            console.error("[preview] event listener failed", name, e);
          }
        });
      }
    }
  } catch {
    // bridge gone; keep trying quietly
  }
  if (listeners.size && !(window as any).__wfhNoEvents) timer = setTimeout(poll, 1500);
}

export function addEventListener<Args extends any[] = []>(event: string, fn: (...args: Args) => any): (...args: Args) => any {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event)!.add(fn as Listener);
  if (!timer) timer = setTimeout(poll, 0);
  return fn;
}

export function removeEventListener<Args extends any[] = []>(event: string, fn: (...args: Args) => any): void {
  listeners.get(event)?.delete(fn as Listener);
  if (listeners.get(event)?.size === 0) listeners.delete(event);
}

// ---- toaster ----------------------------------------------------------------------------------

export interface ToastData {
  title: any;
  body: any;
  duration?: number;
  [k: string]: any;
}

export const toaster = {
  toast(t: ToastData) {
    dbg.toasts.push({ title: String(t.title), body: t.body ? String(t.body) : undefined });
    console.info("[preview] toast:", t.title, t.body ?? "");
    const el = document.createElement("div");
    el.className = "pv-toast";
    el.textContent = `${t.title}${t.body ? " — " + t.body : ""}`;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), t.duration ?? 4000);
  },
  dismissToast() {},
};

// ---- misc -------------------------------------------------------------------------------------

export enum FileSelectionType {
  FILE,
  FOLDER,
}

export async function openFilePicker(..._args: any[]): Promise<{ path: string; realpath: string }> {
  console.info("[preview] openFilePicker is not available in the preview");
  throw new Error("File picker is not available in the preview harness");
}

export function definePlugin<T>(fn: () => T): () => T {
  return fn;
}

export const routerHook = {
  addRoute: (..._a: any[]) => {},
  removeRoute: (..._a: any[]) => {},
  addPatch: (..._a: any[]) => ({}),
  removePatch: (..._a: any[]) => {},
  addGlobalComponent: (..._a: any[]) => {},
  removeGlobalComponent: (..._a: any[]) => {},
};

export const executeInTab = async () => ({ success: false, result: null });
export const injectCssIntoTab = async () => "";
export const removeCssFromTab = async () => {};
export const fetchNoCors = (input: RequestInfo, init?: RequestInit) => fetch(input, init);
export const getExternalResourceURL = (url: string) => url;
export const useQuickAccessVisible = () => true;
