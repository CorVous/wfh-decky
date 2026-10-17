"""Stats page / QAM: daily resource deltas and the trade ledger (docs/port/menus.md §12-13).

Port of WFHelper services/statsTracker.ts (per-day baselines from inventory payloads,
relics opened, trades per day; config/shared/statsTypes.ts resource catalog) and the
EE.log trade dialog parser of services/eeLogMonitor.ts (_parseTradeDialog +
services/tradeLogSanitize.ts) feeding a small ledger (services/tradeTracker.ts, live log
only: no GDPR import, no WFM order matching).

Day keys are local time ("YYYY-MM-DD"). delta = current - first reading of the day (plus a
resumed delta after a worker restart); a payload that does not report a resource keeps the
previous reading. Relics opened = sum of decreases of the total relic count during the day.

Files (runtime dir): stats-history.json {schemaVersion: 2, entries: [...]} (365 days),
stats-trades.json {schemaVersion: 1, trades: [...]} (newest MAX_TRADES).

RPCs
  stats.view(days)              today + per-day series for the chart resources, trade summary
  stats.qam()                   today's deltas for plat/ducats/credits/endo/kuva (memory only)
  stats.trades(offset, limit)   ledger page, newest first
  stats.trade_delete(id)
  stats.configure(chart?, days?, trade_tracking?)
  stats.status()
Event: stats.updated {date} after an inventory snapshot or a trade is recorded.
Settings: stats.chart (resource ids), stats.days, stats.trade_tracking.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import date, timedelta
from typing import Any, Callable

from .. import log, rpc
from ..eelog import FILE

_log = log.get("menus.stats")

HISTORY_FILE = "stats-history.json"
TRADES_FILE = "stats-trades.json"
HISTORY_SCHEMA = 2
HISTORY_MAX_DAYS = 365
MAX_TRADES = 2000
MAX_TRADE_ITEMS = 12
TRADE_DEDUPE_S = 30.0
TRADE_DIALOG_TIMEOUT_S = 60.0
DAY_CHOICES = (7, 14, 30, 90)

MISC = "/Lotus/Types/Items/MiscItems/"
# (id, label, (kind, key), compact) verbatim ids/sources from config/shared/statsTypes.ts.
RESOURCES: list[tuple[str, str, tuple[str, str], bool]] = [
    ("plat", "Platinum", ("field", "PremiumCredits"), False),
    ("ducats", "Ducats", ("misc", MISC + "PrimeBucks"), False),
    ("aya", "Aya", ("misc", MISC + "SchismKey"), False),
    ("credits", "Credits", ("field", "RegularCredits"), True),
    ("endo", "Endo", ("field", "FusionPoints"), True),
    ("vitus", "Vitus Essence", ("misc", MISC + "Elitium"), False),
    ("kuva", "Kuva", ("misc", MISC + "Kuva"), True),
    ("regalAya", "Regal Aya", ("field", "PrimeTokens"), False),
    ("voidTraces", "Void Traces", ("misc", MISC + "VoidTearDrop"), False),
    ("steelEssence", "Steel Essence", ("misc", MISC + "SteelEssence"), False),
    ("rivenSliver", "Riven Slivers", ("misc", MISC + "RivenFragment"), False),
    ("vosfor", "Vosfor", ("misc", MISC + "DistillPoints"), True),
    ("nitain", "Nitain Extract", ("misc", MISC + "Alertium"), False),
    ("forma", "Forma", ("misc", MISC + "Forma"), False),
    ("argonCrystal", "Argon Crystal", ("misc", MISC + "ArgonCrystal"), False),
    ("orokinCell", "Orokin Cell", ("misc", MISC + "OrokinCell"), False),
    ("tellurium", "Tellurium", ("misc", MISC + "Tellurium"), False),
    ("somaticFibers", "Somatic Fibers", ("misc", MISC + "MemoryCryptoFragment"), False),
    ("hexenon", "Hexenon", ("misc", MISC + "ConcentratedGas"), False),
    ("narmerIsoplast", "Narmer Isoplast", ("misc", MISC + "NarmerBountyResource"), False),
    ("cetusWisp", "Cetus Wisp", ("misc", "/Lotus/Types/Gameplay/Eidolon/Resources/CetusWispItem"), False),
    ("pathosClamp", "Pathos Clamp", ("misc", "/Lotus/Types/Gameplay/Duviri/Resource/DuviriDragonDropItem"), False),
]
RESOURCE_IDS = [r[0] for r in RESOURCES]
LABELS = {r[0]: r[1] for r in RESOURCES}
COMPACT = {r[0] for r in RESOURCES if r[3]}
DEFAULT_CHART = ["plat", "ducats", "aya", "credits", "endo", "vitus", "kuva"]
QAM_IDS = ["plat", "ducats", "credits", "endo", "kuva"]


def day_key(ts: float) -> str:
    t = time.localtime(ts)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        return None
    return v


def read_amounts(data: dict) -> dict[str, float]:
    """Every catalog resource the payload reports. DE drops a MiscItems row at zero, so
    with the array present a missing row is 0; without it nothing is reported."""
    misc_list = data.get("MiscItems")
    has_misc = isinstance(misc_list, list)
    misc: dict[str, float] = {}
    for e in misc_list if has_misc else []:
        if not isinstance(e, dict):
            continue
        t, c = e.get("ItemType"), _num(e.get("ItemCount"))
        if isinstance(t, str) and c is not None and t not in misc:
            misc[t] = c
    out: dict[str, float] = {}
    for rid, _label, (kind, key), _c in RESOURCES:
        if kind == "field":
            v = _num(data.get(key))
            if v is not None:
                out[rid] = v
        elif has_misc:
            out[rid] = misc.get(key, 0)
    return out


def relic_total(data: dict) -> int:
    from ..inventory.model import collect_relic_counts

    try:
        return int(sum(max(0, c) for c in collect_relic_counts(data).values()))
    except Exception:  # noqa: BLE001 - odd payloads must not break the tracker
        return 0


# ---- trade dialog (eeLogMonitor.ts / tradeLogSanitize.ts) -------------------------------------

TRADE_DIALOG_START = "Are you sure you want to accept this trade?"
TRADE_SUCCESS = "The trade was successful!"
_TAIL_RE = re.compile(r"\b(?:leftItem|rightItem)=")
_PUA = re.compile("[-�]+")
_MOJIBAKE = re.compile("[îï][\u0080-¿]{2}")
_TRAILING_NON_ASCII = re.compile("[\u0080-￿]+$")
_ARG_TAIL = re.compile(r",?\s*\b(?:title|leftItem|rightItem)=.*$", re.I)
_STAMP = re.compile(r"^(\d+\.\d+)\b")
_RECEIVE = re.compile(r"and will receive from\s+(.+?)\s+the following:", re.I)
_PLAT = re.compile(r"^Platinum(?:\s+x\s+(\d+))?$", re.I)
_STACK = re.compile(r"^(.+?)\s+x\s+(\d+)$", re.I)


def strip_glyphs(value: str) -> str:
    v = _TRAILING_NON_ASCII.sub("", _MOJIBAKE.sub("", _PUA.sub("", value)))
    return re.sub(r"\s{2,}", " ", v).strip()


def is_framework_line(value: str) -> bool:
    return bool(re.match(r"^\d+\.\d+\s", value) or re.search(r"\[(Info|Error|Warning)\]", value)
                or re.search(r"\b[A-Za-z_]+\.lua\b", value))


def strip_arg_tail(value: str) -> str:
    return _ARG_TAIL.sub("", value)


def parse_trade_dialog(lines: list[str]) -> dict | None:
    """{partner, platChange, type sale|purchase|trade, items[{name, count, direction}], stamp}."""
    text = "\n".join(lines)
    start = text.find("You are offering:")
    if start < 0:
        return None
    desc = text[start:]
    m = _RECEIVE.search(desc)
    if not m:
        return None
    partner = strip_glyphs(m.group(1))
    offering = desc[len("You are offering:"):m.start()]
    receiving = desc[m.end():]

    def block(b: str) -> tuple[list[tuple[str, int]], int]:
        plat = 0
        counts: dict[str, int] = {}
        for raw in re.split(r"\r\n|[\n\r]", b):
            line = raw.strip()
            if not line:
                continue
            if re.match(r"^(leftItem|rightItem|title)=", line):
                break
            no_tail = strip_arg_tail(line)
            if is_framework_line(no_tail):
                continue
            cleaned = strip_glyphs(no_tail)
            if not cleaned:
                continue
            pm = _PLAT.match(cleaned)
            if pm:
                plat += int(pm.group(1)) if pm.group(1) else 1
                continue
            name, qty = cleaned, 1
            sm = _STACK.match(cleaned)
            if sm:
                name, qty = sm.group(1).strip(), int(sm.group(2)) or 1
            counts[name] = counts.get(name, 0) + qty
        return list(counts.items()), plat

    given, plat_out = block(offering)
    got, plat_in = block(receiving)
    kind = "sale" if plat_in > 0 and plat_out == 0 else "purchase" if plat_out > 0 and plat_in == 0 else "trade"
    sm = _STAMP.match(lines[0] if lines else "")
    items = ([{"name": n, "count": c, "direction": "given"} for n, c in given]
             + [{"name": n, "count": c, "direction": "received"} for n, c in got])
    return {"partner": partner, "platChange": max(plat_in, plat_out), "type": kind,
            "items": items[:MAX_TRADE_ITEMS], "stamp": sm.group(1) if sm else None}


class TradeDialogWatcher:
    """Buffers the multi-line dialog entry and parses it when the success line arrives.
    Fed every EE.log file line once (subscribe(source="file")); cheap per line."""

    def __init__(self, on_trade: Callable[[dict], None], clock: Callable[[], float] = time.monotonic):
        self.on_trade = on_trade
        self.clock = clock
        self._buf: list[str] | None = None
        self._start = 0.0
        self._sealed = False
        self._glue: int | None = None

    def _buffer_line(self, line: str) -> None:
        segments = line.split("\r")
        for i, seg in enumerate(segments):
            if is_framework_line(strip_arg_tail(seg)):
                if i == 0:
                    self._sealed = True
                elif self._glue is None:
                    self._glue = len(self._buf)
                return
            if seg.strip():
                self._buf.append(seg)
            if _TAIL_RE.search(seg):
                self._sealed = True
                self._glue = None
                return

    def feed(self, line: str) -> None:
        if not line:
            return
        if TRADE_DIALOG_START in line:
            self._buf = [line]
            self._start = self.clock()
            self._glue = None
            self._sealed = bool(_TAIL_RE.search(line[line.index(TRADE_DIALOG_START):]))
        elif self._buf is not None:
            if self.clock() - self._start > TRADE_DIALOG_TIMEOUT_S:
                self._buf = None
            elif not self._sealed:
                self._buffer_line(line)
        if TRADE_SUCCESS in line and self._buf is not None:
            lines = self._buf if self._glue is None else self._buf[:self._glue]
            self._buf, self._sealed, self._glue = None, False, None
            parsed = parse_trade_dialog(lines)
            if parsed:
                self.on_trade(parsed)


# ---- tracker ----------------------------------------------------------------------------------

def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        _log.warning("unreadable %s: %s", path, e)
        return None


def _write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def trade_flow(trades: list[dict]) -> dict:
    """computeFlow: platIn from sales, platOut from purchases, swaps, net, count."""
    plat_in = sum(t.get("platChange", 0) for t in trades if t.get("type") == "sale")
    plat_out = sum(t.get("platChange", 0) for t in trades if t.get("type") == "purchase")
    return {"count": len(trades), "sales": sum(1 for t in trades if t.get("type") == "sale"),
            "purchases": sum(1 for t in trades if t.get("type") == "purchase"),
            "swaps": sum(1 for t in trades if t.get("type") == "trade"),
            "platIn": plat_in, "platOut": plat_out, "net": plat_in - plat_out}


LEGACY_FIELDS = {"plat": ("platDelta", "absPlat"), "credits": ("creditsDelta", "absCredits"),
                 "endo": ("endoDelta", "absEndo"), "ducats": ("ducatsDelta", "absDucats"),
                 "aya": ("ayaDelta", "absAya"), "vitus": ("vitusDelta", "absVitus")}


def read_day(entry: dict, rid: str) -> dict | None:
    """{delta, abs?} for one resource of a history row; falls back to WFHelper's legacy
    flat fields (readStatResourceDay) for rows without the resources map."""
    mapped = (entry.get("resources") or {}).get(rid) if isinstance(entry.get("resources"), dict) else None
    if isinstance(mapped, dict) and _num(mapped.get("delta")) is not None:
        return mapped
    legacy = LEGACY_FIELDS.get(rid)
    if not legacy:
        return None
    d, a = entry.get(legacy[0]), entry.get(legacy[1])
    if _num(d) is None and _num(a) is None:
        return None
    out: dict = {"delta": d if _num(d) is not None else 0}
    if _num(a) is not None:
        out["abs"] = a
    return out


def trade_signature(t: dict) -> str:
    """WFHelper's duplicate key: type, plat, partner (case-folded), sorted dir:name:count."""
    items = sorted(f"{i.get('direction', '')}:{str(i.get('name') or i.get('displayName') or '').lower()}:"
                   f"{i.get('count', 1)}" for i in (t.get("items") or []) if isinstance(i, dict))
    return "|".join([str(t.get("type", "trade")), str(int(t.get("platChange") or 0)),
                     str(t.get("partner") or "").lower(), *items])


