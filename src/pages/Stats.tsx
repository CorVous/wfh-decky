// Stats: daily resource deltas from inventory snapshots and the EE.log trade ledger
// (backend: py_modules/wfh/menus/stats.py). Bars are plain divs, no chart library.
import { ConfirmModal, DialogButton, DropdownItem, Field, Focusable, ModalRoot, Navigation, PanelSection, PanelSectionRow, ToggleField, showModal } from "@decky/ui";
import { FC, useEffect, useState } from "react";

import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { Empty, Guard, Page, Row, Section, Stat, colors } from "../components/ui";
import { call, useEvent, useRpc } from "../rpc";

interface CatalogEntry {
  id: string;
  label: string;
  compact: boolean;
}

interface StripEntry {
  id: string;
  label: string;
  current: number | null;
  delta: number;
  currentText: string;
  deltaText: string;
}

interface Flow {
  count: number;
  sales: number;
  purchases: number;
  swaps: number;
  platIn: number;
  platOut: number;
  net: number;
}

interface Trade {
  id: string;
  ts: number;
  date: string;
  type: "sale" | "purchase" | "trade";
  platChange: number;
  partner: string;
  items: { name: string; count: number; direction: "given" | "received" }[];
}

interface StatsView {
  catalog: CatalogEntry[];
  chart: string[];
  days: number;
  dayChoices: number[];
  strip: StripEntry[];
  today: { date: string; relicsOpened: number; dailyTrades: number; hasData: boolean };
  series: { dates: string[]; values: Record<string, number[]>; relicsOpened: number[]; dailyTrades: number[]; played: boolean[] };
  totals: Record<string, number>;
  trades: { today: Flow; month: Flow; total: number; recent: Trade[] };
  hasData: boolean;
  tradeTracking: boolean;
}

interface QamView {
  hasData: boolean;
  date: string;
  rows: { id: string; label: string; delta: number; deltaText: string; currentText: string }[];
  relicsOpened: number;
  trades: Flow;
}

const COLORS: Record<string, string> = {
  plat: colors.plat,
  ducats: colors.ducat,
  credits: "#c8d2e6",
  endo: "#d29be6",
  kuva: "#ff7a6e",
  aya: "#7ad7c8",
  vitus: "#9ad06a",
};

