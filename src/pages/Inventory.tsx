// Inventory: tabs (prime parts, relics, mods, arcanes, full sets, equipment, resources, misc,
// everything), value strip, filters and sort, item details with the order book on demand.
// Backend: py_modules/wfh/menus/inventory.py (inventory.view/item/qam/prefetch_prices/ui_set) and
// py_modules/wfh/inventory (inventory.status/refresh/import_file).
import { openFilePicker } from "@decky/api";
import {
  ButtonItem,
  DialogButton,
  DropdownItem,
  Field,
  Focusable,
  ModalRoot,
  Navigation,
  PanelSection,
  PanelSectionRow,
  SliderField,
  TextField,
  ToggleField,
  showModal,
} from "@decky/ui";
import { FC, ReactNode, useEffect, useState } from "react";

import { Badge, Ducats, Empty, ErrorText, Loading, Page, Plat, Section, Stat, colors } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (mirror menus/inventory.py) -----------------------------------------------------

type Tab = "everything" | "all_parts" | "relics" | "mods" | "arcanes" | "full_sets" | "equipment" | "resources" | "misc";
type YesNo = "all" | "yes" | "no";

interface InvRow {
  key: string;
  name: string;
  internalName: string;
  category: string;
  categoryLabel: string;
  inventoryGroup: string;
  rank: number;
  maxRank: number | null;
  amount: number;
  combinedAmount: number | null;
  isPrime: boolean;
  vaulted: boolean;
  tradable: boolean;
  favorite: boolean;
  equipped: boolean;
  leveledUp: boolean;
  marketSlug: string | null;
  subtype: string | null;
  platinum: number | null;
  ducats: number | null;
  ducatonator: number | null;
  equippedSummary: string | null;
  completeSets: number | null;
  missingParts: number | null;
  ownedPartTypes: number | null;
  totalPartTypes: number | null;
  setCategory?: string;
}

interface ValueTotals {
  platinum: number;
  ducats: number;
  platinumUnpriced: number;
  ducatsUnpriced: number;
  unpriced: number;
  counted: number;
}

interface Prefs {
  valueScope: "prime" | "tradable";
  valueMinPlat: number;
  hideFoundryClaims: boolean;
  autoPrefetch: boolean;
}

export interface InvStatus {
  source?: string | null;
  file?: string | null;
  found?: boolean;
  running?: boolean;
  last_run_at?: number | null;
  last_run_ok?: boolean | null;
  last_reason?: string | null;
  last_error?: { message?: string } | null;
  warframe_running?: boolean;
}

interface InvMeta {
  origin: string;
  syncTime: number | null;
  fetchedAt: number;
  path: string | null;
}

interface InvView {
  hasInventory: boolean;
  status?: InvStatus;
  home?: string;
  prefs?: Prefs;
  inventory?: InvMeta | null;
  tab: Tab;
  sort?: string;
  direction?: "asc" | "desc";
  sorts?: string[];
  total: number;
  offset?: number;
  limit?: number;
  rows: InvRow[];
  tabs?: { id: Tab; label: string; count: number }[];
  everythingHidden?: string[];
  setCategories?: { id: string; count: number }[];
  value?: { scope: string; minPlat: number; inView: ValueTotals; inventory: ValueTotals; rows: ("inView" | "inventory")[] };
  pricesMissing?: number;
}

interface ItemDetail extends InvRow {
  equippedIn: string[];
  modularParts: string[];
  priceRank: number | null;
  components?: { name: string; uniqueName: string; needed: number; owned: number; ducats: number | null }[];
  parentSet?: { key: string; name: string; completeSets: number | null; missingParts: number | null; platinum: number | null };
  recipe?: { product: string; credits: number; buildTime: number; ingredients: { name: string; needed: number; owned: number }[] };
}

interface OrderRow {
  user?: string;
  ingame_name?: string;
  platinum?: number;
  quantity?: number;
  rank?: number | null;
  status?: string;
}

interface MarketItem {
  slug: string;
  name: string;
  price: { median: number | null; low_sell: number | null; high_buy: number | null; volume: number | null } | null;
  orders: { sell: OrderRow[]; buy: OrderRow[] };
}

interface QamView {
  hasInventory: boolean;
  scope: "prime" | "tradable";
  minPlat: number;
  presets: number[];
  totals?: ValueTotals;
  top?: { name: string; amount: number; platinum: number; ducats: number | null }[];
  inventory?: InvMeta | null;
}

