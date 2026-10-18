// Market (docs/port/menus.md §9): browse an item's price and top online orders (whisper text
// to copy), recently viewed items, and the warframe.market account: sign-in, my orders
// (sold one / edit / hide / delete) and the online status. All network work is in the
// backend (menus/market.py, data/wfm_auth.py); this file only holds UI state.
import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  DropdownItem,
  Field,
  Focusable,
  ModalRoot,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  showModal,
} from "@decky/ui";
import { FC, ReactNode, useEffect, useRef, useState } from "react";

import { Badge, Empty, ErrorText, Grid, Loading, Page, Plat, Row, Section, Segmented, Stat, card, colors } from "../components/ui";
import { openTab } from "../nav";
import { ROUTE } from "../nav";
import { call, useEvent, useRpc } from "../rpc";

// ---- types (menus/market.py) -----------------------------------------------------------------

interface PriceSummary {
  plat: number | null;
  median: number | null;
  lowSell: number | null;
  highBuy: number | null;
  volume: number | null;
  sellers: number;
  source: "top" | "stats";
  fetchedAt: number;
  ageS: number;
}

interface OrderRow {
  user: string;
  status: string;
  platinum: number;
  unit: number;
  quantity: number;
  perTrade: number;
  rank: number | null;
  reputation: number | null;
  whisper: string;
}

interface Detail {
  slug: string;
  name: string;
  rank: number | null;
  maxRank: number | null;
  owned: number | null;
  url: string;
  price: PriceSummary | null;
  sell: OrderRow[];
  buy: OrderRow[];
  offline: boolean;
  excluded: boolean;
  error?: string;
}

interface SearchHit {
  slug: string;
  name: string;
  max_rank: number | null;
}

interface RecentRow {
  slug: string;
  name: string;
  rank: number | null;
  at: number;
  price: PriceSummary | null;
  fresh: boolean;
}

type WfmStatus = "ingame" | "online" | "invisible";

interface Account {
  signedIn: boolean;
  userName: string | null;
  platform: string;
  slug: string | null;
  status: WfmStatus | null;
  statusUntil: string | null;
  persisted: boolean;
  profileUrl: string | null;
  holdMin: number;
  holdChoices: number[];
  error?: string;
}

interface MyOrder {
  id: string;
  type: "sell" | "buy";
  platinum: number;
  quantity: number;
  perTrade: number;
  visible: boolean;
  rank: number | null;
  subtype: string | null;
  name: string;
  slug: string | null;
  maxRank: number | null;
  owned: number | null;
}

interface Counts {
  sell: number;
  buy: number;
  total: number;
  visible: number;
  sellPlat: number;
}

interface OrdersPayload {
  orders: MyOrder[];
  counts: Counts;
  fetchedAt: number | null;
  error: string | null;
  signedIn: boolean;
}

interface WriteResult extends Partial<OrdersPayload> {
  ok: boolean;
  error?: string;
  code?: string;
  closed?: number;
  changed?: number;
  failed?: { id: string; name: string; error: string }[];
}

interface QamView {
  recent: RecentRow[];
  account: Account;
  counts: Counts | null;
  ordersError: string | null;
  offline: boolean;
}

// ---- helpers ---------------------------------------------------------------------------------

type Tab = "browse" | "orders" | "account";

// Survive tab switches and QAM -> page jumps.
let lastTab: Tab = "browse";
let lastItem: { slug: string; rank: number | null } | null = null;

export function openMarket(tab: Tab = "browse", item?: { slug: string; rank: number | null }): void {
  lastTab = tab;
  if (item) lastItem = item;
  // A mounted Market page read lastTab/lastItem only at mount; its listener applies them now.
  window.dispatchEvent(new CustomEvent("wfh-market-tab", { detail: tab }));
  openTab("market");
  Navigation.Navigate(ROUTE);
  Navigation.CloseSideMenus();
}

const STATUS_LABEL: Record<string, string> = { ingame: "In game", online: "Online", invisible: "Invisible", offline: "Offline" };
const STATUS_COLOR: Record<string, string> = { ingame: colors.good, online: colors.accent, invisible: colors.muted, offline: colors.muted };

const holdLabel = (m: number) => (m === 0 ? "Until changed" : m < 60 ? `${m} min` : `${m / 60} h`);

