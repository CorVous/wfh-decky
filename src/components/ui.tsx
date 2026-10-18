// Shared building blocks so every page looks and navigates the same way.
import { DialogButton, Field, Focusable, PanelSectionRow } from "@decky/ui";
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

// Prev / Next row under a paged list. `label` adds a count after "x / y" (e.g. "140 relics").
// Used directly by pages that page on the server, and by Paged / Grid below.
export const Pager: FC<{ page: number; pages: number; onPage: (p: number) => void; label?: ReactNode }> = ({ page, pages, onPage, label }) => (
  <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "8px" }}>
    <DialogButton style={pagerButton} disabled={page <= 0} onClick={() => onPage(page - 1)}>
      Prev
    </DialogButton>
    <span style={{ color: colors.muted, fontSize: "13px", whiteSpace: "nowrap" }}>
      {page + 1} / {pages}
      {label != null && label !== "" && <> · {label}</>}
    </span>
    <DialogButton style={pagerButton} disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
      Next
    </DialogButton>
  </Focusable>
);
const pagerButton: CSSProperties = { minWidth: "80px", width: "auto", flex: "0 0 auto", padding: "6px 14px" };

// Client-side pager for long lists (gamepad friendly: Prev/Next buttons). The page goes back
// to the first one when `resetKey` changes (e.g. a new filter or search).
export function Paged<T>({ items, pageSize = 25, render, label, resetKey }: { items: T[]; pageSize?: number; render: (item: T, i: number) => ReactNode; label?: ReactNode; resetKey?: unknown }) {
  const [page, setPage] = useState(0);
  const [key, setKey] = useState(resetKey);
  if (key !== resetKey) {
    setKey(resetKey);
    setPage(0);
  }
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const p = Math.min(page, pages - 1);
  return (
    <>
      {items.slice(p * pageSize, (p + 1) * pageSize).map((it, i) => render(it, p * pageSize + i))}
      {pages > 1 && <Pager page={p} pages={pages} onPage={setPage} label={label} />}
    </>
  );
}

export function chunk<T>(items: T[], n: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += n) out.push(items.slice(i, i + n));
  return out;
}