// ---- constants ------------------------------------------------------------------------------

const SORT_LABEL: Record<string, string> = {
  name: "Name",
  platinum: "Platinum",
  ducats: "Ducats",
  amount: "Amount",
  ducatonator: "Ducats per plat",
  complete_sets: "Complete sets",
  missing_parts: "Missing parts",
};
const DESC_DEFAULT = ["platinum", "ducats", "amount", "ducatonator", "complete_sets"];
const SOURCES: { id: string; label: string }[] = [
  { id: "all_parts", label: "Prime parts" },
  { id: "relics", label: "Relics" },
  { id: "mods", label: "Mods" },
  { id: "arcanes", label: "Arcanes" },
  { id: "full_sets", label: "Full sets" },
  { id: "equipment", label: "Equipment" },
  { id: "resources", label: "Resources" },
  { id: "misc", label: "Misc" },
];
const MIN_PLAT_OPTIONS = [0, 5, 10, 20, 50, 100];
const PAGE_SIZE = 30;

interface Filters {
  vaulted: YesNo;
  partType: "all" | "normal" | "prime";
  favorite: YesNo;
  equipped: YesNo;
  leveledUp: YesNo;
  minimumPlatinum: number;
  minimumAmount: number;
}

interface ViewState {
  tab: Tab;
  sort: string | null;
  direction: "asc" | "desc" | null;
  search: string;
  filters: Filters;
  everythingHidden: string[];
  fullSetsHidden: string[];
  showIncomplete: boolean;
}

const DEFAULT_FILTERS: Filters = { vaulted: "all", partType: "all", favorite: "all", equipped: "all", leveledUp: "all", minimumPlatinum: 0, minimumAmount: 0 };
const DEFAULT_STATE: ViewState = {
  tab: "all_parts",
  sort: null,
  direction: null,
  search: "",
  filters: DEFAULT_FILTERS,
  everythingHidden: ["full_sets"],
  fullSetsHidden: [],
  showIncomplete: false,
};
const STORE_KEY = "wfh.inventory.view";

function loadState(): ViewState {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) {
      const s = JSON.parse(raw);
      // search and filters are session-only (WFHelper keeps them out of storage)
      return { ...DEFAULT_STATE, tab: s.tab ?? DEFAULT_STATE.tab, everythingHidden: s.everythingHidden ?? DEFAULT_STATE.everythingHidden, fullSetsHidden: s.fullSetsHidden ?? [] };
    }
  } catch {
    /* storage unavailable */
  }
  return DEFAULT_STATE;
}

function saveState(s: ViewState): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ tab: s.tab, everythingHidden: s.everythingHidden, fullSetsHidden: s.fullSetsHidden }));
  } catch {
    /* ignore */
  }
}

// ---- helpers ----------------------------------------------------------------------------------

export const fmtNum = (n: number | null | undefined): string => (n == null ? "–" : Math.round(n).toLocaleString("en-US"));