// Reload a recent-items list when a market.price event lands for one of its rows, at most once
// a second. Other features prefetch hundreds of slugs; those events are ignored here.
function usePriceReload(rows: RecentRow[] | undefined, reload: () => void): void {
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  useEvent("market.price", (e: { slug?: string; rank?: number | null }) => {
    if (!rows?.some((r) => r.slug === e?.slug && (r.rank ?? null) === (e?.rank ?? null))) return;
    if (timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = undefined;
      reload();
    }, 1000);
  });
}

function ago(s: number): string {
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

// Steam's CEF: navigator.clipboard when allowed, else the execCommand fallback.
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

const StatusDot: FC<{ status: string }> = ({ status }) => (
  <span style={{ display: "inline-block", width: "8px", height: "8px", borderRadius: "4px", marginRight: "6px", background: STATUS_COLOR[status] ?? colors.muted }} />
);

const SmallButton: FC<{ onClick: () => void; disabled?: boolean; children: ReactNode; width?: string }> = ({ onClick, disabled, children, width }) => (
  <DialogButton disabled={disabled} onClick={onClick} style={{ minWidth: 0, width: width ?? "auto", padding: "6px 14px", flex: "0 0 auto" }}>
    {children}
  </DialogButton>
);

function confirm(title: string, body: string, ok: string, onOK: () => void, destructive = false): void {
  showModal(<ConfirmModal strTitle={title} strDescription={body} strOKButtonText={ok} bDestructiveWarning={destructive} onOK={onOK} />);
}

// ---- whisper modal ---------------------------------------------------------------------------

const WhisperModal: FC<{ row: OrderRow; side: "sell" | "buy"; closeModal?: () => void }> = ({ row, side, closeModal }) => {
  const [msg, setMsg] = useState<string>();
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text }}>
        <div style={{ fontSize: "18px", fontWeight: 600, marginBottom: "6px" }}>
          <StatusDot status={row.status} />
          {row.user}
          <span style={{ color: colors.muted, fontSize: "13px", marginLeft: "8px" }}>
            {side === "sell" ? "sells" : "buys"} · {STATUS_LABEL[row.status] ?? row.status}
            {row.reputation != null && ` · rep ${row.reputation}`}
          </span>
        </div>
        <div style={{ marginBottom: "10px" }}>
          <Plat value={row.platinum} />
          {row.perTrade > 1 ? ` for ${row.perTrade}` : ""} · {row.quantity} listed{row.rank != null ? ` · rank ${row.rank}` : ""}
        </div>
        <div style={{ fontSize: "12px", color: colors.muted, marginBottom: "4px" }}>Whisper (type or paste it in the in-game chat)</div>
        <Focusable
          focusClassName="gpfocus"
          onActivate={async () => setMsg((await copyText(row.whisper)) ? "Copied to the clipboard." : "Copy is not available here.")}
          style={{ padding: "10px", background: "rgba(0,0,0,0.35)", border: `1px solid ${colors.border}`, borderRadius: "4px", fontFamily: "monospace", fontSize: "14px", wordBreak: "break-word", userSelect: "text" }}
        >
          {row.whisper}
        </Focusable>
        {msg && <div style={{ marginTop: "6px", color: colors.good }}>{msg}</div>}
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", marginTop: "12px" }}>
          <SmallButton width="160px" onClick={async () => setMsg((await copyText(row.whisper)) ? "Copied to the clipboard." : "Copy is not available here.")}>
            Copy whisper
          </SmallButton>
          <SmallButton width="120px" onClick={() => closeModal?.()}>
            Close
          </SmallButton>
        </Focusable>
      </div>
    </ModalRoot>
  );
};

// ---- browse ----------------------------------------------------------------------------------