function compact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(1).replace(/\.0$/, "")}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  if (a >= 1e4) return `${(v / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  return Math.round(v).toLocaleString();
}

function signed(v: number, text?: string): string {
  const t = text ?? compact(v);
  return v > 0 ? `+${t}` : v < 0 ? (t.startsWith("-") ? t : `-${t}`) : "±0";
}

function deltaColor(v: number): string {
  return v > 0 ? colors.good : v < 0 ? colors.bad : colors.muted;
}

// Vertical bars around a zero line; negative days hang below it.
export const BarChart: FC<{ title: string; values: number[]; dates: string[]; played?: boolean[]; color?: string; total?: number }> = ({ title, values, dates, played, color, total }) => {
  const max = Math.max(1, ...values.map((v) => Math.max(0, v)));
  const min = Math.min(0, ...values);
  const span = max - min || 1;
  const height = 96;
  const zero = (max / span) * height;
  const best = values.reduce((bi, v, i) => (v > values[bi] ? i : bi), 0);
  const [sel, setSel] = useState<number | null>(null);
  const i = sel ?? values.length - 1;
  return (
    <Focusable
      focusClassName="gpfocus"
      onActivate={() => setSel(sel === null ? best : null)}
      style={{ margin: "10px 0", padding: "8px", borderRadius: "6px", background: colors.panel, border: `1px solid ${colors.border}` }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", marginBottom: "6px" }}>
        <span style={{ fontWeight: 600, color: color ?? colors.accent }}>{title}</span>
        <span style={{ color: colors.muted }}>
          {dates[i]}: <span style={{ color: deltaColor(values[i] ?? 0) }}>{signed(values[i] ?? 0)}</span>
          {total !== undefined && <> · {dates.length}d total <span style={{ color: deltaColor(total) }}>{signed(total)}</span></>}
          {sel !== null ? " · best day" : ""}
        </span>
      </div>
      <div style={{ position: "relative", height: `${height}px`, display: "flex", alignItems: "stretch", gap: values.length > 45 ? "1px" : "3px" }}>
        <div style={{ position: "absolute", left: 0, right: 0, top: `${zero}px`, borderTop: `1px solid ${colors.border}` }} />
        {values.map((v, k) => {
          const h = (Math.abs(v) / span) * height;
          const isSel = k === i;
          return (
            <div key={k} style={{ flex: 1, position: "relative", minWidth: "2px" }}>
              <div
                style={{
                  position: "absolute",
                  left: 0,
                  right: 0,
                  top: v >= 0 ? `${zero - h}px` : `${zero}px`,
                  height: `${Math.max(v === 0 ? 0 : 2, h)}px`,
                  background: v < 0 ? colors.bad : color ?? colors.accent,
                  opacity: played && !played[k] ? 0.25 : isSel ? 1 : 0.75,
                  outline: isSel ? `1px solid ${colors.text}` : undefined,
                }}
              />
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: colors.muted, marginTop: "4px" }}>
        <span>{dates[0]}</span>
        <span>max {compact(max)}{min < 0 ? ` · min ${compact(min)}` : ""}</span>
        <span>{dates[dates.length - 1]}</span>
      </div>
    </Focusable>
  );
};

function ResourcePicker({ catalog, chart, onSave, closeModal }: { catalog: CatalogEntry[]; chart: string[]; onSave: (ids: string[]) => void; closeModal?: () => void }) {
  const [sel, setSel] = useState<string[]>(chart);
  return (
    <ModalRoot
      closeModal={closeModal}
      onOK={() => {
        onSave(sel.length ? sel : chart);
        closeModal?.();
      }}
    >
      <div style={{ fontWeight: 600, marginBottom: "8px" }}>Charted resources</div>
      <div style={{ maxHeight: "420px", overflowY: "auto" }}>
        {catalog.map((c) => (
          <ToggleField
            key={c.id}
            label={c.label}
            checked={sel.includes(c.id)}
            onChange={(v) => setSel(v ? [...sel, c.id] : sel.filter((x) => x !== c.id))}
          />
        ))}
      </div>
      <DialogButton
        style={{ marginTop: "10px" }}
        onClick={() => {
          onSave(sel.length ? sel : chart);
          closeModal?.();
        }}
      >
        Save
      </DialogButton>
    </ModalRoot>
  );
}

function tradeLabel(t: Trade): string {
  const given = t.items.filter((i) => i.direction === "given");
  const got = t.items.filter((i) => i.direction === "received");
  const list = (t.type === "purchase" ? got : given).length ? (t.type === "purchase" ? got : given) : t.items;
  const names = list.map((i) => (i.count > 1 ? `${i.name} x${i.count}` : i.name)).join(", ");
  return names || "(no items)";
}

const TYPE_LABEL: Record<Trade["type"], string> = { sale: "Sold", purchase: "Bought", trade: "Traded" };

const TradeRow: FC<{ t: Trade; onDeleted: () => void }> = ({ t, onDeleted }) => (
  <Row
    label={tradeLabel(t)}
    sub={`${TYPE_LABEL[t.type]} · ${t.partner || "unknown"} · ${new Date(t.ts * 1000).toLocaleString()}`}
    value={
      t.platChange ? (
        <span style={{ color: t.type === "purchase" ? colors.bad : colors.good }}>
          {t.type === "purchase" ? "-" : "+"}
          {t.platChange}p
        </span>
      ) : (
        <span style={{ color: colors.muted }}>swap</span>
      )
    }
    onActivate={() =>
      showModal(
        <ConfirmModal
          strTitle="Delete this trade?"
          strDescription={`${TYPE_LABEL[t.type]} ${tradeLabel(t)} with ${t.partner || "unknown"}. It is removed from the ledger (the day's trade count stays).`}
          strOKButtonText="Delete"
          bDestructiveWarning
          onOK={async () => {
            await call("stats.trade_delete", { id: t.id });
            onDeleted();
          }}
        />,
      )
    }
  />
);

const Ledger: FC<{ version: number }> = ({ version }) => {
  const [offset, setOffset] = useState(0);
  const limit = 15;
  const page = useRpc<{ rows: Trade[]; total: number; offset?: number }>("stats.trades", { offset, limit }, [version]);
  // The backend clamps past-the-end offsets (last row of the last page deleted); follow it.
  const served = page.data?.offset;
  useEffect(() => {
    if (served !== undefined && served !== offset) setOffset(served);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [served]);
  return (
    <Guard state={page}>
      {(p) =>
        p.total === 0 ? (
          <Empty text="No trades recorded yet. Trades are read from the in-game trade dialog in EE.log." />
        ) : (
          <>
            {p.rows.map((t) => (
              <TradeRow key={t.id} t={t} onDeleted={page.reload} />
            ))}
            {p.total > limit && (
              <Focusable style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "8px" }} flow-children="horizontal">
                <DialogButton style={{ minWidth: 0, width: "90px" }} disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
                  Prev
                </DialogButton>
                <span style={{ color: colors.muted }}>
                  {offset / limit + 1} / {Math.ceil(p.total / limit)}
                </span>
                <DialogButton style={{ minWidth: 0, width: "90px" }} disabled={offset + limit >= p.total} onClick={() => setOffset(offset + limit)}>
                  Next
                </DialogButton>
              </Focusable>
            )}
          </>
        )
      }
    </Guard>
  );
};

const FlowLine: FC<{ label: string; f: Flow }> = ({ label, f }) => (
  <div style={{ margin: "4px 0" }}>
    <Stat label={label} value={<span style={{ color: deltaColor(f.net) }}>{signed(f.net)}p</span>} />
    <Stat label="Sales" value={`${f.sales} · +${f.platIn}p`} />
    <Stat label="Purchases" value={`${f.purchases} · -${f.platOut}p`} />
    <Stat label="Swaps" value={f.swaps} />
  </div>
);