export function ago(ms: number | null | undefined): string {
  if (!ms) return "never";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function openPage(id: string): void {
  openTab(id);
  Navigation.CloseSideMenus();
  Navigation.Navigate(ROUTE);
}

// Segmented buttons (D-pad left/right).
export function Segmented<T extends string | number>({ options, value, onChange, width }: { options: { data: T; label: string }[]; value: T; onChange: (v: T) => void; width?: string }) {
  return (
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px", flexWrap: "wrap" }}>
      {options.map((o) => (
        <DialogButton
          key={String(o.data)}
          onClick={() => onChange(o.data)}
          style={{
            minWidth: 0,
            width: width ?? "auto",
            padding: "4px 10px",
            background: o.data === value ? colors.accent : undefined,
            color: o.data === value ? "#0e121e" : undefined,
            fontWeight: o.data === value ? 700 : 400,
          }}
        >
          {o.label}
        </DialogButton>
      ))}
    </Focusable>
  );
}

// "No inventory yet": refresh from the game or import an exported JSON file.
export const NoInventory: FC<{ status?: InvStatus; home?: string; onDone: () => void }> = ({ status, home, onDone }) => {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string>();
  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    setMsg(undefined);
    try {
      await fn();
      setMsg(ok);
      onDone();
    } catch (e) {
      setMsg(String((e as Error)?.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  const importFile = async () => {
    let path: string;
    try {
      const res = await openFilePicker(0 as unknown as Parameters<typeof openFilePicker>[0], home || "/home/deck", true, true, undefined, ["json"]);
      path = res.realpath || res.path;
    } catch {
      return; // picker closed
    }
    await run(() => call("inventory.import_file", { path }), "Inventory imported.");
  };
  const err = status?.last_error?.message ?? status?.last_reason;
  return (
    <Section title="No inventory yet">
      <div style={{ color: colors.muted, marginBottom: "8px", lineHeight: 1.4 }}>
        {status?.source === "file"
          ? `Source: file${status.file ? ` (${status.file})` : ""}.`
          : status?.warframe_running
            ? "Warframe is running: refresh to read your inventory (after you log in)."
            : "Start Warframe and log in, then refresh. Or import an inventory JSON you exported."}
        {err && <div style={{ color: colors.warn, marginTop: "4px" }}>Last attempt: {err}</div>}
      </div>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px" }}>
        <DialogButton style={{ width: "220px" }} disabled={busy} onClick={() => run(() => call("inventory.refresh"), "Inventory refreshed.")}>
          {busy ? "Working…" : "Refresh from game"}
        </DialogButton>
        <DialogButton style={{ width: "220px" }} disabled={busy} onClick={importFile}>
          Import JSON file…
        </DialogButton>
      </Focusable>
      {msg && <div style={{ marginTop: "8px", color: colors.muted }}>{msg}</div>}
    </Section>
  );
};

// ---- value strip ------------------------------------------------------------------------------

const ValueLine: FC<{ label: string; t: ValueTotals; scope: string }> = ({ label, t, scope }) => (
  <div style={{ display: "flex", gap: "14px", alignItems: "baseline", flexWrap: "wrap" }}>
    <span style={{ color: colors.muted, width: "84px" }}>{label}</span>
    <span style={{ color: colors.plat, fontWeight: 700, fontSize: "16px" }}>
      {t.platinumUnpriced > 0 ? "≥ " : ""}
      {fmtNum(t.platinum)}p
    </span>
    {scope === "prime" && (
      <span style={{ color: colors.ducat, fontWeight: 600 }}>
        {t.ducatsUnpriced > 0 ? "≥ " : ""}
        {fmtNum(t.ducats)}d
      </span>
    )}
    <span style={{ color: colors.muted, fontSize: "12px" }}>
      {t.counted} items{t.unpriced > 0 ? ` · ${t.unpriced} unpriced` : ""}
    </span>
  </div>
);

const ValueStrip: FC<{ v: NonNullable<InvView["value"]>; prefs: Prefs; onPrefs: (p: Partial<{ value_scope: string; value_min_plat: number }>) => void; onFetch: () => void; fetchMsg?: string }> = ({ v, prefs, onPrefs, onFetch, fetchMsg }) => (
  <Section title="Inventory value" right={<span style={{ fontSize: "12px", color: colors.muted }}>median 48 h trade price × amount</span>}>
    {v.rows.length === 0 && <div style={{ color: colors.muted }}>Nothing sellable in this scope.</div>}
    {v.rows.includes("inView") && <ValueLine label="In view" t={v.inView} scope={v.scope} />}
    {v.rows.includes("inventory") && <ValueLine label="Inventory" t={v.inventory} scope={v.scope} />}
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "16px", marginTop: "8px", alignItems: "center", flexWrap: "wrap" }}>
      <Segmented
        options={[
          { data: "prime", label: "Prime parts" },
          { data: "tradable", label: "All tradables" },
        ]}
        value={prefs.valueScope}
        onChange={(s) => onPrefs({ value_scope: s })}
      />
      <span style={{ color: colors.muted, fontSize: "12px" }}>Min per unit</span>
      <Segmented options={[0, 5, 10, 15].map((n) => ({ data: n, label: n === 0 ? "Any" : `${n}p` }))} value={prefs.valueMinPlat} onChange={(n) => onPrefs({ value_min_plat: n })} />
      <DialogButton style={{ minWidth: 0, width: "150px", padding: "4px 10px" }} onClick={onFetch}>
        Fetch prices
      </DialogButton>
      {fetchMsg && <span style={{ color: colors.muted, fontSize: "12px" }}>{fetchMsg}</span>}
    </Focusable>
  </Section>
);

// ---- detail modal -----------------------------------------------------------------------------

const OrderBook: FC<{ slug: string; rank: number | null }> = ({ slug, rank }) => {
  const st = useRpc<MarketItem>("market.item", { slug, rank }, []);
  if (st.error) return <ErrorText error={st.error} />;
  if (!st.data) return <Loading />;
  const m = st.data;
  const col = (title: string, rows: OrderRow[], color: string) => (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ color, fontWeight: 600, marginBottom: "2px" }}>{title}</div>
      {rows.length === 0 && <div style={{ color: colors.muted }}>none online</div>}
      {rows.slice(0, 5).map((o, i) => (
        <div key={i} style={{ display: "flex", justifyContent: "space-between", fontSize: "13px", gap: "8px" }}>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: colors.muted }}>{o.ingame_name ?? o.user ?? "?"}</span>
          <span>
            <span style={{ color: colors.plat }}>{o.platinum}p</span>
            {o.quantity && o.quantity > 1 ? <span style={{ color: colors.muted }}> ×{o.quantity}</span> : null}
          </span>
        </div>
      ))}
    </div>
  );
  return (
    <div>
      {m.price && (
        <div style={{ marginBottom: "6px" }}>
          <Stat label="Median" value={<Plat value={m.price.median} />} />
          <Stat label="Lowest sell" value={<Plat value={m.price.low_sell} />} />
          <Stat label="Best buy" value={<Plat value={m.price.high_buy} />} />
          <Stat label="Volume 48h" value={m.price.volume ?? "–"} />
        </div>
      )}
      <div style={{ display: "flex", gap: "16px" }}>
        {col("Sellers", m.orders.sell, colors.good)}
        {col("Buyers", m.orders.buy, colors.accent)}
      </div>
    </div>
  );
};