// One column of orders. Its own vertical Focusable so the two tables side by side form a
// horizontal pair: up/down moves within a table, left/right jumps between them.
const OrderTable: FC<{ title: string; side: "sell" | "buy"; rows: OrderRow[] }> = ({ title, side, rows }) => (
  <Focusable style={{ flex: 1, minWidth: 0 }}>
    <Section title={`${title} · ${rows.length}`} style={{ margin: 0 }}>
      {rows.length === 0 && <div style={{ color: colors.muted, padding: "4px 0" }}>No online orders.</div>}
      {rows.map((r, i) => (
        <Row
          key={`${r.user}-${i}`}
          onActivate={() => showModal(<WhisperModal row={r} side={side} />)}
          label={
            <>
              <StatusDot status={r.status} />
              {r.user}
            </>
          }
          sub={`${STATUS_LABEL[r.status] ?? r.status} · qty ${r.quantity}${r.perTrade > 1 ? ` (${r.perTrade}/trade)` : ""}${r.rank != null ? ` · R${r.rank}` : ""}${r.reputation != null ? ` · rep ${r.reputation}` : ""}`}
          value={<Plat value={r.platinum} />}
        />
      ))}
    </Section>
  </Focusable>
);

const ItemCard: FC<{ name: ReactNode; sub?: string; value?: ReactNode; selected?: boolean; color?: string; onActivate: () => void }> = ({ name, sub, value, selected, color, onActivate }) => (
  <Focusable
    focusClassName="gpfocus"
    onActivate={onActivate}
    style={{
      ...card,
      border: `1px solid ${selected ? colors.accent : colors.border}`,
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      gap: "8px",
      minWidth: 0,
      minHeight: "44px",
      boxSizing: "border-box",
    }}
  >
    <div style={{ minWidth: 0 }}>
      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: selected ? colors.accent : color ?? colors.text }}>{name}</div>
      {sub && <div style={{ fontSize: "12px", color: colors.muted }}>{sub}</div>}
    </div>
    {value !== undefined && <div style={{ flexShrink: 0 }}>{value}</div>}
  </Focusable>
);

const ItemDetail: FC<{ slug: string; rank: number | null; onRank: (r: number | null) => void; onLoaded: () => void }> = ({ slug, rank, onRank, onLoaded }) => {
  const [d, setD] = useState<Detail>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const loadedKey = useRef<string | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    setBusy(true);
    // first load of an item records it in the recent list; Refresh and the 45 s tick force a
    // fresh order-book read (backend rate-limits it) and leave the recent list alone
    const key = `${slug}|${rank}`;
    const first = loadedKey.current !== key;
    loadedKey.current = key;
    call<Detail>("market.detail", first ? { slug, rank } : { slug, rank, force: true, record: false })
      .then((v) => {
        if (!alive) return;
        setD(v);
        setErr(v.error);
        onLoaded();
      })
      .catch((e) => alive && setErr(String(e?.message ?? e)))
      .finally(() => alive && setBusy(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, rank, tick]);
  // MarketBrowseView auto refresh (45 s): a forced read of the top orders.
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 45000);
    return () => clearInterval(t);
  }, []);
  if (!d) return err ? <ErrorText error={err} /> : <Loading />;
  const p = d.price;
  return (
    <>
      <Section
        title={d.name}
        right={
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px" }}>
            <SmallButton disabled={busy} onClick={() => setTick((n) => n + 1)}>
              {busy ? "Loading…" : "Refresh"}
            </SmallButton>
          </Focusable>
        }
      >
        {err && <ErrorText error={err} />}
        {d.excluded && <div style={{ color: colors.warn }}>warframe.market does not price this item.</div>}
        {p ? (
          <div>
            <Stat label="Price" value={<Plat value={p.plat} />} />
            <Stat label="Lowest sell" value={<Plat value={p.lowSell} />} />
            <Stat label="Highest buy" value={<Plat value={p.highBuy} />} />
            {p.source === "top" ? <Stat label="Sellers online" value={p.sellers} /> : <Stat label="48 h volume" value={p.volume ?? "–"} />}
            {d.owned != null && <Stat label="Owned" value={d.owned} />}
          </div>
        ) : (
          <div style={{ color: colors.muted }}>{d.offline ? "warframe.market is unreachable; no cached price." : "No price on warframe.market."}</div>
        )}
        <div style={{ fontSize: "12px", color: colors.muted, marginTop: "4px" }}>
          {p ? `${p.source === "top" ? "Median of the 5 cheapest online sellers" : "48 h trade average"} · updated ${ago(p.ageS)}` : ""}
          {d.offline && <span style={{ color: colors.warn }}> · offline, cached</span>}
        </div>
        {d.maxRank ? (
          <div style={{ marginTop: "6px" }}>
            <Segmented<string>
              value={rank == null ? "r0" : "max"}
              options={[
                { id: "r0", label: "Unranked" },
                { id: "max", label: `Rank ${d.maxRank}` },
              ]}
              onChange={(v) => onRank(v === "max" ? d.maxRank : null)}
            />
          </div>
        ) : null}
      </Section>
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "12px", alignItems: "flex-start" }}>
        <OrderTable title="Sellers (you buy)" side="sell" rows={d.sell} />
        <OrderTable title="Buyers (you sell)" side="buy" rows={d.buy} />
      </Focusable>
    </>
  );
};

