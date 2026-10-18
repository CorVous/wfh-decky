"""Wiki page: "where does X drop" over the bundled relic data (docs/port/menus.md §14).

WFHelper searches the full warframestat.us drop tables (services/dropData.ts). The plugin
bundles only the relic tables (data/relics.json.gz, refreshed weekly with the item data),
so the search covers relic rewards: which relics hold an item, at what rarity and chance
per refinement, and whether the relic is vaulted. The scoring follows searchDrops
(dropData.ts:341-372): lower-case substring, score 0 = match at the start, 1 = after a
space, 2 = elsewhere; then unvaulted relics before vaulted ones, intact chance desc, item,
relic. Owned relic counts come from the latest inventory snapshot (no fetch).

RPCs
  wiki.search(query, mode item|relic, limit, unvaulted_only, record)   {rows, total, items}
  wiki.relic(name)                     one relic with chances per refinement and owned counts
  wiki.relics(tier, unvaulted_only)    relic names per tier (browse without typing)
  wiki.recent()                        recent searches (setting wiki.recent)
  wiki.recent_clear()
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from typing import Any

from .. import log, rpc
from ..data.items import REFINEMENTS, TIERS

_log = log.get("menus.wiki")

MAX_LIMIT = 300
MAX_RECENT = 8
MODES = ("item", "relic")
WIKI_BASE = "https://wiki.warframe.com/w/"
_PART_SUFFIX = re.compile(r"\s+(Blueprint|Neuroptics|Chassis|Systems|Barrel|Receiver|Stock|Blade|Handle|Hilt|"
                          r"Grip|String|Upper Limb|Lower Limb|Link|Pouch|Stars|Head|Gauntlet|Ornament|Disc|"
                          r"Carapace|Cerebrum|Harness|Wings|Guard|Boot|Band|Buckle|Chain)(\s+Blueprint)?$", re.I)


def wiki_url(name: str) -> str:
    return WIKI_BASE + urllib.parse.quote(name.strip().replace(" ", "_"), safe="_()'-:,")


def wiki_page_for(item_name: str, parent_name: str | None = None) -> str:
    """Prime parts link to their parent's page ("Ash Prime Systems Blueprint" -> Ash_Prime)."""
    if parent_name:
        return wiki_url(parent_name)
    base = _PART_SUFFIX.sub("", item_name).strip() or item_name
    return wiki_url(base)


def build_rows(relic_db) -> list[dict]:
    """One row per (relic, reward)."""
    rows: list[dict] = []
    for relic in relic_db.all():
        for r in relic.rewards:
            rows.append({
                "item": r.item_name, "itemUnique": r.item_unique, "slug": r.wfm_slug,
                "relic": relic.name, "tier": relic.tier, "vaulted": relic.vaulted,
                "rarity": r.rarity,
                "chances": {q: round(r.chance(q), 2) for q in REFINEMENTS},
                "_item": r.item_name.lower(), "_relic": relic.name.lower(),
            })
    return rows


def _score(hay: str, q: str) -> int | None:
    i = hay.find(q)
    if i < 0:
        return None
    if i == 0:
        return 0
    if hay[i - 1] == " ":
        return 1
    return 2


def search_rows(rows: list[dict], query: str, mode: str = "item", limit: int = 60,
                unvaulted_only: bool = False) -> tuple[list[dict], int]:
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    if q.endswith(" relic"):
        q = q[:-6].strip()
    if not q:
        return [], 0
    field = "_relic" if mode == "relic" else "_item"
    hits: list[tuple[int, dict]] = []
    for row in rows:
        if unvaulted_only and row["vaulted"]:
            continue
        s = _score(row[field], q)
        if s is not None:
            hits.append((s, row))
    if mode == "relic":
        tier_order = {t: i for i, t in enumerate(TIERS)}
        rarity_order = {"Common": 0, "Uncommon": 1, "Rare": 2}
        hits.sort(key=lambda h: (h[0], tier_order.get(h[1]["tier"], 9), _relic_code_key(h[1]["relic"]),
                                 rarity_order.get(h[1]["rarity"], 3), h[1]["_item"]))
    else:
        hits.sort(key=lambda h: (h[0], h[1]["vaulted"], -h[1]["chances"].get("intact", 0), h[1]["_item"],
                                 _relic_code_key(h[1]["relic"])))
    total = len(hits)
    return [{k: v for k, v in h[1].items() if not k.startswith("_")} for h in hits[:limit]], total


def _relic_code_key(name: str) -> tuple:
    """"Lith A10" sorts after "Lith A2"."""
    parts = re.findall(r"\d+|\D+", name)
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def item_summaries(rows: list[dict], items_db=None, limit: int = 12) -> list[dict]:
    """Distinct matched items: relic count, unvaulted count, ducats, wiki link."""
    out: dict[str, dict] = {}
    for r in rows:
        s = out.get(r["item"])
        if s is None:
            if len(out) >= limit:
                continue
            it = items_db.by_unique(r["itemUnique"]) if items_db is not None else None
            if it is None and items_db is not None:
                it = items_db.by_name(r["item"])
            parent = items_db.by_unique(it.parent) if (items_db is not None and it is not None and it.parent) else None
            s = out[r["item"]] = {
                "item": r["item"], "slug": r["slug"], "ducats": it.ducats if it else None,
                "relics": 0, "unvaulted": 0, "bestIntact": 0.0, "bestRadiant": 0.0,
                "wikiUrl": wiki_page_for(r["item"], parent.name if parent else None),
            }
        s["relics"] += 1
        if not r["vaulted"]:
            s["unvaulted"] += 1
            s["bestIntact"] = max(s["bestIntact"], r["chances"].get("intact", 0))
            s["bestRadiant"] = max(s["bestRadiant"], r["chances"].get("radiant", 0))
    return list(out.values())


