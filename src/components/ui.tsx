// Shared building blocks so every page looks and navigates the same way.
import { DialogButton, Focusable } from "@decky/ui";
import { Component, CSSProperties, FC, ReactNode, useState } from "react";

export const colors = {
  text: "#e8ecf5",
  muted: "#96a0b6",
  accent: "#6eaaff",
  good: "#60d68c",
  warn: "#f0be50",
  bad: "#f06464",
  plat: "#a0c8ff",
  ducat: "#e2b45a",
  panel: "rgba(255,255,255,0.04)",
  border: "rgba(255,255,255,0.10)",
};

export const Empty: FC<{ text: string }> = ({ text }) => (
  <div style={{ padding: "16px", color: colors.muted }}>{text}</div>
);

export const Loading: FC = () => <Empty text="Loading…" />;

export const ErrorText: FC<{ error: string }> = ({ error }) => (
  <div style={{ padding: "8px 16px", color: colors.bad, wordBreak: "break-word" }}>{error}</div>
);

// Renders loading / error / empty states around an RPC result.
export function Guard<T>({ state, children, empty }: {
  state: { data: T | undefined; error?: string; loading: boolean };
  children: (data: T) => ReactNode;
  empty?: string;
}) {
  if (state.error && state.data === undefined) return <ErrorText error={state.error} />;
  if (state.data === undefined) return <Loading />;
  if (empty && Array.isArray(state.data) && state.data.length === 0) return <Empty text={empty} />;
  return <>{children(state.data)}</>;
}

export const Page: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ padding: "0 24px 24px", overflowY: "auto", height: "100%", width: "100%", boxSizing: "border-box", color: colors.text }}>{children}</div>
);

export const Section: FC<{ title?: string; right?: ReactNode; children: ReactNode; style?: CSSProperties }> = ({ title, right, children, style }) => (
  <div style={{ margin: "12px 0", background: colors.panel, border: `1px solid ${colors.border}`, borderRadius: "6px", padding: "10px 12px", ...style }}>
    {(title || right) && (
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "6px" }}>
        <div style={{ fontWeight: 600, color: colors.accent }}>{title}</div>
        {right}
      </div>
    )}
    {children}
  </div>
);

// One focusable row: label on the left, value on the right.
export const Row: FC<{ label: ReactNode; value?: ReactNode; sub?: ReactNode; onActivate?: () => void; color?: string }> = ({ label, value, sub, onActivate, color }) => (
  <Focusable
    onActivate={onActivate}
    focusClassName="gpfocus"
    style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "4px 2px", gap: "8px", borderBottom: `1px solid ${colors.border}` }}
  >
    <div style={{ minWidth: 0 }}>
      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: color ?? colors.text }}>{label}</div>
      {sub && <div style={{ fontSize: "12px", color: colors.muted }}>{sub}</div>}
    </div>
    {value !== undefined && <div style={{ flexShrink: 0, textAlign: "right" }}>{value}</div>}
  </Focusable>
);

export const Stat: FC<{ label: string; value: ReactNode; color?: string }> = ({ label, value, color }) => (
  <div style={{ display: "inline-flex", flexDirection: "column", marginRight: "18px" }}>
    <span style={{ fontSize: "11px", color: colors.muted, textTransform: "uppercase" }}>{label}</span>
    <span style={{ fontSize: "16px", fontWeight: 600, color: color ?? colors.text }}>{value}</span>
  </div>
);

export const Badge: FC<{ text: string; color?: string }> = ({ text, color }) => (
  <span style={{ fontSize: "11px", padding: "1px 6px", borderRadius: "8px", border: `1px solid ${color ?? colors.accent}`, color: color ?? colors.accent, marginLeft: "6px" }}>{text}</span>
);

export const Plat: FC<{ value: number | null | undefined }> = ({ value }) => (
  <span style={{ color: colors.plat }}>{value == null ? "–" : `${Math.round(value * 10) / 10}p`}</span>
);

export const Ducats: FC<{ value: number | null | undefined }> = ({ value }) => (
  <span style={{ color: colors.ducat }}>{value == null ? "–" : `${value}d`}</span>
);

// Countdown text ("2h 05m") to an epoch-ms timestamp; re-renders each second via the parent poll.
export function countdown(toMs: number | null | undefined, nowMs = Date.now()): string {
  if (!toMs) return "–";
  let s = Math.max(0, Math.floor((toMs - nowMs) / 1000));
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

// Client-side pager for long lists (gamepad friendly: Prev/Next buttons).
export function Paged<T>({ items, pageSize = 25, render }: { items: T[]; pageSize?: number; render: (item: T, i: number) => ReactNode }) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const p = Math.min(page, pages - 1);
  return (
    <>
      {items.slice(p * pageSize, (p + 1) * pageSize).map((it, i) => render(it, p * pageSize + i))}
      {pages > 1 && (
        <Focusable style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "8px" }} flow-children="horizontal">
          <DialogButton style={{ minWidth: 0, width: "90px" }} disabled={p === 0} onClick={() => setPage(p - 1)}>Prev</DialogButton>
          <span style={{ color: colors.muted }}>{p + 1} / {pages}</span>
          <DialogButton style={{ minWidth: 0, width: "90px" }} disabled={p >= pages - 1} onClick={() => setPage(p + 1)}>Next</DialogButton>
        </Focusable>
      )}
    </>
  );
}

// Catches a render error in one full-screen page so the tab strip (and the
// other pages) stay usable. Key it by page id so switching tabs resets it.
export class PageBoundary extends Component<{ id: string; children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    return { error: String((e as Error)?.message ?? e) };
  }
  componentDidCatch(e: unknown) {
    console.error("[WFHelper] page failed", this.props.id, e);
  }
  render() {
    if (this.state.error !== null) {
      return (
        <Page>
          <Section title="This page failed to render">
            <ErrorText error={this.state.error} />
            <Focusable style={{ padding: "8px 16px" }}>
              <DialogButton onClick={() => this.setState({ error: null })}>Retry</DialogButton>
            </Focusable>
          </Section>
        </Page>
      );
    }
    return this.props.children;
  }
}