const Browse: FC = () => {
  const [draft, setDraft] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [sel, setSel] = useState(lastItem);
  const recent = useRpc<RecentRow[]>("market.recent", {}, []);
  usePriceReload(recent.data, recent.reload);
  const seq = useRef(0);
  useEffect(() => {
    const q = draft.trim();
    if (q.length < 2) {
      setHits([]);
      return;
    }
    const t = setTimeout(async () => {
      const n = ++seq.current;
      setSearching(true);
      try {
        const r = await call<SearchHit[]>("market.search", { query: q, limit: 8 });
        if (n === seq.current) setHits(r);
      } catch {
        if (n === seq.current) setHits([]);
      } finally {
        if (n === seq.current) setSearching(false);
      }
    }, 300);
    return () => clearTimeout(t);
  }, [draft]);
  const pick = (slug: string, rank: number | null) => {
    lastItem = { slug, rank };
    setSel(lastItem);
  };
  const q = draft.trim();
  return (
    <>
      <Section
        title="Find an item"
        right={
          recent.data && recent.data.length > 0 ? (
            <SmallButton
              onClick={() =>
                confirm("Clear recent items?", "Removes every item from the recently viewed list.", "Clear", async () => {
                  await call("market.recent_clear");
                  recent.reload();
                })
              }
            >
              Clear recent
            </SmallButton>
          ) : undefined
        }
      >
        <TextField label="Item name (2+ letters; A opens the keyboard)" value={draft} onChange={(e) => setDraft(e.target.value)} />
        {q.length >= 2 && (
          <>
            <div style={{ fontSize: "12px", color: colors.muted, marginTop: "6px", textTransform: "uppercase" }}>
              {searching ? "Searching…" : hits.length === 0 ? "No match." : "Matches"}
            </div>
            <Grid style={{ marginTop: "8px" }}
              items={hits}
              cols={4}
              rows={2}
              render={(h) => (
                <ItemCard key={h.slug} name={h.name} sub={h.max_rank ? `Rank 0-${h.max_rank}` : undefined} selected={sel?.slug === h.slug} onActivate={() => pick(h.slug, null)} />
              )}
            />
          </>
        )}
        <div style={{ fontSize: "12px", color: colors.muted, marginTop: "8px", textTransform: "uppercase" }}>Recently viewed</div>
        {recent.error && <ErrorText error={recent.error} />}
        {recent.data?.length === 0 && <div style={{ color: colors.muted, padding: "4px 0" }}>Items you look up appear here.</div>}
        {recent.data && (
          <Grid style={{ marginTop: "8px" }}
            items={recent.data}
            cols={4}
            rows={2}
            render={(r) => (
              <ItemCard
                key={`${r.slug}-${r.rank}`}
                name={r.name}
                sub={r.rank != null ? `Rank ${r.rank}` : undefined}
                selected={sel?.slug === r.slug && sel?.rank === r.rank}
                value={<Plat value={r.price?.plat} />}
                onActivate={() => pick(r.slug, r.rank)}
              />
            )}
          />
        )}
      </Section>
      {sel ? (
        <ItemDetail key={sel.slug} slug={sel.slug} rank={sel.rank} onRank={(r) => pick(sel.slug, r)} onLoaded={() => recent.reload()} />
      ) : (
        <Empty text="Search an item or pick a recent one to see its price and the best online orders. Press A on an order for the whisper." />
      )}
    </>
  );
};

// ---- my orders -------------------------------------------------------------------------------