const ItemModal: FC<{ itemKey: string; closeModal?: () => void }> = ({ itemKey, closeModal }) => {
  const st = useRpc<ItemDetail | null>("inventory.item", { key: itemKey }, []);
  const [book, setBook] = useState(false);
  const d = st.data;
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text, minWidth: "620px" }}>
        {st.error && <ErrorText error={st.error} />}
        {d === undefined && !st.error && <Loading />}
        {d === null && <Empty text="Item no longer in the inventory." />}
        {d && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "6px" }}>
              <span style={{ fontSize: "20px", fontWeight: 700 }}>{d.name}</span>
              <span style={{ color: colors.muted }}>{d.categoryLabel}</span>
              {d.vaulted && <Badge text="Vaulted" color={colors.bad} />}
              {d.equipped && <Badge text="Equipped" color={colors.good} />}
              {d.favorite && <Badge text="Favorite" color={colors.warn} />}
            </div>
            <div style={{ marginBottom: "8px" }}>
              <Stat label="Owned" value={d.combinedAmount != null && d.combinedAmount !== d.amount ? `${d.amount} (${d.combinedAmount} all ranks)` : d.amount} />
              {d.maxRank != null && d.maxRank > 0 && d.maxRank !== 30 && <Stat label="Rank" value={`${d.rank}/${d.maxRank}`} />}
              <Stat label="Platinum" value={<Plat value={d.platinum} />} />
              {d.ducats != null && <Stat label="Ducats" value={<Ducats value={d.ducats} />} />}
              {d.ducatonator != null && <Stat label="d/p" value={d.ducatonator.toFixed(2)} />}
              {d.tradable === false && <Stat label="Trade" value="untradable" color={colors.muted} />}
            </div>
            {d.equippedIn.length > 0 && <div style={{ color: colors.muted, fontSize: "13px" }}>Equipped in {d.equippedIn.join(", ")}</div>}
            {d.modularParts.length > 0 && <div style={{ color: colors.muted, fontSize: "13px" }}>Parts: {d.modularParts.join(", ")}</div>}
            {d.components && (
              <Section title="Set parts">
                {d.components.map((c) => (
                  <div key={c.uniqueName} style={{ display: "flex", justifyContent: "space-between", fontSize: "14px" }}>
                    <span>{c.name}</span>
                    <span style={{ color: c.owned >= c.needed ? colors.good : colors.bad }}>
                      {c.owned}/{c.needed}
                    </span>
                  </div>
                ))}
              </Section>
            )}
            {d.parentSet && (
              <div style={{ fontSize: "13px", margin: "6px 0" }}>
                Part of <b>{d.parentSet.name}</b>:{" "}
                {d.parentSet.missingParts != null ? `${d.parentSet.missingParts} part type(s) missing` : `${d.parentSet.completeSets ?? 0} complete set(s)`}
                {d.parentSet.platinum != null && <span style={{ color: colors.plat }}> · set {Math.round(d.parentSet.platinum)}p</span>}
              </div>
            )}
            {d.recipe && (
              <Section title={`Builds ${d.recipe.product}`} right={<span style={{ color: colors.muted, fontSize: "12px" }}>{fmtNum(d.recipe.credits)} cr</span>}>
                {d.recipe.ingredients.map((i) => (
                  <div key={i.name} style={{ display: "flex", justifyContent: "space-between", fontSize: "14px" }}>
                    <span>{i.name}</span>
                    <span style={{ color: i.owned >= i.needed ? colors.text : colors.bad }}>
                      {fmtNum(i.owned)}/{fmtNum(i.needed)}
                    </span>
                  </div>
                ))}
              </Section>
            )}
            {d.marketSlug && (
              <Section title="warframe.market">
                {book ? (
                  <OrderBook slug={d.marketSlug} rank={d.priceRank} />
                ) : (
                  <Focusable>
                    <DialogButton style={{ width: "220px" }} onClick={() => setBook(true)}>
                      Load order book
                    </DialogButton>
                  </Focusable>
                )}
              </Section>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "10px" }}>
              <DialogButton style={{ width: "140px" }} onClick={closeModal}>
                Close
              </DialogButton>
            </div>
          </>
        )}
      </div>
    </ModalRoot>
  );
};