class StatsTracker:
    def __init__(self, runtime_dir: str, clock: Callable[[], float] = time.time):
        self.dir = runtime_dir
        self.clock = clock
        self._lock = threading.RLock()
        self.history: list[dict] = []
        self.trades: list[dict] = []  # oldest first
        self._baselines: dict[str, float] = {}
        self._currents: dict[str, float] = {}
        self._resumed: dict[str, float] = {}
        self._last_relics: int | None = None
        self._relics_opened = 0
        self._trades_today = 0
        self._day = ""
        self.updated_at: float | None = None
        self._load()

    @property
    def history_path(self) -> str:
        return os.path.join(self.dir, HISTORY_FILE)

    @property
    def trades_path(self) -> str:
        return os.path.join(self.dir, TRADES_FILE)

    def _load(self) -> None:
        raw = _read_json(self.history_path)
        entries = raw.get("entries") if isinstance(raw, dict) else raw if isinstance(raw, list) else None
        self.history = [e for e in entries or [] if isinstance(e, dict) and isinstance(e.get("date"), str)]
        self.history.sort(key=lambda e: e["date"])
        raw_t = _read_json(self.trades_path)
        trades = raw_t.get("trades") if isinstance(raw_t, dict) else None
        self.trades = [t for t in trades or [] if isinstance(t, dict) and t.get("id")]
        today = day_key(self.clock())
        entry = self._entry(today)
        if entry:
            # Claim the day even at zero so the first payload does not treat it as a rollover.
            self._day = today
            self._relics_opened = int(entry.get("relicsOpened") or 0)
            self._trades_today = int(entry.get("dailyTrades") or 0)
            for rid in RESOURCE_IDS:
                d = read_day(entry, rid)
                if d is None:
                    continue
                if _num(d.get("delta")):
                    self._resumed[rid] = d["delta"]
                if _num(d.get("abs")) is not None:
                    self._currents.setdefault(rid, d["abs"])

    def _entry(self, day: str) -> dict | None:
        for e in reversed(self.history):
            if e["date"] == day:
                return e
        return None

    def _roll(self, today: str) -> None:
        if self._day != today:
            self._day = today
            self._relics_opened = 0
            self._trades_today = 0
            self._last_relics = None  # no spurious spike across midnight
            self._resumed.clear()
            self._baselines.clear()

    def delta(self, rid: str) -> float:
        resumed = self._resumed.get(rid, 0)
        cur, base = self._currents.get(rid), self._baselines.get(rid)
        if cur is None or base is None:
            return resumed
        return resumed + (cur - base)

    def _upsert_today(self) -> None:
        resources: dict[str, dict] = {}
        for rid in RESOURCE_IDS:
            d, cur = self.delta(rid), self._currents.get(rid)
            if cur is None and d == 0:
                continue
            resources[rid] = {"delta": d} if cur is None else {"delta": d, "abs": cur}
        entry = {"date": self._day, "resources": resources, "relicsOpened": self._relics_opened,
                 "dailyTrades": self._trades_today, "daysPlayed": 1}
        # Legacy flat fields so the file stays readable by WFHelper's importer.
        for rid, (flat, flat_abs) in LEGACY_FIELDS.items():
            entry[flat] = resources.get(rid, {}).get("delta", 0)
            if "abs" in resources.get(rid, {}):
                entry[flat_abs] = resources[rid]["abs"]
        if self.history and self.history[-1]["date"] == self._day:
            self.history[-1] = entry
        else:
            self.history.append(entry)
            self.history.sort(key=lambda e: e["date"])
            self.history = self.history[-HISTORY_MAX_DAYS:]
        try:
            _write_json(self.history_path, {"schemaVersion": HISTORY_SCHEMA, "entries": self.history})
        except OSError as e:
            _log.warning("could not save stats history: %s", e)

    def on_inventory(self, data: dict, synced_at: float | None = None) -> None:
        """synced_at (epoch s) dates the payload; one from an earlier day (the snapshot
        persisted overnight) only updates the balances and never becomes today's baseline."""
        if not isinstance(data, dict):
            return
        with self._lock:
            today = day_key(self.clock())
            self._roll(today)
            if synced_at is not None and day_key(synced_at) < today:
                self._currents.update(read_amounts(data))
                self.updated_at = self.clock()
                return
            total = relic_total(data)
            if self._last_relics is not None and total < self._last_relics:
                self._relics_opened += self._last_relics - total
            self._last_relics = total
            self._currents.update(read_amounts(data))
            for rid, v in self._currents.items():
                # Resumed currents from the file are not a baseline: the day restarts from here.
                self._baselines.setdefault(rid, v)
            self._upsert_today()
            self.updated_at = self.clock()

    def add_trade(self, trade: dict) -> dict | None:
        now = self.clock()
        with self._lock:
            key, stamp = trade_signature(trade), trade.get("stamp")
            for t in reversed(self.trades[-20:]):
                if abs(now - float(t.get("ts", 0))) >= TRADE_DEDUPE_S or trade_signature(t) != key:
                    continue
                # Same dialog twice (file line + real-time echo) unless both carry
                # distinct log stamps: then it is a second, identical trade (WFHelper).
                if stamp is None or t.get("stamp") is None or t.get("stamp") == stamp:
                    return None
            row = {"id": uuid.uuid4().hex[:12], "ts": now, "date": day_key(now),
                   "type": trade.get("type", "trade"), "platChange": int(trade.get("platChange") or 0),
                   "partner": trade.get("partner") or "", "items": list(trade.get("items") or []),
                   "stamp": trade.get("stamp"), "source": "live"}
            self.trades.append(row)
            self.trades = self.trades[-MAX_TRADES:]
            self._roll(row["date"])
            self._trades_today += 1
            self._upsert_today()
            try:
                _write_json(self.trades_path, {"schemaVersion": 1, "trades": self.trades})
            except OSError as e:
                _log.warning("could not save trades: %s", e)
            self.updated_at = now
            return row

    def delete_trade(self, trade_id: str) -> bool:
        with self._lock:
            n = len(self.trades)
            self.trades = [t for t in self.trades if t.get("id") != trade_id]
            if len(self.trades) == n:
                return False
            _write_json(self.trades_path, {"schemaVersion": 1, "trades": self.trades})
            return True

    # ---- views ---------------------------------------------------------------------------

    def today(self) -> dict:
        with self._lock:
            today = day_key(self.clock())
            fresh = self._day == today
            res = {rid: {"delta": self.delta(rid) if fresh else 0, "current": self._currents.get(rid)}
                   for rid in RESOURCE_IDS}
            return {"date": today, "resources": res, "relicsOpened": self._relics_opened if fresh else 0,
                    "dailyTrades": self._trades_today if fresh else 0, "hasData": bool(self._currents)}

    def series(self, ids: list[str], days: int) -> dict:
        today = date.fromisoformat(day_key(self.clock()))
        dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        with self._lock:
            by_date = {e["date"]: e for e in self.history}
        values: dict[str, list[float]] = {rid: [] for rid in ids}
        relics: list[int] = []
        trades: list[int] = []
        played: list[bool] = []
        for d in dates:
            e = by_date.get(d) or {}
            for rid in ids:
                values[rid].append((read_day(e, rid) or {}).get("delta", 0) or 0)
            relics.append(int(e.get("relicsOpened") or 0))
            trades.append(int(e.get("dailyTrades") or 0))
            played.append(bool(e))
        return {"dates": dates, "values": values, "relicsOpened": relics, "dailyTrades": trades, "played": played}

    def trades_since(self, day: str) -> list[dict]:
        with self._lock:
            return [t for t in self.trades if str(t.get("date", "")) >= day]

    def trade_page(self, offset: int, limit: int) -> dict:
        with self._lock:
            rows = list(reversed(self.trades))
        if limit > 0 and offset >= len(rows):
            # Past the end (last row on the last page deleted): clamp to the last page.
            offset = max(0, (len(rows) - 1) // limit * limit)
        return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset}