const OrderModal: FC<{ o: MyOrder; onDone: (r: WriteResult) => void; closeModal?: () => void }> = ({ o, onDone, closeModal }) => {
  const [price, setPrice] = useState(String(o.platinum));
  const [qty, setQty] = useState(String(o.quantity));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const run = async (method: string, params: Record<string, unknown>) => {
    setBusy(true);
    setErr(undefined);
    try {
      const r = await call<WriteResult>(method, params);
      onDone(r);
      if (r.ok) closeModal?.();
      else setErr(r.error ?? "Failed.");
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  const tradeSize = Math.min(o.perTrade || 1, o.quantity);
  const newPrice = parseInt(price, 10);
  const newQty = parseInt(qty, 10);
  const changed = (newPrice > 0 && newPrice !== o.platinum) || (newQty > 0 && newQty !== o.quantity);
  return (
    <ModalRoot closeModal={closeModal} onCancel={closeModal}>
      <div style={{ color: colors.text }}>
        <div style={{ fontSize: "18px", fontWeight: 600 }}>
          {o.name}
          <Badge text={o.type === "sell" ? "WTS" : "WTB"} color={o.type === "sell" ? colors.good : colors.warn} />
          {!o.visible && <Badge text="hidden" color={colors.muted} />}
        </div>
        <div style={{ color: colors.muted, marginBottom: "8px" }}>
          <Plat value={o.platinum} /> · {o.quantity} listed{o.perTrade > 1 ? ` · ${o.perTrade} per trade` : ""}
          {o.rank != null ? ` · rank ${o.rank}` : ""}
          {o.owned != null ? ` · you own ${o.owned}` : ""}
        </div>
        {err && <ErrorText error={err} />}
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "8px" }}>
          <SmallButton
            disabled={busy}
            onClick={() =>
              confirm(
                o.type === "sell" ? "Mark as sold?" : "Mark as bought?",
                `Closes ${tradeSize > 1 ? `${tradeSize} of` : "one"} ${o.name} at ${o.platinum}p on warframe.market (${o.quantity - tradeSize} left).`,
                o.type === "sell" ? `Sold ${tradeSize}` : `Bought ${tradeSize}`,
                () => run("market.order_close", { id: o.id }),
              )
            }
          >
            {o.type === "sell" ? `Sold ${tradeSize}` : `Bought ${tradeSize}`}
          </SmallButton>
          <SmallButton disabled={busy} onClick={() => run("market.order_visible", { id: o.id, visible: !o.visible })}>
            {o.visible ? "Hide" : "Show"}
          </SmallButton>
          <SmallButton
            disabled={busy}
            onClick={() => confirm("Delete order?", `Removes your ${o.type} order for ${o.name}.`, "Delete", () => run("market.order_delete", { id: o.id }), true)}
          >
            Delete
          </SmallButton>
        </Focusable>
        <TextField label="Price (platinum)" mustBeNumeric value={price} onChange={(e) => setPrice(e.target.value)} />
        <TextField label="Quantity" mustBeNumeric value={qty} onChange={(e) => setQty(e.target.value)} />
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
          <SmallButton
            disabled={busy || !changed}
            onClick={() =>
              run("market.order_update", {
                id: o.id,
                platinum: newPrice > 0 && newPrice !== o.platinum ? newPrice : null,
                quantity: newQty > 0 && newQty !== o.quantity ? newQty : null,
              })
            }
          >
            Save price / quantity
          </SmallButton>
          {o.slug && (
            <SmallButton
              onClick={() => {
                lastItem = { slug: o.slug!, rank: o.rank && o.maxRank && o.rank === o.maxRank ? o.rank : null };
                lastTab = "browse";
                closeModal?.();
                window.dispatchEvent(new CustomEvent("wfh-market-tab", { detail: "browse" }));
              }}
            >
              Market prices
            </SmallButton>
          )}
          <SmallButton onClick={() => closeModal?.()}>Close</SmallButton>
        </Focusable>
      </div>
    </ModalRoot>
  );
};