// ---- rows --------------------------------------------------------------------------------------

const ItemRow: FC<{ r: InvRow; tab: Tab }> = ({ r, tab }) => {
  const open = () => showModal(<ItemModal itemKey={r.key} />);
  const ranked = r.inventoryGroup === "mods" || r.inventoryGroup === "arcanes";
  const isSet = r.inventoryGroup === "full_sets" || r.inventoryGroup === "incomplete_sets";
  return (
    <Focusable
      focusClassName="gpfocus"
      onActivate={open}
      onClick={open}
      style={{ display: "grid", gridTemplateColumns: "1fr 64px 70px 56px 52px", gap: "8px", alignItems: "center", padding: "5px 4px", borderBottom: `1px solid ${colors.border}` }}
    >
      <div style={{ minWidth: 0 }}>
        <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {r.name}
          {ranked && r.maxRank ? <span style={{ color: colors.muted, fontSize: "12px" }}> R{r.rank}/{r.maxRank}</span> : null}
          {r.vaulted && <span style={{ color: colors.bad, fontSize: "11px", marginLeft: "6px" }}>V</span>}
          {r.equipped && <span style={{ color: colors.good, fontSize: "11px", marginLeft: "6px" }}>E</span>}
          {r.favorite && <span style={{ color: colors.warn, fontSize: "11px", marginLeft: "6px" }}>★</span>}
        </div>
        <div style={{ fontSize: "11px", color: colors.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {tab === "everything" || tab === "misc" ? r.categoryLabel : null}
          {isSet && r.missingParts != null ? `${r.ownedPartTypes}/${r.totalPartTypes} parts · ${r.missingParts} missing` : null}
          {isSet && r.setCategory ? ` ${r.setCategory}` : null}
          {r.equippedSummary ? ` ${r.equippedSummary}` : null}
          {r.tradable === false && r.inventoryGroup !== "resources" ? " untradable" : null}
        </div>
      </div>
      <span style={{ textAlign: "right" }}>×{fmtNum(r.amount)}</span>
      <span style={{ textAlign: "right" }}>
        <Plat value={r.platinum} />
      </span>
      <span style={{ textAlign: "right" }}>{r.ducats != null ? <Ducats value={r.ducats} /> : ""}</span>
      <span style={{ textAlign: "right", color: colors.muted, fontSize: "12px" }}>{r.ducatonator != null ? `${r.ducatonator.toFixed(1)}` : ""}</span>
    </Focusable>
  );
};

// ---- full page --------------------------------------------------------------------------------

const yesNoOptions = (label: string) => [
  { data: "all", label: `${label}: any` },
  { data: "yes", label: `${label}: yes` },
  { data: "no", label: `${label}: no` },
];

const Inventory: FC = () => {
  const [s, setS] = useState<ViewState>(loadState);
  const [page, setPage] = useState(0);
  const [searchDraft, setSearchDraft] = useState(s.search);
  const [fetchMsg, setFetchMsg] = useState<string>();
  const update = (patch: Partial<ViewState>) => {
    setS((prev) => {
      const next = { ...prev, ...patch };
      saveState(next);
      return next;
    });
    setPage(0);
  };
  const setFilter = (patch: Partial<Filters>) => update({ filters: { ...s.filters, ...patch } });
  useEffect(() => {
    const t = setTimeout(() => searchDraft !== s.search && update({ search: searchDraft }), 300);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchDraft]);

  const view = useRpc<InvView>(
    "inventory.view",
    {
      tab: s.tab,
      search: s.search,
      sort: s.sort,
      direction: s.direction,
      filters: s.filters,
      offset: page * PAGE_SIZE,
      limit: PAGE_SIZE,
      everything_hidden: s.everythingHidden,
      full_sets_hidden: s.fullSetsHidden,
      show_incomplete: s.showIncomplete,
    },
    [],
  );
  useEvent("inventory.updated", () => view.reload());
  const v = view.data;

  if (view.error && !v) return <Page><ErrorText error={view.error} /></Page>;
  if (!v) return <Page><Loading /></Page>;
  if (!v.hasInventory)
    return (
      <Page>
        <NoInventory status={v.status} home={v.home} onDone={() => view.reload()} />
      </Page>
    );

  const prefs = v.prefs!;
  const pages = Math.max(1, Math.ceil(v.total / PAGE_SIZE));
  const sorts = v.sorts ?? ["name"];
  const setPrefs = async (patch: Record<string, unknown>) => {
    await call("inventory.ui_set", patch);
    view.reload();
  };
  const fetchPrices = async () => {
    const r = await call<{ queued: number }>("inventory.prefetch_prices", { tab: s.tab === "everything" ? "all_parts" : s.tab });
    setFetchMsg(r.queued > 0 ? `Fetching ${r.queued} prices…` : "Prices are cached.");
    if (r.queued > 0) {
      setTimeout(() => view.reload(), 8000);
      setTimeout(() => view.reload(), 30000);
    }
  };

  const tabStrip = (
    <Focusable flow-children="horizontal" style={{ display: "flex", gap: "4px", flexWrap: "wrap", margin: "8px 0" }}>
      {(v.tabs ?? []).map((t) => (
        <DialogButton
          key={t.id}
          onClick={() => {
            setSearchDraft("");
            update({ tab: t.id, sort: null, direction: null, search: "" });
          }}
          style={{
            minWidth: 0,
            width: "auto",
            padding: "4px 10px",
            background: t.id === s.tab ? colors.accent : undefined,
            color: t.id === s.tab ? "#0e121e" : undefined,
            fontWeight: t.id === s.tab ? 700 : 400,
          }}
        >
          {t.label} <span style={{ opacity: 0.7, fontSize: "12px" }}>{t.count}</span>
        </DialogButton>
      ))}
    </Focusable>
  );

  const filters = (
    <Focusable style={{ width: "250px", flexShrink: 0, display: "flex", flexDirection: "column" }}>
      <TextField label="Search" value={searchDraft} onChange={(e) => setSearchDraft(e.target.value)} />
      <DropdownItem
        label="Sort"
        rgOptions={sorts.map((k) => ({ data: k, label: SORT_LABEL[k] ?? k }))}
        selectedOption={v.sort ?? "name"}
        onChange={(o) => update({ sort: o.data, direction: DESC_DEFAULT.includes(o.data) ? "desc" : "asc" })}
      />
      <ToggleField label="Descending" checked={v.direction === "desc"} onChange={(b) => update({ sort: v.sort ?? "name", direction: b ? "desc" : "asc" })} />
      {s.tab === "everything" && (
        <Field label="Sources" childrenLayout="below" focusable={false}>
          <Focusable style={{ display: "flex", flexDirection: "column" }}>
            {SOURCES.map((src) => (
              <ToggleField
                key={src.id}
                label={src.label}
                checked={!s.everythingHidden.includes(src.id)}
                onChange={(b) => update({ everythingHidden: b ? s.everythingHidden.filter((x) => x !== src.id) : [...s.everythingHidden, src.id] })}
              />
            ))}
          </Focusable>
        </Field>
      )}
      {s.tab === "full_sets" && (
        <>
          <ToggleField label="Show incomplete sets" description="Sets with at least one part owned" checked={s.showIncomplete} onChange={(b) => update({ showIncomplete: b, sort: b ? "missing_parts" : null, direction: b ? "asc" : null })} />
          {(v.setCategories ?? []).map((c) => (
            <ToggleField
              key={c.id}
              label={`${c.id} (${c.count})`}
              checked={!s.fullSetsHidden.includes(c.id)}
              onChange={(b) => update({ fullSetsHidden: b ? s.fullSetsHidden.filter((x) => x !== c.id) : [...s.fullSetsHidden, c.id] })}
            />
          ))}
        </>
      )}
      {s.tab !== "resources" && (
        <>
          <DropdownItem label="Min platinum" rgOptions={MIN_PLAT_OPTIONS.map((n) => ({ data: n, label: n === 0 ? "Any" : `${n}p or more` }))} selectedOption={s.filters.minimumPlatinum} onChange={(o) => setFilter({ minimumPlatinum: o.data })} />
          <DropdownItem label="Vaulted" rgOptions={yesNoOptions("Vaulted")} selectedOption={s.filters.vaulted} onChange={(o) => setFilter({ vaulted: o.data })} />
          <DropdownItem
            label="Part type"
            rgOptions={[
              { data: "all", label: "Any" },
              { data: "prime", label: "Prime" },
              { data: "normal", label: "Non-prime" },
            ]}
            selectedOption={s.filters.partType}
            onChange={(o) => setFilter({ partType: o.data })}
          />
          <DropdownItem label="Equipped" rgOptions={yesNoOptions("Equipped")} selectedOption={s.filters.equipped} onChange={(o) => setFilter({ equipped: o.data })} />
          <DropdownItem label="Favorite" rgOptions={yesNoOptions("Favorite")} selectedOption={s.filters.favorite} onChange={(o) => setFilter({ favorite: o.data })} />
          <DropdownItem label="Leveled up" rgOptions={yesNoOptions("Leveled")} selectedOption={s.filters.leveledUp} onChange={(o) => setFilter({ leveledUp: o.data })} />
        </>
      )}
      <ToggleField label="Spares only" description="Own more than one" checked={s.filters.minimumAmount >= 2} onChange={(b) => setFilter({ minimumAmount: b ? 2 : 0 })} />
      <ButtonItem layout="below" onClick={() => { setSearchDraft(""); update({ search: "", filters: DEFAULT_FILTERS, sort: null, direction: null }); }}>
        Reset filters
      </ButtonItem>
      <ToggleField
        label="Hide blueprints in the foundry"
        checked={prefs.hideFoundryClaims}
        onChange={(b) => setPrefs({ hide_foundry_claims: b })}
      />
      <ButtonItem
        layout="below"
        onClick={async () => {
          setFetchMsg("Refreshing inventory…");
          try {
            await call("inventory.refresh");
            setFetchMsg("Inventory refreshed.");
          } catch (e) {
            setFetchMsg(String((e as Error)?.message ?? e));
          }
          view.reload();
        }}
      >
        Refresh inventory
      </ButtonItem>
      {v.inventory && (
        <div style={{ color: colors.muted, fontSize: "12px", padding: "4px 0" }}>
          Synced {ago(v.inventory.syncTime ?? v.inventory.fetchedAt)} · {v.inventory.origin}
        </div>
      )}
    </Focusable>
  );

  let body: ReactNode;
  if (v.rows.length === 0) body = <Empty text={s.search ? "Nothing matches this search." : "Nothing in this tab with these filters."} />;
  else
    body = (
      <>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 64px 70px 56px 52px", gap: "8px", padding: "0 4px 4px", color: colors.muted, fontSize: "11px", textTransform: "uppercase" }}>
          <span>Item</span>
          <span style={{ textAlign: "right" }}>Owned</span>
          <span style={{ textAlign: "right" }}>Plat</span>
          <span style={{ textAlign: "right" }}>Ducats</span>
          <span style={{ textAlign: "right" }}>d/p</span>
        </div>
        <Focusable style={{ display: "flex", flexDirection: "column" }}>
          {v.rows.map((r) => (
            <ItemRow key={r.key} r={r} tab={s.tab} />
          ))}
        </Focusable>
        {pages > 1 && (
          <Focusable style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "10px" }} flow-children="horizontal">
            <DialogButton style={{ minWidth: 0, width: "100px" }} disabled={page === 0} onClick={() => setPage(page - 1)}>
              Prev
            </DialogButton>
            <span style={{ color: colors.muted }}>
              {page + 1} / {pages} · {v.total} items
            </span>
            <DialogButton style={{ minWidth: 0, width: "100px" }} disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
              Next
            </DialogButton>
          </Focusable>
        )}
        {(v.pricesMissing ?? 0) > 0 && (
          <div style={{ color: colors.muted, fontSize: "12px", marginTop: "6px" }}>{v.pricesMissing} prices not cached yet. Use “Fetch prices”.</div>
        )}
      </>
    );

  return (
    <Page>
      {tabStrip}
      <Focusable style={{ display: "flex", gap: "16px", alignItems: "flex-start" }} flow-children="horizontal">
        {filters}
        <div style={{ flex: 1, minWidth: 0 }}>
          {v.value && s.tab !== "resources" && <ValueStrip v={v.value} prefs={prefs} onPrefs={setPrefs} onFetch={fetchPrices} fetchMsg={fetchMsg} />}
          {view.loading && <div style={{ color: colors.muted, fontSize: "12px" }}>Updating…</div>}
          {body}
        </div>
      </Focusable>
    </Page>
  );
};