def fmt_amount(rid: str, v: float | None) -> str:
    if v is None:
        return "–"
    if rid in COMPACT and abs(v) >= 1000:
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if abs(v) >= div:
                return f"{v / div:.1f}".rstrip("0").rstrip(".") + suf
    return f"{int(v):,}" if float(v).is_integer() else f"{v:,.1f}"


def build_view(tracker: StatsTracker, chart: list[str], days: int) -> dict:
    chart = [c for c in chart if c in LABELS] or list(DEFAULT_CHART)
    today = tracker.today()
    s = tracker.series(chart, days)
    totals = {rid: sum(s["values"][rid]) for rid in chart}
    month_start = today["date"][:8] + "01"
    recent = tracker.trade_page(0, 10)["rows"]
    return {
        "catalog": [{"id": rid, "label": LABELS[rid], "compact": rid in COMPACT} for rid in RESOURCE_IDS],
        "chart": chart, "days": days, "dayChoices": list(DAY_CHOICES),
        "today": today,
        "strip": [{"id": rid, "label": LABELS[rid], "current": today["resources"][rid]["current"],
                   "delta": today["resources"][rid]["delta"],
                   "currentText": fmt_amount(rid, today["resources"][rid]["current"]),
                   "deltaText": fmt_amount(rid, today["resources"][rid]["delta"])} for rid in chart],
        "series": s, "totals": totals,
        "trades": {"today": trade_flow(tracker.trades_since(today["date"])),
                   "month": trade_flow(tracker.trades_since(month_start)),
                   "total": len(tracker.trades), "recent": recent},
        "hasData": today["hasData"] or bool(tracker.history),
    }


