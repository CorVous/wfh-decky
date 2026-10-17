"""Item database built from a trimmed @wfcd/items bundle.

The bundle (``data/items.json.gz`` + ``data/relics.json.gz``) is produced at build
time by scripts/fetch-data.sh, which calls :func:`build_bundle` on the npm
package's ``data/json`` files. The same function runs at runtime when a weekly
refresh pulls newer WFCD JSON from raw.githubusercontent.com into
``<runtime_dir>/data``; whichever copy was generated from newer source data wins.

Everything is loaded lazily on first use (parsing takes ~0.3 s on a Deck) and
is read-only afterwards, so it is safe to share between threads.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .. import log, net, rpc
from . import names

_log = log.get("items")

SCHEMA = 1
ITEMS_FILE = "items.json.gz"
RELICS_FILE = "relics.json.gz"
MANIFEST_FILE = "manifest.json"

# Categories kept from @wfcd/items (WFHelper loadWfcdItems + SentinelWeapons for
# deconstructor parts). Enemy, Glyphs, Node, Quests, Railjack, Sigils, Skins and
# Honoria are dropped: nothing in the plugin prices or scans them.
CATEGORIES = [
    "Warframes", "Primary", "Secondary", "Melee", "Sentinels", "SentinelWeapons", "Pets",
    "Archwing", "Arch-Gun", "Arch-Melee", "Mods", "Resources", "Misc", "Fish", "Gear", "Arcanes",
]
SOURCE_FILES = CATEGORIES + ["Components", "Relics"]

RAW_BASE = "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/"
REFRESH_INTERVAL = 7 * 24 * 3600
# Files whose ETags decide whether a refresh needs the full download.
_PROBE_FILES = ("Relics", "Components")
# A refresh smaller than this is a broken upstream, not a game update.
MIN_REFRESH_RELICS = 500
MIN_REFRESH_ITEMS = 5000

TIERS = ("Lith", "Meso", "Neo", "Axi", "Requiem", "Vanguard")
REFINEMENTS = ("intact", "exceptional", "flawless", "radiant")
_QUALITY_WORDS = {q.capitalize(): q for q in REFINEMENTS}

# DE chance bands mislabel relic commons; rarity is derived from refinement and
# chance instead (WFHelper services/relicRarity.ts).
CHANCE_RARITY: dict[str, list[tuple[float, str]]] = {
    "intact": [(25.33, "Common"), (11, "Uncommon"), (2, "Rare")],
    "exceptional": [(23.33, "Common"), (13, "Uncommon"), (4, "Rare")],
    "flawless": [(20, "Common"), (17, "Uncommon"), (6, "Rare")],
    "radiant": [(16.67, "Common"), (20, "Uncommon"), (10, "Rare")],
}


def relic_reward_rarity(refinement: str, chance: float, fallback: str = "Common") -> str:
    table = CHANCE_RARITY.get(str(refinement).lower())
    if not table:
        return fallback
    for expected, rarity in table:
        if abs(float(chance) - expected) < 0.5:
            return rarity
    return fallback


# ---- model -------------------------------------------------------------

@dataclass
class Component:
    unique_name: str
    name: str
    ducats: int | None = None
    item_count: int = 1
    tradable: bool = False

    def to_json(self) -> dict:
        return {"unique_name": self.unique_name, "name": self.name, "ducats": self.ducats,
                "item_count": self.item_count, "tradable": self.tradable}


@dataclass
class Item:
    unique_name: str
    name: str
    category: str = ""
    type: str = ""
    ducats: int | None = None
    vaulted: bool | None = None
    tradable: bool = False
    wfm_slug: str | None = None
    components: list[Component] = field(default_factory=list)
    mastery_req: int | None = None
    image_name: str | None = None
    parent: str | None = None          # uniqueName of the item this is a part of
    rarity: str | None = None          # relic reward pool entries only
    relic_reward: bool = False

    def to_json(self) -> dict:
        return {
            "unique_name": self.unique_name, "name": self.name, "category": self.category,
            "type": self.type, "ducats": self.ducats, "vaulted": self.vaulted,
            "tradable": self.tradable, "wfm_slug": self.wfm_slug, "mastery_req": self.mastery_req,
            "image_name": self.image_name, "parent": self.parent, "rarity": self.rarity,
            "components": [c.to_json() for c in self.components],
        }


class ItemDb:
    def __init__(self, data: dict, source: str = "bundle", path: str = ""):
        self.version: str = str(data.get("version") or "")
        self.source_time: str = str(data.get("source_time") or "")
        self.source = source
        self.path = path
        self._items: list[Item] = []
        self._by_unique: dict[str, Item] = {}
        self._by_name: dict[str, Item] = {}
        self._by_slug: dict[str, Item] = {}
        self._pool: list[Item] = []
        for raw in data.get("items") or []:
            item = _item_from_json(raw)
            self._items.append(item)
            self._by_unique.setdefault(item.unique_name, item)
            self._index_name(item)
        for raw in data.get("rewards") or []:
            base = self._by_unique.get(raw.get("uniqueName") or "")
            item = Item(
                unique_name=raw.get("uniqueName") or "",
                name=raw["name"],
                category=base.category if base else "Components",
                type=base.type if base else "",
                ducats=raw.get("ducats"),
                vaulted=bool(raw.get("vaulted")),
                tradable=True if raw.get("wfmSlug") else bool(base and base.tradable),
                wfm_slug=raw.get("wfmSlug"),
                image_name=raw.get("imageName") or (base.image_name if base else None),
                parent=raw.get("parent") or (base.parent if base else None),
                rarity=raw.get("rarity"),
                relic_reward=True,
            )
            self._pool.append(item)
            # A reward name beats a same-named catalog entry: it carries the slug and ducats.
            self._by_name[names.normalize_for_search(item.name)] = item
            if item.wfm_slug:
                self._by_slug[item.wfm_slug] = item
            if item.unique_name and item.unique_name not in self._by_unique:
                self._by_unique[item.unique_name] = item
        self._pool = names.sort_pool(self._pool)

    def _index_name(self, item: Item) -> None:
        key = names.normalize_for_search(item.name)
        prev = self._by_name.get(key)
        # Prefer entries that carry part metadata (lookupItemByNameOrSlug, itemDatabase.ts:984-1004).
        if prev is None or (not (prev.parent or prev.ducats) and (item.parent or item.ducats)):
            self._by_name[key] = item
        if item.wfm_slug and item.wfm_slug not in self._by_slug:
            self._by_slug[item.wfm_slug] = item

    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> list[Item]:
        return list(self._items)

    def by_unique(self, unique_name: str) -> Item | None:
        return self._by_unique.get(unique_name or "")

    def by_name(self, name: str) -> Item | None:
        """Exact (case/space-insensitive) name, else the name folded to a WFM slug."""
        key = names.normalize_for_search(name)
        hit = self._by_name.get(key)
        if hit is None and key:
            slug = names.normalize_wfm_slug(key)
            hit = self._by_slug.get(slug or "")
        return hit

    def by_slug(self, slug: str) -> Item | None:
        return self._by_slug.get((slug or "").strip().lower())

    def prime_parts(self) -> list[Item]:
        """The relic reward pool (prime parts, Forma, 2X Forma, Kuva...), longest name first."""
        return list(self._pool)

    def rank_names(self, ocr_text: str, limit: int = 5, pool: Iterable[Item] | None = None) -> list[names.Candidate]:
        return names.rank_candidates(ocr_text, self._pool if pool is None else pool, limit)

    def match_name(self, ocr_text: str, pool: Iterable[Item] | None = None) -> tuple[Item | None, float]:
        """Best reward-pool item for one OCR'd card title and its confidence (0..1).

        Callers accept a read at names.SLOT_ACCEPT_CONFIDENCE (0.86) or above.
        """
        best = names.match_single(ocr_text, self._pool if pool is None else pool)
        return best.item, best.confidence

    def search(self, query: str, limit: int = 20, tradable_only: bool = False) -> list[Item]:
        """Prefix matches first, then substring matches."""
        q = names.normalize_for_search(query)
        if len(q) < 2:
            return []
        starts, contains = [], []
        seen: set[str] = set()
        for item in list(self._pool) + self._items:
            if tradable_only and not item.tradable:
                continue
            key = names.normalize_for_search(item.name)
            if key in seen:
                continue
            if key.startswith(q):
                starts.append(item)
                seen.add(key)
            elif q in key:
                contains.append(item)
                seen.add(key)
        starts.sort(key=lambda i: (len(i.name), i.name))
        contains.sort(key=lambda i: (len(i.name), i.name))
        return (starts + contains)[:limit]

    def status(self) -> dict:
        return {"version": self.version, "source": self.source, "source_time": self.source_time,
                "items": len(self._items), "reward_pool": len(self._pool), "path": self.path}


def _item_from_json(raw: dict) -> Item:
    return Item(
        unique_name=raw.get("uniqueName") or "",
        name=raw.get("name") or "",
        category=raw.get("category") or "",
        type=raw.get("type") or "",
        ducats=raw.get("ducats"),
        vaulted=raw.get("vaulted"),
        tradable=bool(raw.get("tradable")),
        wfm_slug=raw.get("wfmSlug"),
        components=[Component(c.get("uniqueName") or "", c.get("name") or "", c.get("ducats"),
                              int(c.get("itemCount") or 1), bool(c.get("tradable")))
                    for c in raw.get("components") or []],
        mastery_req=raw.get("masteryReq"),
        image_name=raw.get("imageName"),
        parent=raw.get("parent"),
    )


# ---- loading -----------------------------------------------------------

_lock = threading.RLock()
_items_db: ItemDb | None = None
_dirs_override: tuple[str, str | None] | None = None
_reset_hooks: list[Callable[[], None]] = []


def set_data_dirs(bundle_dir: str, runtime_dir: str | None = None) -> None:
    """Point the loaders somewhere else (tests, CLI); clears loaded databases."""
    global _dirs_override
    _dirs_override = (bundle_dir, runtime_dir)
    reset()


def on_reset(fn: Callable[[], None]) -> None:
    """relics.py registers here so a data refresh drops both databases."""
    _reset_hooks.append(fn)


def reset() -> None:
    global _items_db
    with _lock:
        _items_db = None
    for fn in list(_reset_hooks):
        fn()


def _dirs() -> tuple[str, str | None]:
    if _dirs_override is not None:
        return _dirs_override
    from ..app import get_app

    app = get_app()
    return app.data_dir, os.path.join(app.runtime_dir, "data")


def _read_manifest(d: str | None) -> dict | None:
    if not d:
        return None
    try:
        with open(os.path.join(d, MANIFEST_FILE), encoding="utf-8") as f:
            m = json.load(f)
        if m.get("schema") == SCHEMA and os.path.exists(os.path.join(d, ITEMS_FILE)):
            return m
    except (OSError, ValueError):
        pass
    return None


def active_data_dir() -> tuple[str | None, str]:
    """(directory, "bundle"|"runtime"): the copy generated from the newest source data."""
    bundle, runtime = _dirs()
    mb, mr = _read_manifest(bundle), _read_manifest(runtime)
    if mr and (not mb or str(mr.get("source_time", "")) > str(mb.get("source_time", ""))):
        return runtime, "runtime"
    if mb:
        return bundle, "bundle"
    return None, "none"


def load_json_gz(d: str, name: str) -> dict:
    with gzip.open(os.path.join(d, name), "rt", encoding="utf-8") as f:
        return json.load(f)


def items() -> ItemDb:
    """The shared ItemDb (empty when no bundle is installed)."""
    global _items_db
    with _lock:
        if _items_db is None:
            d, src = active_data_dir()
            if d is None:
                _log.warning("no item data found (run scripts/fetch-data.sh)")
                _items_db = ItemDb({}, "none")
            else:
                t0 = time.monotonic()
                _items_db = ItemDb(load_json_gz(d, ITEMS_FILE), src, d)
                _log.info("item db %s (%s): %d items, %d rewards in %.2fs", _items_db.version, src,
                          len(_items_db), len(_items_db._pool), time.monotonic() - t0)
        return _items_db


# ---- bundle build (fetch-data.sh and runtime refresh) -----------------

def _ducats(raw: dict) -> int | None:
    v = raw.get("primeSellingPrice", raw.get("ducats"))
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _compact(d: dict) -> dict:
    """Drop empty fields; keeps the gzip small and the loader uses .get()."""
    return {k: v for k, v in d.items() if v not in (None, "", [], False)}


def _relic_vaulted(relic: dict) -> bool:
    """@wfcd flags Citrine relics vaulted while listing their drops (relicService.ts:147-150)."""
    drops = relic.get("dropCount")
    if drops is None:
        drops = len(relic.get("drops") or [])
    return bool(relic.get("vaulted")) and drops == 0


def _parse_relic_name(name: str) -> tuple[str, str, str] | None:
    parts = (name or "").split(" ")
    if len(parts) < 3 or parts[0] not in TIERS or parts[-1] not in _QUALITY_WORDS:
        return None
    return parts[0], " ".join(parts[1:-1]), _QUALITY_WORDS[parts[-1]]


def build_bundle(read: Callable[[str], list | None], out_dir: str, version: str = "",
                 source_time: str = "", source: str = "@wfcd/items") -> dict:
    """Trim WFCD category JSON into items.json.gz + relics.json.gz + manifest.json.

    ``read(category)`` returns the parsed ``data/json/<category>.json`` list or None.
    Returns the manifest.
    """
    comps_raw = read("Components") or []
    relics_raw = read("Relics") or []
    if not relics_raw:
        raise ValueError("Relics.json missing or empty")
    catalog: dict[str, dict] = {c["uniqueName"]: c for c in comps_raw if c.get("uniqueName")}
    comp_ids = set(catalog)
    cat_rows: list[tuple[str, dict]] = []
    for cat in CATEGORIES:
        for row in read(cat) or []:
            if row.get("uniqueName") and row.get("name"):
                cat_rows.append((cat, row))
                catalog.setdefault(row["uniqueName"], row)

    # Relic reward pool first: it fixes slugs and names for prime parts.
    rewards: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    for relic in relics_raw:
        parsed = _parse_relic_name(relic.get("name", ""))
        if not parsed:
            continue
        tier, code, quality = parsed
        base = f"{tier} {code}"
        vaulted = _relic_vaulted(relic)
        g = groups.get(base)
        if g is None:
            g = groups[base] = {"name": base, "tier": tier, "code": code, "vaulted": vaulted,
                                "imageName": relic.get("imageName"), "uniqueNames": {},
                                "wfmSlug": names.normalize_wfm_slug(((relic.get("marketInfo") or {}).get("urlName"))),
                                "rewards": {}}
        g["vaulted"] = g["vaulted"] and vaulted
        if quality == "intact" and relic.get("imageName"):
            g["imageName"] = relic["imageName"]
        g["uniqueNames"][quality] = relic.get("uniqueName")
        for r in relic.get("rewards") or []:
            it = r.get("item") or {}
            rname = it.get("name")
            if not rname:
                continue
            wm = it.get("warframeMarket") or {}
            slug = names.normalize_wfm_slug(wm.get("urlName") or wm.get("url_name")) if wm else None
            uniq = it.get("uniqueName") or ""
            if uniq.startswith("/Lotus/Types/Game/Projections/"):
                uniq = ""  # older WFCD put the relic's own uniqueName here
            chance = float(r.get("chance") or 0)
            rarity = relic_reward_rarity(quality, chance, r.get("rarity") or "Common")
            gr = g["rewards"].setdefault(rname, {"uniqueName": uniq, "name": rname, "rarity": rarity,
                                                 "wfmSlug": slug, "chances": {}})
            gr["chances"][quality] = chance
            if quality == "intact":
                gr["rarity"] = rarity
            pool = rewards.get(rname)
            if pool is None:
                comp = catalog.get(uniq) or catalog.get(_component_alias(uniq)) or {}
                parents = comp.get("parentUniqueNames") or []
                rewards[rname] = {"name": rname, "uniqueName": uniq, "wfmSlug": slug, "rarity": rarity,
                                  "ducats": _ducats(it) or _ducats(comp), "vaulted": vaulted,
                                  "parent": parents[0] if parents else None,
                                  "imageName": it.get("imageName") or comp.get("imageName")}
            else:
                pool["vaulted"] = pool["vaulted"] and vaulted

    reward_by_unique: dict[str, dict] = {}
    for r in rewards.values():
        if r["uniqueName"]:
            reward_by_unique.setdefault(r["uniqueName"], r)

    def slug_for_unique(u: str) -> str | None:
        r = reward_by_unique.get(u) or reward_by_unique.get(_blueprint_alias(u))
        return r["wfmSlug"] if r else None

    out_items: list[dict] = []
    seen: set[str] = set()
    part_rows: dict[str, dict] = {}
    for cat, row in cat_rows:
        u = row["uniqueName"]
        if u in seen:
            continue
        seen.add(u)
        comps = []
        tradable_parts = 0
        for ref in row.get("components") or []:
            cu = ref.get("uniqueName") or ""
            src = catalog.get(cu, {})
            cname = ref.get("name") or src.get("name") or cu.rsplit("/", 1)[-1]
            full = f"{row['name']} {cname}" if cu in comp_ids else cname
            ctrade = bool(src.get("tradable", ref.get("tradable"))) or cu in reward_by_unique \
                or _blueprint_alias(cu) in reward_by_unique
            if ctrade and cu in comp_ids:
                tradable_parts += 1
            cd = _ducats(src) or _ducats(ref)
            comps.append(_compact({"uniqueName": cu, "name": full, "ducats": cd,
                                   "itemCount": int(ref.get("itemCount") or 1), "tradable": ctrade}))
            if cu in comp_ids and cu not in part_rows:
                part_rows[cu] = {"uniqueName": cu, "name": full, "category": "Components",
                                 "type": "Component", "ducats": cd, "tradable": ctrade,
                                 "wfmSlug": slug_for_unique(cu) if ctrade else None,
                                 "imageName": src.get("imageName"), "parent": u,
                                 "vaulted": row.get("vaulted")}
        tradable = bool(row.get("tradable"))
        slug = None
        if tradable:
            slug = names.normalize_for_slug(row["name"])
        elif row.get("isPrime") and tradable_parts >= 2:
            slug = names.normalize_for_slug(row["name"] + " Set")
        out_items.append(_compact({
            "uniqueName": u, "name": row["name"], "category": cat, "type": row.get("type"),
            "ducats": _ducats(row), "vaulted": row.get("vaulted"), "tradable": tradable,
            "masteryReq": row.get("masteryReq"), "imageName": row.get("imageName"),
            "wfmSlug": slug, "components": comps,
        }))
    for cu, part in part_rows.items():
        if cu not in seen:
            seen.add(cu)
            out_items.append(_compact(part))
    # Relics as inventory items (one per refinement uniqueName).
    for relic in relics_raw:
        parsed = _parse_relic_name(relic.get("name", ""))
        if not parsed or not relic.get("uniqueName") or relic["uniqueName"] in seen:
            continue
        seen.add(relic["uniqueName"])
        out_items.append(_compact({
            "uniqueName": relic["uniqueName"], "name": relic["name"], "category": "Relics",
            "type": "Relic", "vaulted": _relic_vaulted(relic), "tradable": bool(relic.get("tradable", True)),
            "imageName": relic.get("imageName"),
            "wfmSlug": names.normalize_wfm_slug((relic.get("marketInfo") or {}).get("urlName")),
        }))

    out_rewards = [_compact(r) for r in sorted(rewards.values(), key=lambda r: r["name"])]
    out_relics = []
    for g in sorted(groups.values(), key=lambda g: (TIERS.index(g["tier"]), g["code"])):
        g = dict(g)
        g["rewards"] = [_compact(r) for r in g["rewards"].values()]
        out_relics.append(_compact(g))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {"schema": SCHEMA, "source": source, "version": version, "source_time": source_time or now,
            "built_at": now}
    os.makedirs(out_dir, exist_ok=True)
    sizes = {}
    for fname, body in ((ITEMS_FILE, {**meta, "items": out_items, "rewards": out_rewards}),
                        (RELICS_FILE, {**meta, "relics": out_relics})):
        path = os.path.join(out_dir, fname)
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with open(path + ".tmp", "wb") as f:
            f.write(gzip.compress(raw, 9, mtime=0))
        os.replace(path + ".tmp", path)
        sizes[fname] = {"json": len(raw), "gzip": os.path.getsize(path)}
    manifest = {**meta, "counts": {"items": len(out_items), "rewards": len(out_rewards), "relics": len(out_relics)},
                "files": sizes}
    with open(os.path.join(out_dir, MANIFEST_FILE + ".tmp"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    os.replace(os.path.join(out_dir, MANIFEST_FILE + ".tmp"), os.path.join(out_dir, MANIFEST_FILE))
    return manifest


def _component_alias(u: str) -> str:
    """WarframeRecipes/XPrimeChassisBlueprint (the reward) <-> XPrimeChassisComponent (the part)."""
    return u[: -len("Blueprint")] + "Component" if u.endswith("Blueprint") else u


def _blueprint_alias(u: str) -> str:
    return u[: -len("Component")] + "Blueprint" if u.endswith("Component") else u


def dir_reader(json_dir: str) -> Callable[[str], list | None]:
    def read(cat: str) -> list | None:
        path = os.path.join(json_dir, f"{cat}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return read


# ---- runtime refresh -------------------------------------------------

def _get(url: str, etag: str | None = None, timeout: float = 60) -> tuple[int, bytes, str | None]:
    """GET with If-None-Match; returns (status, body, etag). net.fetch hides headers."""
    hdrs = {"User-Agent": net.USER_AGENT, "Accept-Encoding": "gzip"}
    if etag:
        hdrs["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return resp.status, body, resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", etag
        raise net.HttpError(e.code, url) from e


def _state_path(runtime: str) -> str:
    return os.path.join(runtime, "refresh-state.json")


def _read_state(runtime: str) -> dict:
    try:
        with open(_state_path(runtime), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(runtime: str, state: dict) -> None:
    os.makedirs(runtime, exist_ok=True)
    tmp = _state_path(runtime) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path(runtime))


_refresh_lock = threading.Lock()


def refresh(force: bool = False, base_url: str = RAW_BASE,
            fetch: Callable[[str, str | None], tuple[int, bytes, str | None]] | None = None) -> dict:
    """Weekly update from WFCD's GitHub JSON into <runtime_dir>/data.

    Probes Relics/Components with ETags first so an unchanged week costs two
    304s; otherwise downloads every category (~25 MB), rebuilds and swaps the
    directory atomically. Any failure leaves the current data in place.
    """
    fetch = fetch or (lambda url, etag: _get(url, etag))
    _, runtime = _dirs()
    if not runtime:
        return {"status": "disabled"}
    if not _refresh_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        state = _read_state(runtime)
        if not force and time.time() - float(state.get("checked_at", 0)) < REFRESH_INTERVAL:
            return {"status": "fresh", "checked_at": state.get("checked_at")}
        etags: dict = dict(state.get("etags") or {})
        changed = force or _read_manifest(runtime) is None
        probed: dict[str, bytes] = {}
        if not changed:
            for cat in _PROBE_FILES:
                status, body, tag = fetch(base_url + f"{cat}.json", etags.get(cat))
                if status != 304:
                    changed = True
                    probed[cat] = body
                    etags[cat] = tag
        if not changed:
            state["checked_at"] = time.time()
            _write_state(runtime, state)
            return {"status": "unchanged"}

        docs: dict[str, list] = {}
        for cat in SOURCE_FILES:
            body = probed.get(cat)
            if body is None:
                _, body, tag = fetch(base_url + f"{cat}.json", None)
                etags[cat] = tag
            docs[cat] = json.loads(body)
        tmp_dir = runtime + ".new"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        manifest = build_bundle(docs.get, tmp_dir, version="github-master", source="WFCD/warframe-items@master")
        if manifest["counts"]["relics"] < MIN_REFRESH_RELICS or manifest["counts"]["items"] < MIN_REFRESH_ITEMS:
            raise ValueError(f"refusing suspicious refresh: {manifest['counts']}")
        old = runtime + ".old"
        shutil.rmtree(old, ignore_errors=True)
        state_keep = {"etags": etags, "checked_at": time.time()}
        if os.path.isdir(runtime):
            os.replace(runtime, old)
        os.replace(tmp_dir, runtime)
        shutil.rmtree(old, ignore_errors=True)
        _write_state(runtime, state_keep)
        reset()
        _log.info("game data refreshed: %s", manifest["counts"])
        return {"status": "updated", "counts": manifest["counts"]}
    finally:
        _refresh_lock.release()


def status() -> dict:
    d, src = active_data_dir()
    bundle, runtime = _dirs()
    return {"active": src, "dir": d, "bundle": _read_manifest(bundle), "runtime": _read_manifest(runtime),
            "last_check": _read_state(runtime).get("checked_at") if runtime else None}


# ---- worker wiring -----------------------------------------------------

def setup(app) -> None:
    def kick() -> None:
        if app.settings.get("data_auto_refresh", True):
            app.submit(refresh)

    app.services["items"] = items
    if os.environ.get("WFH_HEADLESS"):
        return  # tests and CLI runs never touch the network on their own

    def warm() -> None:
        from .relics import relics

        items()
        relics()

    # Parse once up front so the first reward scan does not pay for it.
    app.submit(warm)
    # Late and in the background: never compete with plugin start or the game launch.
    t = threading.Timer(120, kick)
    t.daemon = True
    t.start()


@rpc.method("data.status")
async def _rpc_status() -> dict:
    from ..app import get_app

    app = get_app()
    out = await app.run_blocking(status)
    out["items"] = (await app.run_blocking(items)).status()
    return out


@rpc.method("data.refresh")
async def _rpc_refresh(force: bool = True) -> dict:
    from ..app import get_app

    return await get_app().run_blocking(refresh, force)


@rpc.method("items.search")
async def _rpc_search(query: str, limit: int = 20) -> list[dict]:
    from ..app import get_app

    db = await get_app().run_blocking(items)
    return [i.to_json() for i in db.search(query, limit)]


def _main(argv: list[str]) -> int:
    """python -m wfh.data.items build <wfcd data/json dir> <out dir> [version] [source_time]"""
    if len(argv) < 3 or argv[0] != "build":
        print(_main.__doc__)
        return 2
    m = build_bundle(dir_reader(argv[1]), argv[2], argv[3] if len(argv) > 3 else "",
                     argv[4] if len(argv) > 4 else "")
    print(json.dumps(m, indent=1))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
