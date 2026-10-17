"""Relic value math shared by the planner overlay and the Relics page.

Ports WFHelper's relic EV pipeline (docs/port/relic-planner.md §5-7,
docs/port/menus.md §6):

- `squad_ev`: expected value of one opening, or of the best of N openings when
  a squad of N cracks the same relic and everyone picks the best (radshare).
- `Valuer`: per-reward platinum (cached warframe.market prices only, never a
  network read) and ducats (item db), memoised for one computation.
- `relic_ev` / `pick_best_owned_quality` / `quality_for_mode`: which owned
  refinement to open and what it is worth. The planner never suggests refining;
  `refinement_table` reports what each refinement would be worth so pages can
  show the gain of refining.
- `build_rows` / `select_rows`: owned relic groups as view-model rows with the
  desktop planner's filters and sorts (relicPlannerView.ts), plus the era
  filter used by the overlay (`era_matches`, `normalize_era`, `tag_era`).
- `normalize_filters`: validation of the "push to overlay" payload.

Everything here is pure (data in, JSON-able dicts out) except `Valuer`, which
reads the shared market cache and item db; tests pass their own lookups.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

REFINEMENTS = ("intact", "exceptional", "flawless", "radiant")
REFINEMENT_LABEL = {"intact": "Intact", "exceptional": "Exceptional", "flawless": "Flawless", "radiant": "Radiant"}
REFINEMENT_SHORT = {"intact": "Int", "exceptional": "Exc", "flawless": "Fla", "radiant": "Rad"}
TIER_ORDER = {"Lith": 0, "Meso": 1, "Neo": 2, "Axi": 3, "Requiem": 4}
ERAS = ("lith", "meso", "neo", "axi", "requiem", "omnia")
# activeMissionTag -> era (relicSelection.ts:203-210)
TAG_ERA = {"VOIDT1": "lith", "VOIDT2": "meso", "VOIDT3": "neo", "VOIDT4": "axi", "VOIDT5": "requiem", "VOIDT6": "omnia"}
RARITY_RANK = {"rare": 0, "uncommon": 1, "common": 2}
# Void trace cost to refine Intact -> X (in-game constant).
REFINE_TRACES = {"intact": 0, "exceptional": 25, "flawless": 50, "radiant": 100}

SORT_MODES = ("tier", "name", "ev", "ducat", "ducatonator", "owned")
QUALITY_MODES = ("owned",) + REFINEMENTS
VAULTED_MODES = ("all", "vaulted", "unvaulted")
OWNED_ABOVE = (0, 2, 4, 6, 8, 10)
MAX_NEEDED_KEYS = 4000
PREVIEW_REWARDS = 6

DEFAULT_FILTERS: dict[str, Any] = {
    "squadSize": 1, "search": "", "containsNeededReward": False, "vaultedMode": "all",
    "qualityMode": "owned", "ownedAbove": 0, "sortMode": "tier", "sortDirection": "asc",
    "tierFilter": None, "neededRewardKeys": None, "pinnedQualities": None,
}
# Before the page ever pushes filters the overlay uses squad 4 and its own EV sort.
OVERLAY_SQUAD_DEFAULT = 4


# ---- EV ------------------------------------------------------------------------

def squad_ev(chances: Iterable[float], values: Iterable[float | None], n: int = 1) -> float:
    """computeSquadExpected (relicMath.ts): chances in percent, None values count 0.

    n <= 1: sum(p*v). n > 1: expected best of n independent openings, i.e.
    sum over value groups (ascending) of v * (CDF_g^n - CDF_{g-1}^n).
    """
    items = [(min(max((c or 0) / 100.0, 0.0), 1.0), float(v or 0)) for c, v in zip(chances, values)]
    n = int(n or 1)
    if n <= 1:
        return sum(p * v for p, v in items)
    grouped: list[list[float]] = []
    for p, v in sorted(items, key=lambda t: t[1]):
        if grouped and grouped[-1][1] == v:
            grouped[-1][0] += p
        else:
            grouped.append([p, v])
    ev = cdf_prev = 0.0
    for p, v in grouped:
        cdf = min(1.0, cdf_prev + p)
        ev += v * (cdf ** n - cdf_prev ** n)
        cdf_prev = cdf
    return ev


def _round(v: float | None, nd: int = 2) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


# ---- values --------------------------------------------------------------------

@dataclass
class Valuer:
    """Plat and ducats per reward, memoised. price_of(slug) -> float|None must
    not touch the network (the overlay path runs inside a 3 s budget)."""

    price_of: Callable[[str], float | None]
    ducats_of: Callable[[str], int | None]
    owned_of: Callable[[str], int] | None = None  # reward uniqueName -> copies (None: no inventory)
    _plat: dict[str, float | None] = field(default_factory=dict)
    _duc: dict[str, int | None] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)  # slugs without a cached price

    def plat(self, reward) -> float | None:
        slug = getattr(reward, "wfm_slug", None) or ""
        if not slug:
            return None
        if slug not in self._plat:
            try:
                v = self.price_of(slug)
            except Exception:  # noqa: BLE001 - a broken cache is "no price"
                v = None
            self._plat[slug] = float(v) if v is not None and v > 0 else None
            if self._plat[slug] is None:
                self.missing.add(slug)
        return self._plat[slug]

    def ducats(self, reward) -> int | None:
        name = getattr(reward, "item_name", "") or ""
        if name not in self._duc:
            try:
                v = self.ducats_of(name)
            except Exception:  # noqa: BLE001
                v = None
            self._duc[name] = int(round(v)) if v is not None and v > 0 else None
        return self._duc[name]

    def owned(self, reward) -> int | None:
        if self.owned_of is None:
            return None
        u = getattr(reward, "item_unique", "") or ""
        try:
            return int(self.owned_of(u)) if u else 0
        except Exception:  # noqa: BLE001
            return 0


# ---- last-known price snapshot ---------------------------------------------------
#
# The market cache drops disk entries older than its 30-minute TTL on load, so after
# a plugin or Deck restart it holds almost no prices until the background prefetch
# catches up. The planner keeps its own last-known plat per slug (any age) in
# <market cache dir>/relic_price_snapshot.json and falls back to it when the market
# cache has no entry. A fresh market price always wins and refreshes the snapshot.

SNAPSHOT_FILE = "relic_price_snapshot.json"
_snap_lock = threading.Lock()
_snap: dict[str, list] | None = None  # slug -> [plat, unix time]
_snap_path: str | None = None
_snap_dirty = False


def _snapshot_load(cache_dir: str | None) -> dict[str, list]:
    global _snap, _snap_path
    with _snap_lock:
        path = os.path.join(cache_dir, SNAPSHOT_FILE) if cache_dir else None
        if _snap is not None and path == _snap_path:
            return _snap
        _snap, _snap_path = {}, path
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    _snap = {k: v for k, v in raw.items()
                             if isinstance(k, str) and isinstance(v, list) and len(v) == 2
                             and isinstance(v[0], (int, float)) and v[0] > 0}
            except (OSError, ValueError):
                pass
        return _snap


def _snapshot_put(slug: str, plat: float) -> None:
    global _snap_dirty
    with _snap_lock:
        if _snap is None:
            return
        cur = _snap.get(slug)
        if cur is None or abs(cur[0] - plat) > 1e-9 or time.time() - cur[1] > 3600:
            _snap[slug] = [plat, time.time()]
            _snap_dirty = True


def save_price_snapshot() -> None:
    """Write the snapshot if prices changed (atomic; errors are logged and ignored)."""
    global _snap_dirty
    with _snap_lock:
        if not _snap_dirty or not _snap_path or _snap is None:
            return
        data, path = dict(_snap), _snap_path
        _snap_dirty = False
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".relic_prices.", suffix=".tmp", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def default_valuer(with_owned: bool = True) -> Valuer:
    """Valuer over the shared market cache (any age, no network), then the planner's
    last-known price snapshot, and the item db for ducats."""
    from .data import items as items_mod
    from .data import market

    m = market.get()
    snap = _snapshot_load(getattr(m, "cache_dir", None))

    def price_of(slug: str) -> float | None:
        hit, p = m.cached(slug, None, max_age=float("inf"))
        v = None
        if hit and p is not None:
            v = p.median if p.median else (float(p.low_sell) if p.low_sell else None)
        if v is not None and v > 0:
            _snapshot_put(slug, float(v))
            return v
        if hit:
            return None  # the market says this item has no price now
        old = snap.get(slug)
        return float(old[0]) if old else None

    db = items_mod.items()

    def ducats_of(name: str) -> int | None:
        it = db.by_name(name)
        return it.ducats if it else None

    owned_of = None
    if with_owned:
        from . import inventory as inv_mod

        inv = inv_mod.inventory()
        if inv is not None:
            owned_of = inv.owned_count
    return Valuer(price_of, ducats_of, owned_of)


def relic_ev(relic, refinement: str, squad: int, valuer: Valuer) -> tuple[float | None, float | None]:
    """(platEv, ducatEv) of opening `refinement`; a side is None when no reward has a value."""
    rewards = [r for r in relic.rewards if r.chance(refinement) > 0]
    if not rewards:
        return None, None
    chances = [min(max(r.chance(refinement), 0.0), 100.0) for r in rewards]
    plats = [valuer.plat(r) for r in rewards]
    ducs = [valuer.ducats(r) for r in rewards]
    plat = squad_ev(chances, plats, squad) if any(v is not None for v in plats) else None
    duc = squad_ev(chances, ducs, squad) if any(v is not None for v in ducs) else None
    return plat, duc


def pick_best_owned_quality(relic, owned: dict[str, int], squad: int, valuer: Valuer) -> str | None:
    """Highest plat EV among owned refinements (radiant first wins ties; then ducats)."""
    best, best_key = None, None
    for q in reversed(REFINEMENTS):
        if (owned.get(q) or 0) <= 0:
            continue
        p, d = relic_ev(relic, q, squad, valuer)
        key = (p if p is not None else -1.0, d if d is not None else -1.0)
        if best_key is None or key > best_key:
            best, best_key = q, key
    return best


def highest_owned(owned: dict[str, int]) -> str | None:
    for q in reversed(REFINEMENTS):
        if (owned.get(q) or 0) > 0:
            return q
    return None


def quality_for_mode(mode: str, owned: dict[str, int], pinned: str | None = None) -> str | None:
    """relicQualityForMode: a named refinement, else the pinned one if owned, else the highest owned."""
    if mode in REFINEMENTS:
        return mode
    if pinned and (owned.get(pinned) or 0) > 0:
        return pinned
    return highest_owned(owned)


def owned_count_for_mode(owned: dict[str, int], mode: str) -> int:
    if mode in REFINEMENTS:
        return int(owned.get(mode) or 0)
    return int(sum(owned.get(q) or 0 for q in REFINEMENTS))


def ducatonator(plat: float | None, ducat: float | None) -> float | None:
    if ducat is None or plat is None or plat <= 0:
        return None
    return ducat / plat


def sort_rewards(rewards: list, refinement: str = "intact") -> list:
    """Rare first, then chance descending (relicRewardOrder.ts)."""
    return sorted(rewards, key=lambda r: (RARITY_RANK.get((r.rarity or "").lower(), 3), -r.chance(refinement)))


def reward_rarity(reward, refinement: str) -> str:
    from .data.items import relic_reward_rarity

    return relic_reward_rarity(refinement, reward.chance(refinement), reward.rarity or "Common")


def is_forma(name: str) -> bool:
    return "forma blueprint" in (name or "").lower()


def reward_json(reward, refinement: str, valuer: Valuer) -> dict:
    owned = valuer.owned(reward)
    return {
        "name": reward.item_name, "uniqueName": reward.item_unique or None, "slug": reward.wfm_slug,
        "rarity": reward_rarity(reward, refinement), "chance": round(reward.chance(refinement), 2),
        "platinum": _round(valuer.plat(reward), 1), "ducats": valuer.ducats(reward),
        "owned": owned, "needed": None if owned is None or is_forma(reward.item_name) else owned == 0,
    }


def refinement_table(relic, squad: int, valuer: Valuer, owned: dict[str, int] | None = None) -> list[dict]:
    """EV of each refinement, with the gain over Intact and plat per 25 void traces."""
    out = []
    base_p, _ = relic_ev(relic, "intact", squad, valuer)
    for q in REFINEMENTS:
        p, d = relic_ev(relic, q, squad, valuer)
        gain = None if p is None or base_p is None else p - base_p
        traces = REFINE_TRACES[q]
        out.append({"quality": q, "label": REFINEMENT_LABEL[q], "owned": int((owned or {}).get(q) or 0),
                    "plat": _round(p), "ducat": _round(d), "gainOverIntact": _round(gain),
                    "traces": traces, "platPer100Traces": _round(gain * 100 / traces) if gain is not None and traces else None})
    return out


# ---- era -------------------------------------------------------------------------

def normalize_era(value: Any) -> str | None:
    """Substring order requiem, lith, meso, neo, axi, omnia (relicSelection.ts:183-195)."""
    s = str(value or "").strip().lower()
    for e in ("requiem", "lith", "meso", "neo", "axi", "omnia"):
        if e in s:
            return e
    return None


def tag_era(tag: str) -> str | None:
    return TAG_ERA.get(str(tag or "").strip().upper())


def era_matches(tier: str, era: str | None) -> bool:
    """All when era is None; omnia keeps every era but Requiem (Vanguard included,
    like buildRecommendations' eraFilter=null for omnia)."""
    if not era:
        return True
    group_era = normalize_era(tier)
    if era == "omnia":
        return group_era != "requiem"
    return group_era == era


def era_label(era: str | None) -> str:
    return {"omnia": "Omnia (all but Requiem)"}.get(era or "", (era or "all").capitalize())


# ---- search ----------------------------------------------------------------------

def _norm_search(s: str) -> str:
    s = re.sub(r"['’]", "", str(s or "").lower())
    return re.sub(r"[\W_]+", " ", s).strip()


def _strip_pb(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b(prime|blueprint)\b", " ", _norm_search(s))).strip()


def search_terms(relic) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(v: str | None) -> None:
        for t in (_norm_search(v or ""), _strip_pb(v or "")):
            if t and t not in seen:
                seen.add(t)
                terms.append(t)

    add(relic.name)
    add(f"{relic.tier} {relic.code}")
    for r in relic.rewards:
        add(r.item_name)
        if r.wfm_slug:
            add(r.wfm_slug.replace("_", " "))
    return terms


def matches_search(relic, query: str, owned: dict[str, int] | None) -> bool:
    """relicGroupMatchesSearch: refinement words filter by ownership, the rest matches terms."""
    tokens = _norm_search(query).split()
    quals = [q for q in REFINEMENTS if q in tokens]
    if quals:
        tokens = [t for t in tokens if t not in quals]
        if owned is not None and not any((owned.get(q) or 0) > 0 for q in quals):
            return False
    q = " ".join(tokens)
    if not q:
        return True
    compact = q.replace(" ", "")
    for term in search_terms(relic):
        if q in term or (compact and compact in term.replace(" ", "")):
            return True
        if len(tokens) > 1 and set(tokens) <= set(term.split()):
            return True
    return False


# ---- filters -----------------------------------------------------------------------

def normalize_filters(push: Any, prev: dict | None = None) -> dict:
    """normalizeRelicOverlayFilterPush: invalid fields fall back to the previous push
    (or the defaults with squad 4 on the first push)."""
    base = dict(prev) if prev else {**DEFAULT_FILTERS, "squadSize": OVERLAY_SQUAD_DEFAULT}
    p = push if isinstance(push, dict) else {}
    out = dict(base)
    sq = p.get("squadSize")
    if isinstance(sq, (int, float)) and not isinstance(sq, bool) and int(sq) == sq and 1 <= sq <= 4:
        out["squadSize"] = int(sq)
    if isinstance(p.get("search"), str):
        out["search"] = p["search"][:200]
    if isinstance(p.get("containsNeededReward"), bool):
        out["containsNeededReward"] = p["containsNeededReward"]
    if p.get("vaultedMode") in VAULTED_MODES:
        out["vaultedMode"] = p["vaultedMode"]
    if p.get("qualityMode") in QUALITY_MODES:
        out["qualityMode"] = p["qualityMode"]
    if p.get("ownedAbove") in OWNED_ABOVE and not isinstance(p.get("ownedAbove"), bool):
        out["ownedAbove"] = int(p["ownedAbove"])
    if p.get("sortMode") in SORT_MODES:
        out["sortMode"] = p["sortMode"]
    if p.get("sortDirection") in ("asc", "desc"):
        out["sortDirection"] = p["sortDirection"]
    if "tierFilter" in p:
        t = p.get("tierFilter")
        out["tierFilter"] = normalize_era(t) if t and str(t).lower() != "all" else None
    if "neededRewardKeys" in p:
        keys = p.get("neededRewardKeys")
        if keys is None:
            out["neededRewardKeys"] = None
        elif isinstance(keys, list):
            out["neededRewardKeys"] = [k for k in keys if isinstance(k, str) and 0 < len(k) <= 64][:MAX_NEEDED_KEYS]
    if "pinnedQualities" in p:
        pins = p.get("pinnedQualities")
        if pins is None:
            out["pinnedQualities"] = None
        elif isinstance(pins, dict):
            out["pinnedQualities"] = {k: v for k, v in pins.items()
                                      if isinstance(k, str) and len(k) <= 64 and v in REFINEMENTS}
    return out


# ---- rows --------------------------------------------------------------------------

def owned_by_group(owned_relics: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    """{("Axi A16","radiant"): 2} -> {"Axi A16": {"intact":0,...,"radiant":2}}."""
    out: dict[str, dict[str, int]] = {}
    for (name, q), n in (owned_relics or {}).items():
        if q not in REFINEMENTS or n <= 0:
            continue
        g = out.setdefault(name, {r: 0 for r in REFINEMENTS})
        g[q] += int(n)
    return out


def needed_reward(valuer: Valuer, reward) -> bool | None:
    """Simplified need rule (relic-planner.md §7.6): needed when no copy is owned."""
    o = valuer.owned(reward)
    return None if o is None else o == 0


def group_has_needed(relic, valuer: Valuer) -> bool:
    return any(needed_reward(valuer, r) for r in relic.rewards if r.item_name and not is_forma(r.item_name))


def build_row(relic, owned: dict[str, int] | None, squad: int, valuer: Valuer, quality: str | None,
              rewards_limit: int = PREVIEW_REWARDS, with_rewards: bool = True) -> dict:
    owned = owned or {q: 0 for q in REFINEMENTS}
    q = quality or "intact"
    plat, duc = relic_ev(relic, q, squad, valuer)
    count = int(owned.get(q) or 0)
    row = {
        "key": relic.name, "name": relic.name, "tier": relic.tier, "code": relic.code,
        "vaulted": bool(relic.vaulted), "owned": dict(owned), "ownedTotal": int(sum(owned.values())),
        "quality": q, "qualityLabel": REFINEMENT_LABEL[q], "count": count,
        "label": f"{count}x {relic.name} {REFINEMENT_LABEL[q]}",
        "plat": _round(plat), "ducat": _round(duc), "ratio": _round(ducatonator(plat, duc), 3),
        "state": "has-value" if plat is not None else "no-data",
    }
    if with_rewards:
        rs = sort_rewards([r for r in relic.rewards if r.chance(q) > 0], q)
        row["rewards"] = [reward_json(r, q, valuer) for r in rs[:rewards_limit]]
    return row


def build_rows(db, owned_relics: dict[tuple[str, str], int] | None, *, squad: int = 1, era: str | None = None,
               ownership: str = "owned", quality_mode: str = "owned", pinned: dict | None = None,
               best_quality: bool = False, valuer: Valuer, with_rewards: bool = True,
               fallback_unowned: bool = False) -> list[dict]:
    """One row per relic group.

    ownership "owned" keeps groups with any owned copy (when an inventory exists).
    best_quality picks the owned refinement with the highest EV (overlay default,
    pickBestOwnedQuality); otherwise quality_for_mode (page and pushed filters).
    fallback_unowned (overlay, buildRecommendations): when the chosen refinement
    has no copies and the group is owned, use the best owned refinement instead,
    so the overlay never ranks a "0x" relic. The page keeps the chosen refinement.
    """
    by_group = owned_by_group(owned_relics or {})
    has_inv = owned_relics is not None
    rows = []
    for relic in db.all():
        if not era_matches(relic.tier, era):
            continue
        owned = by_group.get(relic.name)
        total = sum(owned.values()) if owned else 0
        if ownership == "owned" and has_inv and total <= 0:
            continue
        if owned and total > 0:
            if best_quality:
                q = pick_best_owned_quality(relic, owned, squad, valuer)
            else:
                q = quality_for_mode(quality_mode, owned, (pinned or {}).get(relic.name))
                if fallback_unowned and (not q or (owned.get(q) or 0) <= 0):
                    q = pick_best_owned_quality(relic, owned, squad, valuer)
        else:
            q = quality_mode if quality_mode in REFINEMENTS else "intact"
        row = build_row(relic, owned, squad, valuer, q, with_rewards=with_rewards)
        row["_relic"] = relic
        rows.append(row)
    return rows


def _tier_name_key(r: dict) -> tuple:
    return (TIER_ORDER.get(r["tier"], 99), _natural(r["name"]))


def _natural(s: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or ""))


def sort_rows(rows: list[dict], sort_mode: str = "tier", direction: str = "asc", quality_mode: str = "owned") -> list[dict]:
    """compareRelicPlannerRows: metric sorts put nulls last, ties by tier then name."""
    desc = direction == "desc"
    if sort_mode == "name":
        return sorted(rows, key=lambda r: _natural(r["name"]), reverse=desc)
    if sort_mode == "tier":
        return sorted(rows, key=_tier_name_key, reverse=desc)
    if sort_mode == "owned":
        metric = lambda r: owned_count_for_mode(r["owned"], quality_mode)  # noqa: E731
    else:
        k = {"ev": "plat", "ducat": "ducat", "ducatonator": "ratio"}.get(sort_mode, "plat")
        metric = lambda r: r.get(k)  # noqa: E731
    with_v = [r for r in rows if metric(r) is not None]
    without = sorted((r for r in rows if metric(r) is None), key=_tier_name_key)
    # stable two-pass: tie-break asc first, then the metric
    with_v.sort(key=_tier_name_key)
    with_v.sort(key=lambda r: metric(r), reverse=desc)
    return with_v + without


def select_rows(rows: list[dict], filters: dict, valuer: Valuer | None = None,
                has_inventory: bool = True) -> list[dict]:
    """selectRelicPlannerRows: vaulted, ownedAbove, search, needed reward, then sort."""
    vm = filters.get("vaultedMode", "all")
    above = int(filters.get("ownedAbove") or 0)
    search = (filters.get("search") or "").strip()
    needed = bool(filters.get("containsNeededReward"))
    keys = filters.get("neededRewardKeys")
    key_set = set(keys) if isinstance(keys, list) else None
    kept = []
    for r in rows:
        if vm != "all" and r["vaulted"] != (vm == "vaulted"):
            continue
        if above > 0 and r["ownedTotal"] <= above:
            continue
        relic = r.get("_relic")
        if search and relic is not None and not matches_search(relic, search, r["owned"] if has_inventory else None):
            continue
        if needed:
            if key_set is not None:
                if r["key"] not in key_set:
                    continue
            elif relic is not None and valuer is not None and not group_has_needed(relic, valuer):
                continue
        kept.append(r)
    return sort_rows(kept, filters.get("sortMode", "tier"), filters.get("sortDirection", "asc"),
                     filters.get("qualityMode", "owned"))


def rank_by_ev(rows: list[dict]) -> list[dict]:
    """Overlay default order: plat desc (null = -1), ducats desc, label."""
    return sorted(rows, key=lambda r: (-(r["plat"] if r["plat"] is not None else -1),
                                       -(r["ducat"] if r["ducat"] is not None else -1), r["label"]))


def public(row: dict) -> dict:
    """Row without internal fields (JSON-able)."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


def fingerprint(owned_relics: dict[tuple[str, str], int] | None) -> str:
    """Stable digest of owned counts ("<group>:<i>|<e>|<f>|<r>;...")."""
    by = owned_by_group(owned_relics or {})
    return ";".join(f"{k}:" + "|".join(str(by[k][q]) for q in REFINEMENTS) for k in sorted(by))