def owned_by_relic(owned: dict) -> dict[str, dict[str, int]]:
    """{("Lith A1", "radiant"): 2} -> {"Lith A1": {"radiant": 2}}."""
    out: dict[str, dict[str, int]] = {}
    for (name, ref), n in (owned or {}).items():
        out.setdefault(name, {})[ref] = out.get(name, {}).get(ref, 0) + int(n)
    return out


def relic_detail(relic, items_db=None, owned: dict[str, int] | None = None) -> dict:
    rewards = []
    rarity_order = {"Common": 0, "Uncommon": 1, "Rare": 2}
    for r in sorted(relic.rewards, key=lambda r: (rarity_order.get(r.rarity, 3), r.item_name)):
        it = items_db.by_unique(r.item_unique) if items_db is not None else None
        rewards.append({"item": r.item_name, "slug": r.wfm_slug, "rarity": r.rarity,
                        "ducats": it.ducats if it else None,
                        "chances": {q: round(r.chance(q), 2) for q in REFINEMENTS}})
    return {"name": relic.name, "tier": relic.tier, "vaulted": relic.vaulted, "slug": relic.wfm_slug,
            "owned": owned or {}, "ownedTotal": sum((owned or {}).values()), "rewards": rewards,
            "wikiUrl": wiki_url(f"{relic.name} Relic")}


# ---- cache over the active relic db -----------------------------------------------------------

_lock = threading.Lock()
_cache: tuple[Any, list[dict]] | None = None


def _rows() -> list[dict]:
    """Rebuilt when the relic db object changes (a data refresh swaps it)."""
    global _cache
    from ..data.relics import relics

    db = relics()
    with _lock:
        if _cache is None or _cache[0] is not db:
            _cache = (db, build_rows(db))
        return _cache[1]


def _items_db():
    try:
        from ..data.items import items

        return items()
    except Exception:  # noqa: BLE001 - no item data: summaries without ducats
        return None


def _owned() -> dict[str, dict[str, int]]:
    try:
        from .. import inventory

        return owned_by_relic(inventory.owned_relics())
    except Exception:  # noqa: BLE001 - no inventory is normal
        return {}


def _search(query: str, mode: str, limit: int, unvaulted_only: bool) -> dict:
    mode = mode if mode in MODES else "item"
    limit = max(1, min(MAX_LIMIT, int(limit or 60)))
    rows, total = search_rows(_rows(), query, mode, limit, unvaulted_only)
    owned = _owned()
    for r in rows:
        r["owned"] = sum(owned.get(r["relic"], {}).values())
    items = item_summaries(rows, _items_db()) if mode == "item" else []
    return {"query": query, "mode": mode, "rows": rows, "total": total, "items": items}


def _record(app, query: str, mode: str) -> list:
    q = (query or "").strip()
    if not q:
        return list(app.settings.get("wiki.recent", []) or [])
    recent = [r for r in (app.settings.get("wiki.recent", []) or [])
              if isinstance(r, dict) and not (r.get("q", "").lower() == q.lower() and r.get("mode") == mode)]
    recent.insert(0, {"q": q, "mode": mode})
    recent = recent[:MAX_RECENT]
    app.settings.update({"wiki": {"recent": recent}})
    return recent


def setup(app) -> None:
    app.services["wiki_rows"] = _rows


def _app():
    from ..app import get_app

    return get_app()


@rpc.method("wiki.search")
async def _rpc_search(query: str = "", mode: str = "item", limit: int = 60, unvaulted_only: bool = False,
                      record: bool = False) -> dict:
    app = _app()
    out = await app.run_blocking(_search, query, mode, limit, unvaulted_only)
    if record and out["total"]:
        out["recent"] = _record(app, query, out["mode"])
    return out


@rpc.method("wiki.relic")
async def _rpc_relic(name: str) -> dict | None:
    def work():
        from ..data.relics import relics

        relic = relics().get(name)
        if relic is None:
            return None
        return relic_detail(relic, _items_db(), _owned().get(relic.name, {}))

    return await _app().run_blocking(work)


@rpc.method("wiki.relics")
async def _rpc_relics(tier: str = "Lith", unvaulted_only: bool = True) -> list[dict]:
    def work():
        from ..data.relics import relics

        owned = _owned()
        out = [{"name": r.name, "vaulted": r.vaulted, "owned": sum(owned.get(r.name, {}).values())}
               for r in relics().all() if r.tier.lower() == str(tier).lower() and not (unvaulted_only and r.vaulted)]
        out.sort(key=lambda r: _relic_code_key(r["name"]))
        return out

    return await _app().run_blocking(work)


@rpc.method("wiki.recent")
def _rpc_recent() -> list:
    return list(_app().settings.get("wiki.recent", []) or [])


@rpc.method("wiki.recent_clear")
def _rpc_recent_clear() -> list:
    _app().settings.update({"wiki": {"recent": []}})
    return []