const MyOrders: FC<{ account: Account | undefined; goAccount: () => void }> = ({ account, goAccount }) => {
  const [data, setData] = useState<OrdersPayload>();
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState<"all" | "sell" | "buy" | "hidden">("all");
  const [msg, setMsg] = useState<string>();
  const load = async (force: boolean) => {
    setBusy(true);
    try {
      const r = await call<OrdersPayload>("market.my_orders", { force });
      setData(r);
      setErr(r.error ?? undefined);
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  const signedIn = !!account?.signedIn;
  useEffect(() => {
    if (signedIn) load(false);
  }, [signedIn]);
  // ORDERS_POLL_MS 30 s while the page is open.
  useEffect(() => {
    if (!signedIn) return;
    const t = setInterval(() => load(false), 30000);
    return () => clearInterval(t);
  }, [signedIn]);
  useEvent("market.orders", (p: OrdersPayload) => {
    setData(p);
    setErr(p.error ?? undefined);
  });
  const apply = (r: WriteResult) => {
    if (r.orders) setData(r as OrdersPayload);
    if (!r.ok) setMsg(r.error);
    else if (r.failed && r.failed.length) setMsg(`${r.failed.length} order(s) failed: ${r.failed[0].error}`);
    else setMsg(undefined);
  };
  if (!account) return <Loading />;
  if (!signedIn)
    return (
      <Section title="My orders">
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "16px", alignItems: "center" }}>
          <div style={{ color: colors.muted, flex: 1 }}>Sign in to warframe.market to see and manage your orders.</div>
          <SmallButton onClick={goAccount}>Sign in</SmallButton>
        </Focusable>
      </Section>
    );
  const rows = (data?.orders ?? []).filter((o) => (filter === "all" ? true : filter === "hidden" ? !o.visible : o.type === filter));
  const c = data?.counts;
  return (
    <>
      <Section
        title={`My orders${account.userName ? ` · ${account.userName}` : ""}`}
        right={
          <Focusable flow-children="horizontal" style={{ display: "flex", gap: "6px" }}>
            <SmallButton disabled={busy} onClick={() => load(true)}>
              {busy ? "Loading…" : "Refresh"}
            </SmallButton>
            <SmallButton
              disabled={busy || !c || c.visible === c.total}
              onClick={async () => apply(await call<WriteResult>("market.orders_visible", { visible: true }))}
            >
              Show all
            </SmallButton>
            <SmallButton
              disabled={busy || !c || c.visible === 0}
              onClick={() =>
                confirm("Hide every order?", "Sets all your orders invisible on warframe.market.", "Hide all", async () =>
                  apply(await call<WriteResult>("market.orders_visible", { visible: false })),
                )
              }
            >
              Hide all
            </SmallButton>
          </Focusable>
        }
      >
        {c && (
          <div>
            <Stat label="Selling" value={c.sell} color={colors.good} />
            <Stat label="Buying" value={c.buy} color={colors.warn} />
            <Stat label="Visible" value={`${c.visible}/${c.total}`} />
            <Stat label="Listed value" value={<Plat value={c.sellPlat} />} />
          </div>
        )}
        {err && <ErrorText error={err} />}
        {msg && <ErrorText error={msg} />}
        <Segmented
          style={{ marginTop: "8px" }}
          value={filter}
          options={[
            { id: "all", label: "All" },
            { id: "sell", label: "Selling" },
            { id: "buy", label: "Buying" },
            { id: "hidden", label: "Hidden" },
          ]}
          onChange={setFilter}
        />
      </Section>
      {!data && <Loading />}
      {data && rows.length === 0 && <Empty text="No orders here." />}
      {rows.length > 0 && (
        <Section>
          <Grid style={{ marginTop: "8px" }}
            items={rows}
            cols={2}
            rows={10}
            render={(o) => (
              <ItemCard
                key={o.id}
                color={o.visible ? undefined : colors.muted}
                onActivate={() => showModal(<OrderModal o={o} onDone={apply} />)}
                name={
                  <>
                    {o.name}
                    <Badge text={o.type === "sell" ? "WTS" : "WTB"} color={o.type === "sell" ? colors.good : colors.warn} />
                    {!o.visible && <Badge text="hidden" color={colors.muted} />}
                  </>
                }
                sub={`${o.quantity} listed${o.perTrade > 1 ? ` · ${o.perTrade}/trade` : ""}${o.rank != null ? ` · R${o.rank}` : ""}${o.owned != null ? ` · own ${o.owned}` : ""}`}
                value={<Plat value={o.platinum} />}
              />
            )}
          />
        </Section>
      )}
    </>
  );
};

// ---- account ---------------------------------------------------------------------------------