def qam_view(tracker: StatsTracker) -> dict:
    today = tracker.today()
    rows = [{"id": rid, "label": LABELS[rid], "delta": today["resources"][rid]["delta"],
             "deltaText": fmt_amount(rid, today["resources"][rid]["delta"]),
             "currentText": fmt_amount(rid, today["resources"][rid]["current"])} for rid in QAM_IDS]
    flow = trade_flow(tracker.trades_since(today["date"]))
    return {"hasData": today["hasData"], "date": today["date"], "rows": rows,
            "relicsOpened": today["relicsOpened"], "trades": flow}


# ---- wiring -----------------------------------------------------------------------------------

_tracker: StatsTracker | None = None
_watcher: TradeDialogWatcher | None = None


def tracker() -> StatsTracker | None:
    return _tracker


def setup(app) -> None:
    global _tracker, _watcher
    from .. import inventory

    _tracker = StatsTracker(app.runtime_dir)

    def on_inv(snap) -> None:
        # fetched_at: when this copy was read (a snapshot persisted overnight keeps its old time).
        _tracker.on_inventory(snap.data, getattr(snap, "fetched_at", None))
        app.emit("stats.updated", {"date": _tracker.today()["date"]})

    def on_trade(parsed: dict) -> None:
        if not app.settings.get("stats.trade_tracking", True):
            return
        row = _tracker.add_trade(parsed)
        if row:
            app.emit("stats.updated", {"date": row["date"], "trade": row["id"]})

    # The tailer thread must return fast: parsing is string work, the file write goes to the pool.
    _watcher = TradeDialogWatcher(lambda parsed: app.submit(on_trade, parsed))
    inv = inventory.inventory()
    if inv is not None:
        app.submit(on_inv, inv)
    inventory.on_update(on_inv)
    app.eelog.subscribe(_watcher.feed, source=FILE)
    app.services["stats"] = _tracker