export default Inventory;

// ---- QAM section ----------------------------------------------------------------------------

export const Qam: FC = () => {
  const st = useRpc<QamView>("inventory.qam", {}, [], 120000);
  const [msg, setMsg] = useState<string>();
  useEvent("inventory.updated", () => st.reload());
  const q = st.data;
  const setPrefs = async (patch: Record<string, unknown>) => {
    await call("inventory.ui_set", patch);
    st.reload();
  };
  const presets = q?.presets ?? [0, 5, 10, 15];
  return (
    <PanelSection title="Inventory value">
      {st.error && !q && (
        <PanelSectionRow>
          <Field label="Error" description={st.error} />
        </PanelSectionRow>
      )}
      {q && !q.hasInventory && (
        <>
          <PanelSectionRow>
            <Field label="No inventory yet" description="Log in to Warframe, or import a JSON file on the Inventory page." focusable />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                setMsg("Refreshing…");
                try {
                  await call("inventory.refresh");
                  setMsg(undefined);
                } catch (e) {
                  setMsg(String((e as Error)?.message ?? e));
                }
                st.reload();
              }}
            >
              Refresh inventory
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
      {q?.hasInventory && q.totals && (
        <>
          <PanelSectionRow>
            <Field
              focusable
              label={q.scope === "prime" ? "Prime parts" : "All tradables"}
              description={`${q.totals.counted} items${q.totals.unpriced ? ` · ${q.totals.unpriced} unpriced` : ""}`}
              childrenContainerWidth="min"
              bottomSeparator="none"
            >
              <div style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <div style={{ color: colors.plat, fontWeight: 700 }}>
                  {q.totals.platinumUnpriced ? "≥" : ""}
                  {fmtNum(q.totals.platinum)}p
                </div>
                {q.scope === "prime" && (
                  <div style={{ color: colors.ducat, fontSize: "13px" }}>
                    {q.totals.ducatsUnpriced ? "≥" : ""}
                    {fmtNum(q.totals.ducats)}d
                  </div>
                )}
              </div>
            </Field>
          </PanelSectionRow>
          {(q.top ?? []).map((t) => (
            <PanelSectionRow key={t.name}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: colors.muted }}>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {t.name} ×{t.amount}
                </span>
                <span style={{ color: colors.plat, flexShrink: 0 }}>{Math.round(t.platinum)}p</span>
              </div>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <ToggleField label="Count all tradables" checked={q.scope === "tradable"} onChange={(b) => setPrefs({ value_scope: b ? "tradable" : "prime" })} />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField
              label="Min price per unit"
              value={Math.max(0, presets.indexOf(q.minPlat))}
              min={0}
              max={presets.length - 1}
              step={1}
              notchCount={presets.length}
              notchLabels={presets.map((p, i) => ({ notchIndex: i, label: p === 0 ? "Any" : `${p}p`, value: i }))}
              notchTicksVisible
              onChange={(i) => setPrefs({ value_min_plat: presets[i] ?? 0 })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                const r = await call<{ queued: number }>("inventory.prefetch_prices", { tab: q.scope === "prime" ? "prime" : "everything" });
                setMsg(r.queued > 0 ? `Fetching ${r.queued} prices…` : "Prices are cached.");
                if (r.queued > 0) setTimeout(() => st.reload(), 15000);
              }}
            >
              Fetch missing prices
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
      {msg && (
        <PanelSectionRow>
          <div style={{ color: colors.muted, fontSize: "12px" }}>{msg}</div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => openPage("inventory")}>
          Open Inventory
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};