// Items laid out `cols` to a line. Each line is a horizontal Focusable inside a vertical one,
// so the d-pad moves left/right inside a line and up/down between lines (a plain CSS grid
// leaves left/right dead). With `rows`, the lines are paged `rows` at a time.
export function Grid<T>({ items, cols, render, rows, gap = "8px", rowGap, label, resetKey, style }: {
  items: T[];
  cols: number;
  render: (item: T, index: number) => ReactNode;
  rows?: number;
  gap?: string;
  rowGap?: string;
  label?: ReactNode;
  resetKey?: unknown;
  style?: CSSProperties;
}) {
  const lines = chunk(items, cols);
  const line = (ln: T[], i: number) => (
    <Focusable key={i} flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`, gap, alignItems: "stretch" }}>
      {ln.map((it, j) => render(it, i * cols + j))}
    </Focusable>
  );
  const body = (shown: T[][], first: number) => (
    <Focusable style={{ display: "flex", flexDirection: "column", gap: rowGap ?? gap, ...style }}>{shown.map((ln, i) => line(ln, first + i))}</Focusable>
  );
  if (!rows) return body(lines, 0);
  return <PagedLines lines={lines} rows={rows} body={body} label={label} resetKey={resetKey} />;
}

function PagedLines<T>({ lines, rows, body, label, resetKey }: { lines: T[][]; rows: number; body: (shown: T[][], first: number) => ReactNode; label?: ReactNode; resetKey?: unknown }) {
  const [page, setPage] = useState(0);
  const [key, setKey] = useState(resetKey);
  if (key !== resetKey) {
    setKey(resetKey);
    setPage(0);
  }
  const pages = Math.max(1, Math.ceil(lines.length / rows));
  const p = Math.min(page, pages - 1);
  return (
    <>
      {body(lines.slice(p * rows, (p + 1) * rows), p * rows)}
      {pages > 1 && <Pager page={p} pages={pages} onPage={setPage} label={label} />}
    </>
  );
}

// Side-by-side columns for the d-pad: left/right moves between columns, up/down stays inside
// one (each column is its own vertical Focusable).
export const Columns: FC<{ cols: ReactNode[]; gap?: string; style?: CSSProperties }> = ({ cols, gap = "12px", style }) => (
  <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: `repeat(${cols.length}, minmax(0, 1fr))`, columnGap: gap, alignItems: "start", ...style }}>
    {cols.map((c, i) => (
      <Focusable key={i} style={{ minWidth: 0 }}>
        {c}
      </Focusable>
    ))}
  </Focusable>
);

// Panel style for the cards in a Grid.
export const card: CSSProperties = {
  background: colors.panel,
  border: `1px solid ${colors.border}`,
  borderRadius: "6px",
  padding: "6px 10px",
  minWidth: 0,
  boxSizing: "border-box",
};

export const ellipsis: CSSProperties = { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };

// Thin progress bar (0..1).
export const Bar: FC<{ value: number; color?: string; width?: string | number }> = ({ value, color, width = "100%" }) => (
  <div style={{ width, height: "6px", background: "rgba(255,255,255,0.10)", borderRadius: "3px", overflow: "hidden" }}>
    <div style={{ width: `${Math.max(0, Math.min(100, value * 100))}%`, height: "100%", background: color ?? colors.accent }} />
  </div>
);

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

// Small labelled control for the filter bars above lists.
export const FilterCell: FC<{ label: string; children: ReactNode; width?: string }> = ({ label, children, width }) => (
  <div style={{ display: "flex", flexDirection: "column", gap: "2px", minWidth: 0, width }}>
    <span style={{ fontSize: "11px", color: colors.muted, textTransform: "uppercase" }}>{label}</span>
    {children}
  </div>
);

// DialogButton style for buttons and toggle chips in filter bars: as wide as the label
// (a bare DialogButton fills the whole line), the "on" state filled with the accent colour.
export const chip = (on: boolean): CSSProperties => ({
  minWidth: 0,
  width: "auto",
  flex: "0 0 auto",
  whiteSpace: "nowrap",
  padding: "6px 14px",
  background: on ? colors.accent : undefined,
  color: on ? "#0e121e" : undefined,
});

// One row of chips, one of them selected (tab strips, quick filters). D-pad left/right.
export function Segmented<T extends string | number>({ value, options, onChange, style }: {
  value: T;
  options: { id: T; label: ReactNode }[];
  onChange: (v: T) => void;
  style?: CSSProperties;
}) {
  return (
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px", flexWrap: "wrap", ...style }}>
      {options.map((o) => (
        <DialogButton key={String(o.id)} onClick={() => onChange(o.id)} style={{ ...chip(o.id === value), fontWeight: o.id === value ? 700 : 400 }}>
          {o.label}
        </DialogButton>
      ))}
    </Focusable>
  );
}

// ---- Quick Access Menu ---------------------------------------------------------------------------
// The QAM panel is ~310px wide (~278px inside PanelSection's padding). Every section builds its
// rows from these so they share one look: Steam Field rows, label and a muted second line on the
// left, the value right-aligned in tabular figures.

const qamValue: CSSProperties = { whiteSpace: "nowrap", textAlign: "right", fontVariantNumeric: "tabular-nums" };

// One compact QAM row. Focusable by default so long sections stay reachable (and scroll) with
// the d-pad; `onActivate` usually opens the matching full-screen view.
export const QamRow: FC<{
  label: ReactNode;
  sub?: ReactNode;
  value?: ReactNode;
  color?: string;
  onActivate?: () => void;
  focusable?: boolean;
  separator?: boolean;
}> = ({ label, sub, value, color, onActivate, focusable = true, separator = true }) => (
  <PanelSectionRow>
    <Field
      focusable={focusable}
      padding="compact"
      label={<span style={{ display: "block", ...ellipsis }}>{label}</span>}
      description={sub}
      onActivate={onActivate}
      childrenContainerWidth="min"
      bottomSeparator={separator ? "standard" : "none"}
    >
      {value != null && value !== false && <span style={{ ...qamValue, color: color ?? colors.text }}>{value}</span>}
    </Field>
  </PanelSectionRow>
);

// Loading / error / empty placeholder row.
export const QamMessage: FC<{ label: string; sub?: ReactNode; focusable?: boolean }> = ({ label, sub, focusable }) => (
  <PanelSectionRow>
    <Field label={label} description={sub} focusable={focusable} padding="compact" />
  </PanelSectionRow>
);

// Small muted line: "+12 more", status messages, stale-data warnings (pass colors.warn).
export const QamNote: FC<{ children: ReactNode; color?: string }> = ({ children, color }) => (
  <PanelSectionRow>
    <div style={{ fontSize: "12px", lineHeight: "16px", padding: "4px 0", color: color ?? colors.muted, wordBreak: "break-word" }}>{children}</div>
  </PanelSectionRow>
);

// Counts on one line ("Claim 4  Building 3  Ready 6"), spread across the panel. Zero counts dim.
export const QamTally: FC<{ items: { label: ReactNode; value: ReactNode; color?: string }[]; onActivate?: () => void }> = ({ items, onActivate }) => (
  <PanelSectionRow>
    <Focusable
      onActivate={onActivate}
      focusClassName="gpfocus"
      style={{ display: "flex", justifyContent: "space-between", gap: "8px", fontSize: "13px", padding: "6px 0", borderBottom: `1px solid ${colors.border}` }}
    >
      {items.map((it, i) => (
        <span key={i} style={{ whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
          <span style={{ color: colors.muted }}>{it.label}</span> <span style={{ color: it.color ?? colors.text }}>{it.value}</span>
        </span>
      ))}
    </Focusable>
  </PanelSectionRow>
);

// One to three buttons side by side: a single d-pad row instead of a full-width button each.
export const QamButtons: FC<{ buttons: ({ label: ReactNode; onClick: () => void; disabled?: boolean } | false | null | undefined)[] }> = ({ buttons }) => (
  <PanelSectionRow>
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", padding: "6px 0" }}>
      {buttons.filter(Boolean).map((b, i) => {
        const x = b as { label: ReactNode; onClick: () => void; disabled?: boolean };
        return (
          <DialogButton key={i} disabled={x.disabled} onClick={x.onClick} style={{ flex: 1, minWidth: 0, width: "auto", padding: "8px 4px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            {x.label}
          </DialogButton>
        );
      })}
    </Focusable>
  </PanelSectionRow>
);