const StatusControls: FC<{ account: Account; onChange: (a: Account) => void; compact?: boolean }> = ({ account, onChange, compact }) => {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const set = async (status: WfmStatus, hold?: number) => {
    setBusy(true);
    setErr(undefined);
    try {
      const r = await call<Account>("market.set_status", { status, hold_min: hold ?? account.holdMin });
      onChange(r);
      if (r.error) setErr(r.error);
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  // no status known yet (profile lookup failed): remember the hold for the next status change
  const saveHold = async (hold: number) => {
    setBusy(true);
    setErr(undefined);
    try {
      onChange(await call<Account>("market.set_hold", { hold_min: hold }));
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <DropdownItem
        label="Status"
        description={compact ? undefined : account.statusUntil ? `Until ${new Date(account.statusUntil).toLocaleTimeString()}` : "Shown to buyers and sellers on warframe.market"}
        disabled={busy}
        rgOptions={(["ingame", "online", "invisible"] as WfmStatus[]).map((s) => ({ data: s, label: STATUS_LABEL[s] }))}
        selectedOption={account.status ?? undefined}
        onChange={(o) => set(o.data as WfmStatus)}
      />
      {!compact && (
        <DropdownItem
          label="Hold status for"
          description={account.status ? "warframe.market resets it after this time" : "Used for your next status change"}
          disabled={busy}
          rgOptions={account.holdChoices.map((m) => ({ data: m, label: holdLabel(m) }))}
          selectedOption={account.holdMin}
          onChange={(o) => (account.status ? set(account.status, o.data as number) : saveHold(o.data as number))}
        />
      )}
      {err && <div style={{ gridColumn: "1 / -1" }}><ErrorText error={err} /></div>}
    </>
  );
};

const AccountPanel: FC<{ account: Account | undefined; onChange: (a: Account) => void }> = ({ account, onChange }) => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  if (!account) return <Loading />;
  if (!account.signedIn)
    return (
      <Section title="Sign in to warframe.market">
        <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "16px" }}>
          <TextField label="E-mail" value={email} onChange={(e) => setEmail(e.target.value)} />
          <TextField label="Password" bIsPassword value={password} onChange={(e) => setPassword(e.target.value)} />
        </Focusable>
        {err && <ErrorText error={err} />}
        <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
          <SmallButton
            disabled={busy || !email || !password}
            onClick={async () => {
              setBusy(true);
              setErr(undefined);
              try {
                const r = await call<Account>("market.sign_in", { email, password });
                if (r.error) setErr(r.error);
                else {
                  setPassword("");
                  onChange(r);
                }
              } catch (e: any) {
                setErr(String(e?.message ?? e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </SmallButton>
        </Focusable>
        <div style={{ fontSize: "12px", color: colors.muted, marginTop: "10px", lineHeight: 1.4 }}>
          Your password goes to warframe.market once and is never saved. Only the session token is kept, in the plugin's settings folder on this Deck
          (readable by root only). Accounts that sign in with Discord, Google or another provider need a warframe.market password set first.
        </div>
      </Section>
    );
  return (
    <Section title="warframe.market account">
      <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "0 16px" }}>
        <Field label="Signed in as" focusable>
          {account.userName ?? "?"}
        </Field>
        <Field label="Platform" focusable>
          {account.platform.toUpperCase()}
        </Field>
      </Focusable>
      <Focusable flow-children="horizontal" style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "0 16px" }}>
        <StatusControls account={account} onChange={onChange} />
      </Focusable>
      {!account.persisted && <div style={{ color: colors.warn, fontSize: "12px" }}>The session could not be saved; you will need to sign in again after a restart.</div>}
      <Focusable flow-children="horizontal" style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
        <SmallButton onClick={async () => onChange(await call<Account>("market.account", { refresh: true }))}>Refresh status</SmallButton>
        <SmallButton
          onClick={() =>
            confirm("Sign out?", "Forgets the warframe.market session on this Deck.", "Sign out", async () => onChange(await call<Account>("market.sign_out")))
          }
        >
          Sign out
        </SmallButton>
      </Focusable>
    </Section>
  );
};

// ---- page ------------------------------------------------------------------------------------

const Market: FC = () => {
  const [tab, setTabState] = useState<Tab>(lastTab);
  const setTab = (t: Tab) => {
    lastTab = t;
    setTabState(t);
  };
  const [account, setAccount] = useState<Account>();
  const [jump, bump] = useState(0);
  useEffect(() => {
    call<Account>("market.account", { refresh: true })
      .then(setAccount)
      .catch(() => call<Account>("market.account").then(setAccount));
  }, []);
  useEvent("market.account", (a: Account) => setAccount(a));
  useEffect(() => {
    const h = (e: Event) => {
      setTab((e as CustomEvent).detail as Tab);
      bump((n) => n + 1);
    };
    window.addEventListener("wfh-market-tab", h);
    return () => window.removeEventListener("wfh-market-tab", h);
  }, []);
  return (
    <Page>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "8px 0" }}>
        <Segmented<Tab>
          value={tab}
          options={[
            { id: "browse", label: "Prices" },
            { id: "orders", label: "My orders" },
            { id: "account", label: "Account" },
          ]}
          onChange={setTab}
        />
        <div style={{ color: colors.muted, fontSize: "13px" }}>
          {account?.signedIn ? (
            <>
              <StatusDot status={account.status ?? "offline"} />
              {account.userName} · {STATUS_LABEL[account.status ?? "offline"] ?? account.status}
            </>
          ) : (
            "Not signed in"
          )}
        </div>
      </div>
      {tab === "browse" && <Browse key={jump} />}
      {tab === "orders" && <MyOrders account={account} goAccount={() => setTab("account")} />}
      {tab === "account" && <AccountPanel account={account} onChange={setAccount} />}
    </Page>
  );
};