const Stats: FC = () => {
  const [days, setDays] = useState<number | undefined>(undefined);
  const [version, setVersion] = useState(0);
  const view = useRpc<StatsView>("stats.view", days ? { days } : {}, [version]);
  useEvent("stats.updated", () => setVersion((v) => v + 1));
  return (
    <Page>
      <Guard state={view}>
        {(v) => (
          <>
            <Section title={`Today (${v.today.date})`}>
              {!v.today.hasData && <div style={{ color: colors.muted, marginBottom: "6px" }}>Waiting for an inventory read. Deltas start from the first read of the day.</div>}
              <Focusable focusClassName="gpfocus" style={{ display: "flex", flexWrap: "wrap", rowGap: "8px" }}>
                {v.strip.map((s) => (
                  <Stat
                    key={s.id}
                    label={s.label}
                    value={
                      <span>
                        {s.currentText} <span style={{ fontSize: "13px", color: deltaColor(s.delta) }}>{signed(s.delta, s.deltaText)}</span>
                      </span>
                    }
                  />
                ))}
                <Stat label="Relics opened" value={v.today.relicsOpened} />
                <Stat label="Trades" value={v.today.dailyTrades} />
              </Focusable>
            </Section>
            <Focusable style={{ display: "flex", gap: "12px", alignItems: "center" }} flow-children="horizontal">
              <div style={{ width: "260px" }}>
                <DropdownItem
                  label="Days"
                  rgOptions={v.dayChoices.map((d) => ({ data: d, label: `${d} days` }))}
                  selectedOption={v.days}
                  onChange={async (o) => {
                    setDays(o.data);
                    await call("stats.configure", { days: o.data });
                  }}
                />
              </div>
              <DialogButton
                style={{ width: "220px", minWidth: 0 }}
                onClick={() =>
                  showModal(
                    <ResourcePicker
                      catalog={v.catalog}
                      chart={v.chart}
                      onSave={async (ids) => {
                        await call("stats.configure", { chart: ids });
                        view.reload();
                      }}
                    />,
                  )
                }
              >
                Choose resources
              </DialogButton>
            </Focusable>
            {!v.hasData && <Empty text="No history yet. Stats are recorded from each inventory read while you play." />}
            {v.chart.map((id) => (
              <BarChart
                key={id}
                title={v.catalog.find((c) => c.id === id)?.label ?? id}
                values={v.series.values[id] ?? []}
                dates={v.series.dates}
                played={v.series.played}
                color={COLORS[id]}
                total={v.totals[id]}
              />
            ))}
            <BarChart title="Relics opened" values={v.series.relicsOpened} dates={v.series.dates} played={v.series.played} color="#e6c86e" total={v.series.relicsOpened.reduce((a, b) => a + b, 0)} />
            <BarChart title="Trades" values={v.series.dailyTrades} dates={v.series.dates} played={v.series.played} color={colors.accent} total={v.series.dailyTrades.reduce((a, b) => a + b, 0)} />
            <Section title="Trades" right={<span style={{ color: colors.muted, fontSize: "12px" }}>{v.trades.total} recorded</span>}>
              {!v.tradeTracking && <div style={{ color: colors.warn, marginBottom: "6px" }}>Trade recording is off (Settings &gt; Tracking).</div>}
              <FlowLine label="Today" f={v.trades.today} />
              <FlowLine label="This month" f={v.trades.month} />
              <Ledger version={version} />
            </Section>
          </>
        )}
      </Guard>
    </Page>
  );
};

export default Stats;

function openStats(): void {
  openTab("stats");
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
}

// QAM: today's deltas from memory only (stats.qam never reads files or the network).
export const Qam: FC = () => {
  const q = useRpc<QamView>("stats.qam", {});
  useEvent("stats.updated", () => q.reload());
  const d = q.data;
  if (!d) return null;
  return (
    <PanelSection title="Today">
      {!d.hasData && (
        <PanelSectionRow>
          <Field label="No inventory read yet today" />
        </PanelSectionRow>
      )}
      {d.hasData &&
        d.rows.map((r) => (
          <PanelSectionRow key={r.id}>
            <Field focusable padding="compact" label={r.label} description={r.currentText} onActivate={openStats}>
              <span style={{ color: deltaColor(r.delta), whiteSpace: "nowrap" }}>{signed(r.delta, r.deltaText)}</span>
            </Field>
          </PanelSectionRow>
        ))}
      {(d.relicsOpened > 0 || d.trades.count > 0) && (
        <PanelSectionRow>
          <Field focusable padding="compact" onActivate={openStats} label="Relics · trades" description={d.trades.count ? `${d.trades.sales} sold, ${d.trades.purchases} bought` : undefined}>
            <span style={{ whiteSpace: "nowrap" }}>
              {d.relicsOpened} · <span style={{ color: deltaColor(d.trades.net) }}>{signed(d.trades.net)}p</span>
            </span>
          </Field>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};