def _app():
    from ..app import get_app

    return get_app()


def _days(v: Any) -> int:
    try:
        d = int(v)
    except (TypeError, ValueError):
        d = 30
    return max(1, min(365, d))


@rpc.method("stats.view")
async def _rpc_view(days: int | None = None) -> dict:
    app = _app()
    if _tracker is None:
        return {"hasData": False, "error": "stats not initialised"}
    d = _days(days if days is not None else app.settings.get("stats.days", 30))
    chart = list(app.settings.get("stats.chart", DEFAULT_CHART) or DEFAULT_CHART)
    out = build_view(_tracker, chart, d)
    out["tradeTracking"] = bool(app.settings.get("stats.trade_tracking", True))
    return out


@rpc.method("stats.qam")
def _rpc_qam() -> dict:
    if _tracker is None:
        return {"hasData": False, "rows": [], "relicsOpened": 0, "trades": trade_flow([])}
    return qam_view(_tracker)


@rpc.method("stats.trades")
def _rpc_trades(offset: int = 0, limit: int = 25) -> dict:
    if _tracker is None:
        return {"rows": [], "total": 0}
    return _tracker.trade_page(max(0, int(offset)), max(1, min(100, int(limit))))


@rpc.method("stats.trade_delete")
async def _rpc_trade_delete(id: str) -> bool:  # noqa: A002 - RPC param name
    if _tracker is None:
        return False
    return await _app().run_blocking(_tracker.delete_trade, id)


@rpc.method("stats.configure")
def _rpc_configure(chart: list | None = None, days: int | None = None, trade_tracking: bool | None = None) -> dict:
    patch: dict = {}
    if chart is not None:
        patch["chart"] = [c for c in chart if c in LABELS][:12] or list(DEFAULT_CHART)
    if days is not None:
        patch["days"] = _days(days)
    if trade_tracking is not None:
        patch["trade_tracking"] = bool(trade_tracking)
    app = _app()
    if patch:
        app.settings.update({"stats": patch})
    return app.settings.get("stats", {})


@rpc.method("stats.status")
def _rpc_status() -> dict:
    t = _tracker
    if t is None:
        return {"ready": False}
    return {"ready": True, "days": len(t.history), "trades": len(t.trades), "updatedAt": t.updated_at,
            "historyPath": t.history_path, "tradesPath": t.trades_path}