export default Market;

// ---- QAM: quick prices for recent items + my order count ---------------------------------------

export const Qam: FC = () => {
  const q = useRpc<QamView>("market.qam", {}, [], 60000);
  usePriceReload(q.data?.recent, q.reload);
  useEvent("market.orders", () => q.reload());
  useEvent("market.account", () => q.reload());
  const d = q.data;
  return (
    <PanelSection title="Market">
      {q.error && !d && (
        <PanelSectionRow>
          <Field label="Error" description={q.error} />
        </PanelSectionRow>
      )}
      {!d && !q.error && (
        <PanelSectionRow>
          <Field label="Loading…" />
        </PanelSectionRow>
      )}
      {d && (
        <>
          <PanelSectionRow>
            {d.account.signedIn ? (
              <Field
                focusable
                padding="compact"
                label={d.account.userName ?? "warframe.market"}
                description={d.counts ? `${d.counts.sell} selling · ${d.counts.buy} buying${d.counts.visible < d.counts.total ? ` · ${d.counts.total - d.counts.visible} hidden` : ""}` : d.ordersError ?? "Loading orders…"}
                onActivate={() => openMarket("orders")}
              >
                <span style={{ color: STATUS_COLOR[d.account.status ?? "offline"], whiteSpace: "nowrap" }}>{STATUS_LABEL[d.account.status ?? "offline"]}</span>
              </Field>
            ) : (
              <Field focusable padding="compact" label="warframe.market" description="Not signed in" onActivate={() => openMarket("account")} />
            )}
          </PanelSectionRow>
          {d.account.signedIn && (
            <PanelSectionRow>
              <StatusControls compact account={d.account} onChange={() => q.reload()} />
            </PanelSectionRow>
          )}
          {d.offline && (
            <PanelSectionRow>
              <div style={{ color: colors.warn, fontSize: "12px" }}>warframe.market unreachable: cached prices</div>
            </PanelSectionRow>
          )}
          {d.recent.length === 0 && (
            <PanelSectionRow>
              <Field label="No recent items" description="Look an item up on the Market page." />
            </PanelSectionRow>
          )}
          {d.recent.map((r) => (
            <PanelSectionRow key={`${r.slug}-${r.rank}`}>
              <Field
                focusable
                padding="compact"
                label={r.name}
                description={
                  [r.rank != null ? `Rank ${r.rank}` : null, r.price ? `low ${r.price.lowSell ?? "–"}p · buy ${r.price.highBuy ?? "–"}p` : null]
                    .filter(Boolean)
                    .join(" · ") || undefined
                }
                onActivate={() => openMarket("browse", { slug: r.slug, rank: r.rank })}
              >
                <span style={{ color: r.fresh ? colors.plat : colors.muted, whiteSpace: "nowrap" }}>{r.price?.plat != null ? `${r.price.plat}p` : "…"}</span>
              </Field>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => openMarket("browse")}>
              Look up an item
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};
