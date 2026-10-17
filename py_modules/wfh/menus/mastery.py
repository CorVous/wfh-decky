"""Mastery page / QAM view-models (docs/port/menus.md §5).

Port of WFHelper services/masteryHelper.ts computeMasteryProgress (rank per owned
item from XP, XPInfo for sold gear, star chart + intrinsics mastery), the summary
strip and projection (MasteryView buildMasterySummary, src/lib/masteryProjection.ts)
and the roadmap (src/lib/masteryRoadmap.ts buildMasteryRoadmap: easy / relics /
platinum lists).

Data: the item db (wfh.data.items) has names, components and market slugs but
not what grants mastery, so scripts/fetch-data.sh builds data/mastery.json.gz
from warframe-public-export-plus (ExportWarframes/Weapons/Sentinels, maxLevelCap,
ExportRecipes for blueprints and Market availability, ExportRegions node mastery)
plus @wfcd/items (`masterable`, `exalted`, vault status):

    python -m wfh.menus.mastery build <pep package dir> <wfcd data/json dir> <out dir>

Pure functions (tested in tests/test_menus_mastery.py): rank_to_xp, xp_to_rank,
build_payload, MasteryData, compute_progress, summary, projection, part_states,
easy_access, relic_completion, build_roadmap, estimate_purchase_cost.

RPCs
  mastery.summary()                              MR, XP to next, totals, by category, completion
  mastery.items(category, status, sort, offset, limit)   collection list (paged)
  mastery.item(unique_name)                      one item with its parts
  mastery.roadmap(limit)                         easy / relics / platinum lists
  mastery.planned()                              pinned items with their parts
  mastery.pin(unique_name, pinned)               settings mastery.pinned (max 40)
  mastery.qam()                                  MR line + top easy items
  mastery.configure(hide_founder?)               settings mastery.hide_founder
Event: mastery.updated (after a new inventory snapshot).
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import sys
import threading
import time
from functools import lru_cache
from typing import Any, Callable, Iterable

from .. import log, rpc

_log = log.get("menus.mastery")

DATA_FILE = "mastery.json.gz"
SCHEMA = 1

MAX_ITEM_RANK = 30
WEAPON_AFFINITY = 500    # per rank^2
SUIT_AFFINITY = 1000
JUNCTION_XP = 1000
INTRINSIC_XP = 1500
MAX_INTRINSIC_RANK = 10
MR_XP_PER_RANK_SQ = 2500
MR30_XP = MR_XP_PER_RANK_SQ * 900
LEGENDARY_XP = 147_500
GILDED_FEATURE_MASK = 8
PIN_LIMIT = 40
PRICE_VIEW_TTL = 5.0      # s a priced item list is reused before prices are re-read
PREFETCH_LIMIT = 150      # slugs mastery.roadmap queues for price prefetch per call

FOUNDER_ITEMS = ("Excalibur Prime", "Lato Prime", "Skana Prime")

# MasteryView CAT_ORDER, "Incomplete sets" is a pseudo tab there; here a status filter.
CAT_ORDER = ["Warframes", "Primary", "Secondary", "Melee", "Companions", "Archwing", "Amps", "Necramech", "Misc"]
STATUSES = ("all", "missing", "progress", "mastered")
SORTS = ("name", "owned", "parts_owned", "mastery_xp", "platinum")

EQUIPMENT_COLLECTIONS = ("Suits", "LongGuns", "Pistols", "Melee", "Sentinels", "SentinelWeapons", "SpaceSuits",
                         "SpaceGuns", "SpaceMelee", "OperatorAmps", "MechSuits")
MODULAR_COLLECTIONS = ("Hoverboards", "MoaPets", "KubrowPets")
SUIT_INVENTORY_KEYS = {"Suits", "Sentinels", "SpaceSuits", "MechSuits", "KubrowPets", "MoaPets", "Hoverboards"}
MASTERED_FLAG_KEYS = ("Mastered", "mastered", "IsMastered", "isMastered", "Completed", "completed",
                      "IsComplete", "isComplete")

ACCESS_PRIORITY = {"owned": 0, "gild": 1, "claimable": 2, "building": 3, "buildable": 4, "foundryParts": 5,
                   "craftParts": 6, "marketBlueprint": 7}
ACCESS_LABEL = {"owned": "Level it", "gild": "Gild it", "claimable": "Claim", "building": "In foundry",
                "buildable": "Build", "foundryParts": "Parts building", "craftParts": "Craft parts",
                "marketBlueprint": "Buy blueprint"}


# ---- account rank math (config/shared/masteryXp.ts) ------------------------------------------

def rank_to_xp(rank: int) -> int:
    if rank <= 30:
        return MR_XP_PER_RANK_SQ * rank * rank
    return MR30_XP + (rank - 30) * LEGENDARY_XP


def xp_to_rank(xp: float) -> int:
    if xp >= MR30_XP:
        return 30 + int((xp - MR30_XP) // LEGENDARY_XP)
    return int(math.floor(math.sqrt(max(0.0, xp) / MR_XP_PER_RANK_SQ)))


def mr_label(rank: int) -> str:
    return f"L{rank - 30}" if rank > 30 else str(rank)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v):
        return v
    if isinstance(v, str):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


# ---- data bundle (build time) ------------------------------------------------------------------

_LEADING_TOKEN = re.compile(r"^<[^>]{1,24}>\s*")
PRODUCT_DISPLAY = {"Suits": "Warframes", "LongGuns": "Primary", "Pistols": "Secondary", "Melee": "Melee",
                   "Sentinels": "Companions", "SentinelWeapons": "Companions", "SpaceSuits": "Archwing",
                   "SpaceGuns": "Archwing", "SpaceMelee": "Archwing", "OperatorAmps": "Amps",
                   "MechSuits": "Necramech", "CrewShipWeapons": "Railjack"}
PATH_CATEGORY_RULES = [
    (re.compile(r"/OperatorAmps?/", re.I), "Amps"),
    (re.compile(r"/OperatorAmplifiers?/", re.I), "Amps"),
    (re.compile(r"/Sentinels/.*Weapons?/", re.I), "Companions"),
    (re.compile(r"/Sentinels?/", re.I), "Companions"),
    (re.compile(r"/Pets?/", re.I), "Companions"),
    (re.compile(r"/SpaceSuits?/", re.I), "Archwing"),
    (re.compile(r"/SpaceGuns?/", re.I), "Archwing"),
    (re.compile(r"/SpaceMelee/", re.I), "Archwing"),
    (re.compile(r"/MechSuits?/", re.I), "Necramech"),
    (re.compile(r"/CrewShip.*Weapons?/", re.I), "Railjack"),
    (re.compile(r"/Suits/", re.I), "Warframes"),
    (re.compile(r"/ModularMelee\b|/Ostron.*Melee|/Zaw", re.I), "Melee"),
    (re.compile(r"/ModularPistol|/SolarisUnited.*Secondary|/Kitgun.*Pistol", re.I), "Secondary"),
    (re.compile(r"/ModularPrimary|/SolarisUnited.*Primary|/Kitgun.*Rifle", re.I), "Primary"),
    (re.compile(r"/LongGuns/", re.I), "Primary"),
    (re.compile(r"/Pistols/", re.I), "Secondary"),
    (re.compile(r"/Melee/", re.I), "Melee"),
]
MASTERABLE_DB_CATEGORIES = {"Warframe", "Weapon", "Companion", "Railjack", "Warframes", "Primary", "Secondary",
                            "Melee", "Companions", "Archwing"}
EXALTED_NAMES = {"regulators", "regulators prime", "iron staff", "iron staff prime", "exalted blade",
                 "exalted blade prime", "dex pixia", "dex pixia prime", "diwata", "diwata prime", "artemis bow",
                 "artemis bow prime", "valkyr talons", "valkyr talons prime", "desert wind", "desert wind prime",
                 "shattered lash"}
KITGUN_CHAMBERS = {"catchmoon", "gaze", "rattleguts", "sporelacer", "tombfinger", "vermisplicer"}
ZAW_STRIKE = re.compile(r"/Ostron/Melee/ModularMelee", re.I)
MODULAR_COMPANION_MODEL = re.compile(
    r"/Pets/(?:MoaPets/MoaPetParts/MoaPetHead[^/]*|ZanukaPets/ZanukaPetParts/ZanukaPetPartHead[ABC])$", re.I)
VINQUIBUS_PRIMARY = "/Lotus/Weapons/Tenno/Bayonet/TnBayonetRifleWeapon"
VINQUIBUS_MELEE = "/Lotus/Weapons/Tenno/Bayonet/TnBayonetMeleeWeapon"
VENARI = re.compile(r"/Powersuits/Khora/Kavat/Khora(?:Prime)?KavatPowerSuit$", re.I)
SUIT_CATEGORIES = {"Warframes", "Companions", "Necramech"}
SUIT_PATH = re.compile(r"/(?:SpaceSuits?|Powersuits/Archwing|Hoverboard)/", re.I)
PLEXUS = {"u": "/Lotus/Types/Game/CrewShip/RailjackHarness", "n": "Plexus", "cat": "Companions", "max": 30,
          "rate": SUIT_AFFINITY // 5}
UNIQUE_ALIASES = {
    "/Lotus/Types/Game/CrewShip/RailjackHarness": ["/Lotus/Types/Game/CrewShip/RailJack/DefaultHarness"],
    "/Lotus/Types/Friendly/Pets/ZanukaPets/ZanukaPetParts/ZanukaPetPartHeadA":
        ["/Lotus/Types/Friendly/Pets/ZanukaPets/ZanukaPetAPowerSuit"],
    "/Lotus/Types/Friendly/Pets/ZanukaPets/ZanukaPetParts/ZanukaPetPartHeadB":
        ["/Lotus/Types/Friendly/Pets/ZanukaPets/ZanukaPetBPowerSuit"],
    "/Lotus/Types/Friendly/Pets/ZanukaPets/ZanukaPetParts/ZanukaPetPartHeadC":
        ["/Lotus/Types/Friendly/Pets/ZanukaPets/ZanukaPetCPowerSuit"],
    VINQUIBUS_PRIMARY: [VINQUIBUS_MELEE],
}
PEP_TABLES = (("ExportWarframes", "Warframe"), ("ExportWeapons", "Weapon"), ("ExportSentinels", "Companion"))
# WFHelper loadWfcdItems order; only these can hold mastery items.
WFCD_CATEGORIES = ("Warframes", "Primary", "Secondary", "Melee", "Sentinels", "Pets", "Archwing", "Arch-Gun",
                   "Arch-Melee", "Misc")


def sanitize_name(name: str | None) -> str:
    raw = str(name or "").strip()
    return _LEADING_TOKEN.sub("", raw).strip() or raw


def _fallback_name(unique: str) -> str:
    last = unique.rsplit("/", 1)[-1] or "Unknown"
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    return sanitize_name(re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", last))


def display_category(item: dict, unique: str) -> str:
    """masteryHelper.ts resolveDisplayCategoryInfo."""
    typ = str(item.get("type") or "")
    if re.search(r"/Hoverboard/", unique, re.I) or re.search(r"k-drive", typ, re.I):
        return "Misc"
    if re.search(r"/Pets?/", unique, re.I) or re.search(r"\bpets?\b", typ, re.I):
        return "Companions"
    if re.search(r"/OperatorAmplifiers?/", unique, re.I):
        return "Amps"
    if ZAW_STRIKE.search(unique):
        return "Melee"
    pc = item.get("productCategory")
    if pc and pc in PRODUCT_DISPLAY:
        return PRODUCT_DISPLAY[pc]
    for pat, cat in PATH_CATEGORY_RULES:
        if pat.search(unique):
            return cat
    return {"Warframe": "Warframes", "Companion": "Companions", "Railjack": "Railjack"}.get(item.get("category"),
                                                                                            "Other")


def _amp_prism(name: str, unique: str) -> bool:
    return bool(re.search(r"/OperatorAmplifiers?/", unique, re.I) and re.search(r"/Barrel/|Barrel$", unique, re.I)
                and " prism" in name.lower())


def _kitgun_chamber(name: str, unique: str) -> bool:
    return bool(re.search(r"/(?:Barrel|Barrels)/", unique, re.I)) and name.strip().lower() in KITGUN_CHAMBERS


def exclude_reason(unique: str, name: str, item: dict) -> str | None:
    """masteryHelper.ts getExcludeReason."""
    if unique == VINQUIBUS_MELEE:
        return "shared-mastery-variant"
    for part, why in (("/Recipes/", "recipe"), ("/StoreItems/", "store-item"), ("/OperatorLoadOuts/", "loadout"),
                      ("/QuestVersions/", "quest-version"), ("/PrototypeVersions/", "prototype-version")):
        if part in unique:
            return why
    if item.get("exalted") is True:
        return "exalted"
    if item.get("productCategory") == "SpecialItems" and not VENARI.search(unique):
        return "special-items"
    if re.search(r"exalted", str(item.get("type") or ""), re.I) or re.search(r"/ExaltedWeapons?/", unique, re.I):
        return "exalted"
    if re.search(r"/SpecialItems/", unique, re.I):
        return "special-items"
    if name and name.lower() in EXALTED_NAMES:
        return "exalted"
    for pat, why in ((r"/Cosmetics?/", "cosmetic"), (r"/Decorations?/", "decoration"), (r"/NPC/", "npc"),
                     (r"/Test/", "test"), (r"/Developers?/", "developer"), (r"/FixedGun", "fixed-gun")):
        if re.search(pat, unique, re.I):
            return why
    if re.search(r"/SentTrainingAmps?/|/SentTrainingAmplifiers?/", unique, re.I):
        return None if _amp_prism(name, unique) else "training-amp"
    if re.search(r"/Pets/(?:MoaPets/MoaPet|ZanukaPets/ZanukaPet[ABC])PowerSuit$", unique):
        return "modular-pet-chassis"
    n = (name or "").lower()
    if n.endswith(" blueprint") or n.endswith(" component"):
        return "blueprint"
    return None


def _heuristic_max_rank(unique: str, name: str) -> int:
    text = f"{unique} {name}"
    path = unique.lower()
    if (re.search(r"(?:^|[\s/])(kuva|tenet|coda)(?:[\s/]|$)", text, re.I) or "infestedlich" in path
            or name.lower() == "paracesis" or "ballassword" in path or "mechsuits" in path or "entrati" in path):
        return 40
    return MAX_ITEM_RANK


def build_payload(pep: dict[str, dict], dictionary: dict, wfcd: dict[str, list], version: str = "",
                  recipes: dict | None = None, regions: dict | None = None) -> dict:
    """Masterable items + recipe index + node mastery from the export tables (see module doc)."""

    def res(v: Any) -> str:
        if not isinstance(v, str) or not v:
            return ""
        return dictionary.get(v, "") if v.startswith("/") else v

    entries: dict[str, dict] = {}
    caps: dict[str, int] = {}
    known: set[str] = set()
    suit_types: list[str] = []
    for table, cat in PEP_TABLES:
        for u, it in (pep.get(table) or {}).items():
            if not u or u == "default" or not isinstance(it, dict):
                continue
            known.add(u)
            cap = _num(it.get("maxLevelCap"))
            if cap is not None and cap > MAX_ITEM_RANK:
                caps[u] = int(cap)
            if cat in ("Warframe", "Companion"):
                suit_types.append(u)
            name = sanitize_name(res(it.get("name")))
            entries[u] = {"name": name or _fallback_name(u), "fallback": not name, "category": cat,
                          "productCategory": it.get("productCategory"), "masteryReq": it.get("masteryReq") or 0,
                          "type": "", "pep": True}
    for cat in WFCD_CATEGORIES:
        for row in wfcd.get(cat) or []:
            u = row.get("uniqueName")
            if not u or not isinstance(row, dict):
                continue
            name = sanitize_name(row.get("name") or "Unknown")
            e = entries.get(u)
            if e is None:
                e = entries[u] = {"name": name, "category": row.get("category") or cat,
                                  "productCategory": row.get("productCategory"),
                                  "masteryReq": row.get("masteryReq") or 0, "type": row.get("type") or ""}
            else:
                if (e.get("fallback") or e["name"].startswith("/Lotus/")) and row.get("name"):
                    e["name"] = name
                    e.pop("fallback", None)
                if not e.get("productCategory") and row.get("productCategory"):
                    e["productCategory"] = row["productCategory"]
                if not e.get("type") and row.get("type"):
                    e["type"] = row["type"]
            if isinstance(row.get("masterable"), bool):
                e["masterable"] = row["masterable"]
            e["exalted"] = row.get("exalted") is True
            if row.get("vaulted"):
                e["vaulted"] = True
            if row.get("tradable") is not None:
                e["tradable"] = bool(row.get("tradable"))
            e["isPrime"] = bool(row.get("isPrime")) or "Prime" in e["name"]

    items: list[dict] = []
    seen: set[str] = set()
    for u, e in entries.items():
        name = e["name"]
        if e.get("category") not in MASTERABLE_DB_CATEGORIES:
            continue
        override = _amp_prism(name, u) or _kitgun_chamber(name, u) or bool(VENARI.search(u))
        if e.get("masterable") is False and not override:
            continue
        if exclude_reason(u, name, e):
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cat = display_category(e, u)
        if cat == "Railjack":
            continue
        if u in caps:
            max_rank = max(caps[u], MAX_ITEM_RANK)
        elif u in known:
            max_rank = MAX_ITEM_RANK
        else:
            max_rank = _heuristic_max_rank(u, name)
        suit = cat in SUIT_CATEGORIES or bool(SUIT_PATH.search(u))
        row = {"u": u, "n": name, "cat": cat, "max": max_rank, "rate": (SUIT_AFFINITY if suit else WEAPON_AFFINITY) // 5}
        if e.get("masteryReq"):
            row["req"] = int(e["masteryReq"])
        if "Prime" in name:
            row["prime"] = 1
        if e.get("vaulted"):
            row["vaulted"] = 1
        items.append(row)
    if PLEXUS["n"].lower() not in seen:
        items.append(dict(PLEXUS))
    items.sort(key=lambda r: r["n"].lower())

    masterable = {r["u"] for r in items}
    # Export-known types that are not bundle rows (name-dedupe variants, non-masterable rows)
    # but that the name heuristic would over-cap at 40: getMasteryMaxRank returns 30 for them.
    known30 = sorted(u for u in known if u not in caps and u not in masterable
                     and _heuristic_max_rank(u, (entries.get(u) or {}).get("name", "")) > MAX_ITEM_RANK)
    part_ids: set[str] = set()
    for cat in WFCD_CATEGORIES:
        for row in wfcd.get(cat) or []:
            if row.get("uniqueName") in masterable:
                for c in row.get("components") or []:
                    if c.get("uniqueName"):
                        part_ids.add(c["uniqueName"])
    recipe_rows = recipes if recipes is not None else (pep.get("ExportRecipes") or {})
    blueprints: dict[str, str] = {}
    market: dict[str, int | None] = {}
    for bp, r in recipe_rows.items():
        result = r.get("resultType") if isinstance(r, dict) else None
        if not result or not isinstance(r.get("ingredients"), list):
            continue
        if result in masterable or result in part_ids or bp in part_ids:
            blueprints[bp] = result
        if result in masterable and r.get("excludeFromMarket") is not True and result not in market:
            c = _num(r.get("creditsCost"))
            market[result] = int(c) if c is not None else None
    for row in items:
        if row["u"] in market:
            row["market"] = 1
            if market[row["u"]] is not None:
                row["credits"] = market[row["u"]]

    region_rows = regions if regions is not None else (pep.get("ExportRegions") or {})
    nodes: dict[str, list] = {}
    for tag, node in region_rows.items():
        if not isinstance(node, dict):
            continue
        junction = node.get("missionType") == "MT_JUNCTION"
        nodes[tag] = [JUNCTION_XP if junction else int(_num(node.get("masteryExp")) or 0), 1 if junction else 0]

    return {"schema": SCHEMA, "source": "warframe-public-export-plus + @wfcd/items", "version": version,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "items": items,
            "caps": caps, "known": len(known), "known30": known30, "suitTypes": sorted(suit_types), "blueprints": blueprints,
            "regions": nodes}


def build(pep_dir: str, wfcd_dir: str, out_dir: str, label: str = "") -> str:
    def load(path: str) -> Any:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    pep = {t: load(os.path.join(pep_dir, t + ".json")) for t, _ in PEP_TABLES}
    pep["ExportRecipes"] = load(os.path.join(pep_dir, "ExportRecipes.json"))
    pep["ExportRegions"] = load(os.path.join(pep_dir, "ExportRegions.json"))
    wfcd = {}
    for cat in WFCD_CATEGORIES:
        p = os.path.join(wfcd_dir, cat + ".json")
        if os.path.exists(p):
            wfcd[cat] = load(p)
    try:
        version = "pep " + str(load(os.path.join(pep_dir, "package.json")).get("version"))
    except (OSError, ValueError):
        version = ""
    payload = build_payload(pep, load(os.path.join(pep_dir, "dict.en.json")), wfcd, label or version)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, DATA_FILE)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(path + ".tmp", "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    os.replace(path + ".tmp", path)
    return f"{path}: {len(payload['items'])} masterable items, {len(payload['blueprints'])} blueprints, " \
           f"{len(payload['regions'])} nodes, {os.path.getsize(path)} bytes"


# ---- data bundle (runtime) -----------------------------------------------------------------------

class MasteryData:
    def __init__(self, payload: dict):
        self.version = str(payload.get("version") or "")
        self.items: list[dict] = list(payload.get("items") or [])
        self.by_unique = {r["u"]: r for r in self.items}
        self.caps: dict[str, int] = dict(payload.get("caps") or {})
        self.known30: set[str] = set(payload.get("known30") or [])
        self.suit_types = set(payload.get("suitTypes") or [])
        self.blueprints: dict[str, str] = dict(payload.get("blueprints") or {})
        self.regions: dict[str, list] = dict(payload.get("regions") or {})

    def max_rank(self, unique: str, fallback: int = MAX_ITEM_RANK, name: str = "") -> int:
        """getMasteryMaxRank: export cap, else the bundle's rank, else name heuristics."""
        if unique in self.caps:
            return max(self.caps[unique], fallback)
        hit = self.by_unique.get(unique)
        if hit is not None:
            return max(hit["max"], fallback)
        if unique in self.known30:
            return max(MAX_ITEM_RANK, fallback)
        if fallback > MAX_ITEM_RANK:
            return fallback
        return _heuristic_max_rank(unique, name)


_data_lock = threading.Lock()
_data: MasteryData | None = None
_data_dir: str | None = None
_data_missing_at: float | None = None   # monotonic time of the last failed load
MISSING_RETRY_S = 300.0


def set_data_dir(path: str | None) -> None:
    global _data, _data_dir, _data_missing_at
    with _data_lock:
        _data_dir, _data, _data_missing_at = path, None, None


def data() -> MasteryData | None:
    """The bundle, or None when scripts/fetch-data.sh has not built it (a missing bundle is
    remembered: retried after MISSING_RETRY_S or set_data_dir, warned about once)."""
    global _data, _data_missing_at
    with _data_lock:
        if _data is not None:
            return _data
        if _data_missing_at is not None and time.monotonic() - _data_missing_at < MISSING_RETRY_S:
            return None
        d = _data_dir
        if d is None:
            try:
                from ..app import get_app

                d = get_app().data_dir
            except Exception:  # noqa: BLE001
                return None
        try:
            with gzip.open(os.path.join(d, DATA_FILE), "rb") as f:
                _data = MasteryData(json.loads(f.read()))
        except (OSError, ValueError) as e:
            if _data_missing_at is None:
                _log.warning("mastery data unavailable: %s", e)
            _data_missing_at = time.monotonic()
            return None
        return _data


# ---- progress (computeMasteryProgress) -----------------------------------------------------------

def _xp_rank(xp: float, max_rank: int, per_sq: int) -> int:
    if not xp or xp <= 0:
        return 0
    return min(max_rank, int(math.floor(math.sqrt(xp / per_sq))))


def _mastered_flag(entry: dict) -> bool | None:
    for k in MASTERED_FLAG_KEYS:
        v = entry.get(k)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "yes", "1"):
                return True
            if s in ("false", "no", "0"):
                return False
    return None


def _owned_record(entry: dict, md: MasteryData, fallback_max: int, owned: bool, per_sq: int,
                  name_of: Callable[[str], str | None]) -> dict | None:
    t = entry.get("ItemType")
    if not isinstance(t, str) or not t:
        return None
    max_rank = md.max_rank(t, fallback_max, name_of(t) or "")
    unlocked = max_rank
    if owned and max_rank > MAX_ITEM_RANK:
        pol = max(0, int(_num(entry.get("Polarized")) or 0))
        unlocked = min(max_rank, MAX_ITEM_RANK + pol * 2)
    xp_rank = _xp_rank(_num(entry.get("XP")) or 0, unlocked, per_sq)
    feat_rank = 0
    if max_rank > MAX_ITEM_RANK:
        f = _num(entry.get("Features"))
        if f is not None:
            r = int(f) + 1
            feat_rank = r if MAX_ITEM_RANK < r <= max_rank else 0
    rank = min(unlocked, max(xp_rank, feat_rank))
    flag = _mastered_flag(entry)
    rec = {"rank": rank, "masteryRank": rank, "maxRank": max_rank, "owned": owned,
           "mastered": flag is True or rank >= max_rank, "perRank": per_sq // 5}
    if not owned:
        rec["fromXPInfo"] = True
    return rec


def _requires_gilding(key: str, entry: dict, part: str | None) -> bool:
    if key == "MoaPets":
        return True
    if key == "KubrowPets":
        return isinstance(entry.get("ModularParts"), list)
    if key == "OperatorAmps":
        return part is not None
    return key in ("LongGuns", "Pistols", "Melee") and part is not None


def _gilded(entry: dict) -> bool:
    f = _num(entry.get("Features"))
    return f is not None and (int(f) & GILDED_FEATURE_MASK) != 0


def _better(cur: dict | None, cand: dict | None) -> dict | None:
    if cand is None:
        return cur
    if cur is None:
        return cand
    mr = max(cur["masteryRank"], cand["masteryRank"])
    for key in ("mastered", "rank", "maxRank", "owned"):
        if cand[key] != cur[key]:
            return {**(cand if cand[key] > cur[key] else cur), "masteryRank": mr}
    return {**cur, "masteryRank": mr}


def mission_xp(inv: dict, md: MasteryData) -> int:
    """Node mastery, granted once normally and again on Steel Path."""
    xp = 0
    for e in inv.get("Missions") or []:
        if not isinstance(e, dict):
            continue
        tag = e.get("Tag")
        node = md.regions.get(tag) if isinstance(tag, str) else None
        if node is None:
            continue
        completes = _num(e.get("Completes")) or 0
        steel = _num(e.get("Tier")) == 1
        if completes <= 0 and not steel:
            continue
        xp += node[0] * (2 if steel else 1)
    return xp


def intrinsic_xp(inv: dict) -> int:
    skills = inv.get("PlayerSkills")
    if not isinstance(skills, dict):
        return 0
    ranks = 0
    for k, v in skills.items():
        r = _num(v)
        if k.startswith("LPS_") and r is not None and r > 0:
            ranks += min(int(r), MAX_INTRINSIC_RANK)
    return ranks * INTRINSIC_XP


def completion(inv: dict, md: MasteryData) -> dict:
    """Star chart + intrinsic completion (display only)."""
    normal_total = junction_total = 0
    for xp, junction in md.regions.values():
        if xp <= 0:
            continue
        if junction:
            junction_total += 1
        else:
            normal_total += 1
    nd = jd = sd = sjd = 0
    for e in inv.get("Missions") or []:
        if not isinstance(e, dict):
            continue
        node = md.regions.get(e.get("Tag")) if isinstance(e.get("Tag"), str) else None
        if node is None or node[0] <= 0:
            continue
        steel = (_num(e.get("Tier")) or 0) >= 1
        if (_num(e.get("Completes")) or 0) <= 0 and not steel:
            continue
        if node[1]:
            jd += 1
            sjd += 1 if steel else 0
        else:
            nd += 1
            sd += 1 if steel else 0
    skills = inv.get("PlayerSkills") if isinstance(inv.get("PlayerSkills"), dict) else {}

    def pair(pat: str) -> dict:
        keys = [k for k in skills if re.match(pat, k)]
        done = sum(min(int(_num(skills[k]) or 0), MAX_INTRINSIC_RANK) for k in keys if (_num(skills[k]) or 0) > 0)
        return {"done": done, "total": len(keys) * MAX_INTRINSIC_RANK}

    return {"starChart": {"normal": {"done": nd, "total": normal_total},
                          "junctions": {"done": jd, "total": junction_total},
                          "steelPath": {"done": sd, "total": normal_total},
                          "steelPathJunctions": {"done": sjd, "total": junction_total}},
            "intrinsics": {"railjack": pair(r"^LPS_(?!DRIFT_)"), "drifter": pair(r"^LPS_DRIFT_")}}


def profile_mastery(inv: dict, total_xp: float | None) -> dict | None:
    rank = None
    for k in ("MasteryRank", "MasteryLevel", "PlayerLevel", "PlayerRank"):
        v = _num(inv.get(k))
        if v is not None:
            rank = int(v)
            break
    if rank is None and total_xp is not None:
        rank = xp_to_rank(total_xp)
    if rank is None:
        return None
    if total_xp is None:
        return {"rank": rank, "percentToNext": None, "totalXp": None, "xpIntoRank": None, "xpForNext": None,
                "testReady": False}
    cur, nxt = rank_to_xp(rank), rank_to_xp(rank + 1)
    span = nxt - cur
    if total_xp >= nxt:
        return {"rank": rank, "percentToNext": 100, "totalXp": total_xp, "xpIntoRank": total_xp - cur,
                "xpForNext": span, "testReady": True}
    if total_xp < cur:
        # Reconstruction undercounts big accounts: trust the game's rank, drop the bar.
        return {"rank": rank, "percentToNext": None, "totalXp": total_xp, "xpIntoRank": None, "xpForNext": None,
                "testReady": False}
    into = total_xp - cur
    return {"rank": rank, "percentToNext": min(100.0, round(into / span * 100, 1)), "totalXp": total_xp,
            "xpIntoRank": into, "xpForNext": span, "testReady": False}


def compute_progress(inv: dict | None, md: MasteryData, name_of: Callable[[str], str | None] | None = None,
                     hide_founder: bool = False) -> dict:
    """{items: [...], stats: {...}} with WFHelper's item fields (minus components, added on hydration)."""
    name_of = name_of or (lambda u: (md.by_unique.get(u) or {}).get("n"))
    if not isinstance(inv, dict):
        return {"items": [], "stats": {"total": 0, "mastered": 0, "inProgress": 0, "missing": 0, "byCategory": {},
                                       "profileMastery": None, "completion": completion({}, md)}}
    masterable = md.by_unique
    owned_map: dict[str, dict] = {}
    for key in EQUIPMENT_COLLECTIONS + MODULAR_COLLECTIONS:
        arr = inv.get(key)
        if not isinstance(arr, list):
            continue
        per_sq = SUIT_AFFINITY if key in SUIT_INVENTORY_KEYS else WEAPON_AFFINITY
        for entry in arr:
            if not isinstance(entry, dict):
                continue
            rec = _owned_record(entry, md, MAX_ITEM_RANK, True, per_sq, name_of)
            if rec is None:
                continue
            parts = entry.get("ModularParts")
            part = next((p for p in parts if isinstance(p, str) and p in masterable), None) \
                if isinstance(parts, list) else None
            if _requires_gilding(key, entry, part) and not _gilded(entry):
                rec.update(rank=0, masteryRank=0, mastered=False, needsGilding=True)
            k = part or entry["ItemType"]
            existing = owned_map.get(k)
            owned_map[k] = existing if existing and existing["rank"] > rec["rank"] else rec
    for entry in inv.get("XPInfo") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("ItemType"), str):
            continue
        t = entry["ItemType"]
        existing = owned_map.get(t)
        suit = (t in md.suit_types or bool(MODULAR_COMPANION_MODEL.search(t)) or bool(re.search(r"/Hoverboard", t, re.I))
                or bool(re.search(r"/CrewShip/(?:RailJack/Default)?Harness$", t, re.I))
                or t.endswith("RailjackHarness"))
        rec = _owned_record(entry, md, existing["maxRank"] if existing else MAX_ITEM_RANK, False,
                            SUIT_AFFINITY if suit else WEAPON_AFFINITY, name_of)
        if rec is None:
            continue
        if existing:
            owned_map[t] = {**existing, "maxRank": max(existing["maxRank"], rec["maxRank"]),
                            "mastered": existing["mastered"] or rec["mastered"],
                            "masteryRank": max(existing["masteryRank"], rec["masteryRank"])}
        else:
            owned_map[t] = rec
    by_name: dict[str, dict] = {}
    for u, rec in owned_map.items():
        n = name_of(u)
        if n:
            by_name[n.lower()] = rec

    items = []
    for row in md.items:
        if hide_founder and row["n"] in FOUNDER_ITEMS:
            continue
        rec = _better(owned_map.get(row["u"]), by_name.get(row["n"].lower()))
        for alias in UNIQUE_ALIASES.get(row["u"], []):
            rec = _better(rec, owned_map.get(alias))
        status, rank, max_rank, cur_owned, xp = "missing", 0, row["max"], False, 0
        if rec:
            rank, max_rank = rec["rank"], rec["maxRank"]
            cur_owned = rec["owned"] is not False
            status = "mastered" if rec["mastered"] or rank >= max_rank else "progress"
            credit = max_rank if rec["mastered"] else rec["masteryRank"]
            xp = min(credit, max_rank) * rec["perRank"]
        per_rank = rec["perRank"] if rec else row["rate"]
        item = {"name": row["n"], "uniqueName": row["u"], "category": row["cat"], "status": status, "rank": rank,
                "maxRank": max_rank, "currentlyOwned": cur_owned, "masteryXp": xp,
                "masteryXpRemaining": max(0, max_rank * per_rank - xp), "isPrime": bool(row.get("prime")),
                "vaulted": bool(row.get("vaulted")), "marketBuyable": bool(row.get("market")),
                "masteryReq": row.get("req", 0)}
        if rec and rec.get("needsGilding"):
            item["needsGilding"] = True
        items.append(item)

    total_xp = None
    if isinstance(inv.get("XPInfo"), list) and inv["XPInfo"]:
        total_xp = sum(i["masteryXp"] for i in items) + mission_xp(inv, md) + intrinsic_xp(inv)
    by_cat: dict[str, dict] = {}
    counts = {"mastered": 0, "progress": 0, "missing": 0}
    for i in items:
        c = by_cat.setdefault(i["category"], {"total": 0, "mastered": 0, "inProgress": 0, "missing": 0})
        c["total"] += 1
        c["inProgress" if i["status"] == "progress" else i["status"]] += 1
        counts[i["status"]] += 1
    return {"items": items, "stats": {"total": len(items), "mastered": counts["mastered"],
                                      "inProgress": counts["progress"], "missing": counts["missing"],
                                      "byCategory": by_cat, "profileMastery": profile_mastery(inv, total_xp),
                                      "completion": completion(inv, md)}}


# ---- foundry, parts and hydration ------------------------------------------------------------------

def _de_ms(obj: Any) -> int:
    if not isinstance(obj, dict):
        return 0
    d = obj.get("$date")
    if isinstance(d, dict):
        d = d.get("$numberLong")
    try:
        return int(d) if d is not None else 0
    except (TypeError, ValueError):
        return 0


def foundry_index(inv: dict, md: MasteryData, now_ms: int) -> dict[str, str]:
    """resultType -> claimable | building from PendingRecipes (claimable wins)."""
    out: dict[str, str] = {}
    for e in inv.get("PendingRecipes") or []:
        if not isinstance(e, dict) or not isinstance(e.get("ItemType"), str):
            continue
        bp = e["ItemType"]
        result = md.blueprints.get(bp)
        if result is None and bp.endswith("Blueprint"):
            result = bp[: -len("Blueprint")]
        if not result:
            continue
        done = _de_ms(e.get("CompletionDate"))
        state = "claimable" if done and done <= now_ms else "building"
        if out.get(result) != "claimable":
            out[result] = state
    return out


def _aliases(u: str) -> list[str]:
    out = [u]
    if u.endswith("Blueprint"):
        out.append(u[: -len("Blueprint")] + "Component")
    if u.endswith("Component"):
        out.append(u[: -len("Component")] + "Blueprint")
    if not u.endswith("Blueprint") and "/Types/Recipes/" in u:
        out.append(u + "Blueprint")
    return out


def is_part(u: str, md: MasteryData | None = None) -> bool:
    """Reservable part: a recipe path or a masterable ingredient (Bronco Prime for Akbronco), not a resource."""
    if not u or "/Types/Items/" in u or "/Types/Gameplay/" in u:
        return False
    return "/Types/Recipes/" in u or (md is not None and u in md.by_unique)


def part_states(components: Iterable, owned: Callable[[str], float], building: dict[str, str],
                md: MasteryData | None, main_blueprint: Callable[[str], bool]) -> list[dict]:
    """Merged part rows with owned / blueprint / building / missing; resources are dropped."""
    merged: dict[str, dict] = {}
    for c in components:
        u = getattr(c, "unique_name", None) or (c.get("uniqueName") if isinstance(c, dict) else None) or ""
        if not is_part(u, md):
            continue
        name = getattr(c, "name", None) or (c.get("name") if isinstance(c, dict) else "") or _fallback_name(u)
        cnt = int(getattr(c, "item_count", None) or (c.get("itemCount") if isinstance(c, dict) else 1) or 1)
        if u in merged:
            merged[u]["need"] += cnt
        else:
            merged[u] = {"uniqueName": u, "name": name, "need": cnt,
                         "tradable": bool(getattr(c, "tradable", False) or (isinstance(c, dict) and c.get("tradable")))}
    rows = []
    for u, r in merged.items():
        need = r["need"]
        exact = owned(u)
        if u.endswith("Blueprint"):
            alt = owned(u[: -len("Blueprint")] + "Component")
            built, bp = max(exact, alt), 0.0
            is_bp = not main_blueprint(u)
            if is_bp:
                built, bp = alt, exact
        else:
            built = exact
            bp = max((owned(a) for a in _aliases(u)[1:]), default=0.0)
        in_foundry = any(building.get(a) for a in _aliases(u))
        held = int(min(need, built + bp))
        if built >= need:
            state = "owned"
        elif built + bp >= need:
            state = "blueprint"
        elif in_foundry and held + 1 >= need:
            state = "building"
        else:
            state = "missing"
        rows.append({**r, "owned": int(min(built + bp, 999)), "state": state, "building": bool(in_foundry),
                     "missing": max(0, need - held - (1 if in_foundry else 0))})
    return rows


def build_readiness(parts: list[dict]) -> str | None:
    if not parts:
        return None
    states = [p["state"] for p in parts]
    if any(s in ("missing", "building") for s in states):
        return None
    return "craftParts" if "blueprint" in states else "buildable"


def easy_access(item: dict) -> str | None:
    """masteryRoadmap.ts easyAccess."""
    if item.get("needsGilding"):
        return "gild"
    if item.get("currentlyOwned"):
        return "owned"
    fs = item.get("foundryState")
    if fs == "claimable":
        return "claimable"
    if fs == "building":
        return "building"
    parts = item.get("parts") or []
    readiness = build_readiness(parts)
    if readiness == "buildable":
        return "buildable"
    missing = [p for p in parts if p["missing"] > 0]
    if parts and any(p["building"] for p in parts) and not missing:
        return "foundryParts"
    if readiness == "craftParts":
        return "craftParts"
    if item.get("marketBuyable") and missing and all(re.search(r"\bblueprint$", p["name"].strip(), re.I)
                                                     for p in missing):
        return "marketBlueprint"
    return None


@lru_cache(maxsize=16384)
def _norm_part(v: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", v.lower())
    return re.sub(r"(?:^|\s)blueprint$", "", s.strip()).strip()


class RelicPools:
    """Owned relic pools, normalised once: per pool (count, [(uniqueName lower, normalised
    name, chance 0..1)]) plus reverse indexes from reward uniqueName / normalised name to the
    pools that drop it, so relic_completion only visits pools holding one of an item's parts."""

    def __init__(self, pools: Iterable[tuple[int, list[tuple[str, str, float]]]] | None = None):
        self.pools: list[tuple[int, list[tuple[str, str, float]]]] = []
        self.by_unique: dict[str, set[int]] = {}
        self.by_name: dict[str, set[int]] = {}
        for count, rewards in pools or []:
            idx = len(self.pools)
            rows = []
            for ru, rn, ch in rewards:
                rl, nn = (ru or "").lower(), _norm_part(rn or "")
                rows.append((rl, nn, max(0.0, min(1.0, (ch or 0) / 100))))
                if rl:
                    self.by_unique.setdefault(rl, set()).add(idx)
                if nn:
                    self.by_name.setdefault(nn, set()).add(idx)
            self.pools.append((int(count), rows))

    def __len__(self) -> int:
        return len(self.pools)

    def candidates(self, matchers: list[tuple[set[str], str]]) -> list[int]:
        hit: set[int] = set()
        for aliases, name in matchers:
            for a in aliases:
                hit |= self.by_unique.get(a, set())
            hit |= self.by_name.get(name, set())
        return sorted(hit)


def relic_completion(item: dict, pools: "RelicPools | list[tuple[int, list[tuple[str, str, float]]]]") -> tuple[float, int] | None:
    """calculateOwnedRelicCompletion: P(owned relics finish every missing part); pools are
    (count, [(uniqueName, name, chance %)]) per owned relic quality, or a prebuilt RelicPools
    (build it once per pool set when scoring many items)."""
    missing = [p for p in item.get("parts") or [] if p["missing"] > 0]
    if not missing or not pools:
        return None
    if not isinstance(pools, RelicPools):
        pools = RelicPools(pools)
    iname = _norm_part(item["name"])
    matchers = []
    for p in missing:
        cn = _norm_part(p["name"])
        matchers.append(({a.lower() for a in _aliases(p["uniqueName"])}, cn if cn.startswith(iname) else f"{iname} {cn}".strip()))
    relevant = []
    obtainable = [False] * len(missing)
    relic_count = 0
    for pi in pools.candidates(matchers):
        count, rewards = pools.pools[pi]
        chances = [0.0] * len(missing)
        for rl, nn, ch in rewards:
            idx = -1
            if rl:
                idx = next((i for i, m in enumerate(matchers) if rl in m[0]), -1)
            if idx < 0:
                idx = next((i for i, m in enumerate(matchers) if m[1] == nn), -1)
            if idx >= 0:
                chances[idx] += ch
        total = sum(chances)
        if total <= 0:
            continue
        if total > 1:
            chances = [c / total for c in chances]
        for i, c in enumerate(chances):
            if c > 0:
                obtainable[i] = True
        relevant.append((count, chances))
        relic_count += count
    if not all(obtainable):
        return None
    mult, states = [], 1
    for p in missing:
        mult.append(states)
        states *= p["missing"] + 1
    if states > 4096:
        return None
    cur = [0.0] * states
    cur[0] = 1.0
    for count, chances in relevant:
        awarded = sum(chances)
        for _ in range(min(count, 200)):
            nxt = [0.0] * states
            for s, pr in enumerate(cur):
                if pr == 0:
                    continue
                nxt[s] += pr * max(0.0, 1 - awarded)
                for i, c in enumerate(chances):
                    if c == 0:
                        continue
                    progress = (s // mult[i]) % (missing[i]["missing"] + 1)
                    nxt[s + mult[i] if progress < missing[i]["missing"] else s] += pr * c
            cur = nxt
    prob = max(0.0, min(1.0, cur[-1]))
    return (prob, relic_count) if prob > 0 else None


def estimate_purchase_cost(root_price: float | None, parts: list[dict],
                           part_price: Callable[[dict], float | None]) -> float | None:
    """masteryRoadmap.ts estimateMasteryPurchaseCost."""
    if not parts:
        return root_price
    missing = [p for p in parts if p["missing"] > 0]
    if not missing:
        return None
    total = 0.0
    for p in missing:
        pr = part_price(p)
        if pr is None or not math.isfinite(pr) or pr <= 0:
            return None
        total += pr * p["missing"]
    if root_price is None or root_price <= 0:
        return total
    return min(root_price, total)


def relic_plans(items: list[dict], pools: "RelicPools | list | None") -> dict[str, tuple[float, int]]:
    """uniqueName -> relic_completion for the unmastered items the roadmap scores by relics
    (price independent, so MasteryService caches it per inventory + owned relics)."""
    if not isinstance(pools, RelicPools):
        pools = RelicPools(pools or [])
    out: dict[str, tuple[float, int]] = {}
    if not len(pools):
        return out
    for it in items:
        if it["status"] == "mastered" or it["masteryXpRemaining"] <= 0 or easy_access(it):
            continue
        plan = relic_completion(it, pools)
        if plan:
            out[it["uniqueName"]] = plan
    return out


def build_roadmap(items: list[dict], pools: "RelicPools | list | None" = None,
                  plans: dict[str, tuple[float, int]] | None = None) -> dict:
    if plans is None:
        plans = relic_plans(items, pools)
    easy, relics, plat = [], [], []
    for it in items:
        if it["status"] == "mastered" or it["masteryXpRemaining"] <= 0:
            continue
        access = easy_access(it)
        if access:
            easy.append({**it, "access": access, "accessLabel": ACCESS_LABEL[access]})
            continue
        plan = plans.get(it["uniqueName"])
        if plan:
            relics.append({**it, "access": "relics", "relicProbability": plan[0], "relevantRelicCount": plan[1]})
        cost = it.get("estimatedCost")
        if isinstance(cost, (int, float)) and math.isfinite(cost) and cost > 0:
            plat.append({**it, "access": "platinum", "xpPerPlatinum": it["masteryXpRemaining"] / cost})
    easy.sort(key=lambda r: (ACCESS_PRIORITY[r["access"]], -r["masteryXpRemaining"], r["name"].lower()))
    relics.sort(key=lambda r: (-r["relicProbability"], -r["masteryXpRemaining"], r["name"].lower()))
    plat.sort(key=lambda r: (-r["xpPerPlatinum"], r["estimatedCost"], r["name"].lower()))
    return {"easy": easy, "relics": relics, "platinum": plat}


def projection(rank: int | None, total_xp: float | None, ready_xp: float) -> dict | None:
    """masteryProjectionSubtext as data: banked rank, foundry-ready XP and the rank it reaches."""
    if rank is None or total_xp is None:
        return None
    banked = max(rank, xp_to_rank(total_xp))
    projected = max(banked, xp_to_rank(total_xp + max(0.0, ready_xp)))
    parts = []
    if banked > rank:
        parts.append(f"Rank {mr_label(banked)} banked (test pending)")
    if ready_xp > 0:
        parts.append(f"Foundry raises you to MR {mr_label(projected)} ({int(ready_xp):,} XP ready)"
                     if projected > banked else f"{int(ready_xp):,} XP ready in the foundry")
    return {"bankedRank": banked, "projectedRank": projected, "readyXp": ready_xp,
            "text": " · ".join(parts) or None}


def summary(progress: dict, ready_xp: float = 0.0) -> dict:
    st = progress["stats"]
    pm = st.get("profileMastery")
    out = {"mastered": st["mastered"], "inProgress": st["inProgress"], "missing": st["missing"], "total": st["total"],
           "percent": round(st["mastered"] / st["total"] * 100, 1) if st["total"] else 0.0,
           "byCategory": [{"category": c, **st["byCategory"][c]} for c in ordered_categories(st["byCategory"])],
           "profile": pm, "completion": st["completion"], "mrLine": None, "projection": None}
    if pm and pm.get("rank") is not None:
        rank = pm["rank"]
        if pm.get("testReady"):
            line = "test ready"
        elif pm.get("xpForNext") is not None and pm.get("xpIntoRank") is not None:
            line = f"{int(pm['xpForNext'] - pm['xpIntoRank']):,} XP to MR {mr_label(rank + 1)}"
        else:
            line = None
        out["mrLine"] = line
        out["projection"] = projection(rank, pm.get("totalXp"), ready_xp)
    return out


def ordered_categories(cats: Iterable[str]) -> list[str]:
    cats = set(cats)
    return [c for c in CAT_ORDER if c in cats and c != "Misc"] + sorted(c for c in cats if c not in CAT_ORDER) \
        + (["Misc"] if "Misc" in cats else [])


# ---- service (inventory + item db + prices) -------------------------------------------------------

class MasteryService:
    def __init__(self, settings=None, inventory: Callable[[], Any] | None = None, md: Callable[[], Any] = data,
                 item_db: Callable[[], Any] | None = None, price_of: Callable[[str], float | None] | None = None,
                 owned_relics: Callable[[], dict] | None = None, relic_db: Callable[[], Any] | None = None,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.settings = settings
        self._inventory = inventory or _default_inventory
        self._md = md
        self._item_db = item_db or _default_item_db
        self._price_of = price_of or _default_price
        self._owned_relics = owned_relics or _default_owned_relics
        self._relic_db = relic_db or _default_relic_db
        self._now = now_ms
        self._lock = threading.Lock()            # guards the cache tuples below
        self._compute = threading.Lock()         # single-flight for the progress miss path
        self._road_compute = threading.Lock()    # single-flight for relic plans
        # (inventory, hide_founder, progress, price-free hydrated items)
        self._cache: tuple[Any, bool, dict, list] | None = None
        # (base items, monotonic time, priced items): prices are read at view time, reused briefly
        self._priced: tuple[list, float, list] | None = None
        # (base items, owned relics, relic db, plans)
        self._plans: tuple[list, Any, Any, dict] | None = None

    def hide_founder(self) -> bool:
        return bool(self.settings.get("mastery.hide_founder", False)) if self.settings else False

    def pins(self) -> list[str]:
        raw = self.settings.get("mastery.pinned", []) if self.settings else []
        return [p for p in raw if isinstance(p, str)][:PIN_LIMIT] if isinstance(raw, list) else []

    def invalidate(self) -> None:
        with self._lock:
            self._cache = self._priced = self._plans = None

    def _inv_data(self) -> tuple[Any, dict | None]:
        inv = self._inventory()
        if inv is None:
            return None, None
        return inv, (inv.data if hasattr(inv, "data") else inv)

    def _base(self) -> tuple[dict | None, list[dict]]:
        """(progress, hydrated items without prices) cached per inventory snapshot; one thread
        computes a miss while concurrent callers wait for its result."""
        md = self._md()
        inv, raw = self._inv_data()
        if md is None or raw is None:
            return None, []
        hide = self.hide_founder()

        def hit():
            c = self._cache
            return (c[2], c[3]) if c and c[0] is inv and c[1] == hide else None

        with self._lock:
            got = hit()
        if got:
            return got
        with self._compute:
            with self._lock:
                got = hit()
            if got:
                return got
            prog = compute_progress(raw, md, self._name_of(md), hide)
            items = self._hydrate(prog["items"], inv, raw, md)
            with self._lock:
                self._cache, self._priced, self._plans = (inv, hide, prog, items), None, None
        return prog, items

    def progress(self) -> tuple[dict | None, list[dict]]:
        """(progress, hydrated items with platinum / estimatedCost from the current price cache);
        (None, []) without data. Prices are applied per view (reused for PRICE_VIEW_TTL) so
        prices that land after the inventory snapshot show up without an inventory update."""
        prog, base = self._base()
        if prog is None:
            return None, []
        now = time.monotonic()
        with self._lock:
            p = self._priced
            if p and p[0] is base and now - p[1] < PRICE_VIEW_TTL:
                return prog, p[2]
        items = self._apply_prices(base)
        with self._lock:
            if self._cache and self._cache[3] is base:
                self._priced = (base, now, items)
        return prog, items

    def _apply_prices(self, base: list[dict]) -> list[dict]:
        db = self._item_db()
        out = []
        for it in base:
            row = dict(it)
            root = self._price_of(it["slug"]) if it.get("slug") else None
            row["platinum"] = root
            if it["status"] != "mastered" and not it["currentlyOwned"]:
                row["estimatedCost"] = estimate_purchase_cost(root, it["parts"], lambda p: self._part_price(db, p))
            else:
                row["estimatedCost"] = None
            out.append(row)
        return out

    def _part_slug(self, db, part: dict) -> str | None:
        if db is None:
            return None
        for a in _aliases(part["uniqueName"]):
            hit = db.by_unique(a)
            if hit is not None and hit.wfm_slug:
                return hit.wfm_slug
        return None

    def unpriced_slugs(self, items: list[dict], limit: int = PREFETCH_LIMIT) -> list[str]:
        """Slugs the roadmap's platinum list needs and the price cache lacks: the set and the
        missing parts of every unmastered item not owned (most mastery XP first)."""
        db = self._item_db()
        want = sorted((i for i in items if i["status"] != "mastered" and not i["currentlyOwned"]
                       and i["masteryXpRemaining"] > 0), key=lambda r: -r["masteryXpRemaining"])
        out: dict[str, None] = {}
        for it in want:
            slugs = [it.get("slug")] + [self._part_slug(db, p) for p in it["parts"] if p["missing"] > 0]
            for sl in slugs:
                if sl and sl not in out and self._price_of(sl) is None:
                    out[sl] = None
            if len(out) >= limit:
                break
        return list(out)[:limit]

    def _name_of(self, md: MasteryData) -> Callable[[str], str | None]:
        db = self._item_db()

        def name_of(u: str) -> str | None:
            hit = md.by_unique.get(u)
            if hit:
                return hit["n"]
            it = db.by_unique(u) if db is not None else None
            return it.name if it else None
        return name_of

    def _hydrate(self, items: list[dict], inv: Any, raw: dict, md: MasteryData) -> list[dict]:
        db = self._item_db()
        foundry = foundry_index(raw, md, self._now())
        if hasattr(inv, "owned_counts"):
            counts = inv.owned_counts()
        else:
            from ..inventory.model import aggregate_component_ownership, without_foundry_pending

            counts = aggregate_component_ownership(without_foundry_pending(raw))

        def owned(u: str) -> float:
            return float(counts.get(u, 0))

        by_result: dict[str, set] = {}
        for bp, result in md.blueprints.items():
            by_result.setdefault(result, set()).add(bp)
        out = []
        for it in items:
            dbi = db.by_unique(it["uniqueName"]) if db is not None else None
            comps = dbi.components if dbi is not None else []
            main_bps = by_result.get(it["uniqueName"], set())
            parts = part_states(comps, owned, foundry, md, lambda u, m=main_bps: u in m or u.endswith(
                "/" + it["uniqueName"].rsplit("/", 1)[-1] + "Blueprint"))
            row = dict(it)
            row["parts"] = parts
            row["partsOwned"] = sum(1 for p in parts if p["state"] in ("owned", "blueprint"))
            row["partsTotal"] = len(parts)
            row["foundryState"] = foundry.get(it["uniqueName"])
            row["progressPct"] = int(it["rank"] / it["maxRank"] * 100) if it["maxRank"] else 0
            row["slug"] = dbi.wfm_slug if dbi is not None else None
            row["platinum"] = row["estimatedCost"] = None   # filled per view by _apply_prices
            out.append(row)
        return out

    def _part_price(self, db, part: dict) -> float | None:
        slug = self._part_slug(db, part)
        return self._price_of(slug) if slug else None

    def ready_xp(self, items: list[dict]) -> float:
        return float(sum(i["masteryXpRemaining"] for i in items
                         if i["status"] != "mastered" and i.get("foundryState") == "claimable"))

    def relic_pools(self, owned: dict | None = None, rdb: Any = None) -> list:
        rdb = rdb if rdb is not None else self._relic_db()
        if rdb is None:
            return []
        pools = []
        for (name, refinement), count in (owned if owned is not None else (self._owned_relics() or {})).items():
            relic = rdb.get(name)
            if relic is None or count <= 0:
                continue
            rewards = [(r.item_unique, r.item_name, r.chance(refinement)) for r in relic.rewards]
            pools.append((int(count), [r for r in rewards if r[2] > 0]))
        return pools

    def relic_plans(self, base: list[dict]) -> dict[str, tuple[float, int]]:
        """relic_plans cached per (price-free items, owned relics, relic db) identity."""
        owned = self._owned_relics() or {}
        rdb = self._relic_db()

        def hit():
            c = self._plans
            return c[3] if c and c[0] is base and c[1] is owned and c[2] is rdb else None

        with self._lock:
            got = hit()
        if got is not None:
            return got
        with self._road_compute:
            with self._lock:
                got = hit()
            if got is not None:
                return got
            plans = relic_plans(base, RelicPools(self.relic_pools(owned, rdb)))
            with self._lock:
                if self._cache and self._cache[3] is base:
                    self._plans = (base, owned, rdb, plans)
        return plans

    # ---- view models -------------------------------------------------------------------------

    def status(self) -> dict:
        md = self._md()
        _, raw = self._inv_data()
        return {"data": md is not None, "dataVersion": md.version if md else None, "inventory": raw is not None,
                "hideFounder": self.hide_founder()}

    def summary(self) -> dict:
        prog, items = self.progress()
        if prog is None:
            return {**self.status(), "summary": None}
        return {**self.status(), "summary": summary(prog, self.ready_xp(items)),
                "categories": ordered_categories(prog["stats"]["byCategory"])}

    def items(self, category: str = "all", status: str = "all", sort: str = "name", offset: int = 0,
              limit: int = 50, query: str = "") -> dict:
        prog, items = self.progress()
        if prog is None:
            return {**self.status(), "rows": [], "matched": 0}
        rows = [i for i in items if (category in ("all", "", None) or i["category"] == category)
                and (status in ("all", "", None) or i["status"] == status)
                and (not query or query.lower() in i["name"].lower())]
        key = {"name": lambda r: r["name"].lower(),
               "owned": lambda r: (not r["currentlyOwned"], r["name"].lower()),
               "parts_owned": lambda r: (-(r["partsOwned"] / r["partsTotal"] if r["partsTotal"] else 0), r["name"].lower()),
               "mastery_xp": lambda r: (-r["masteryXpRemaining"], r["name"].lower()),
               "platinum": lambda r: (r["platinum"] is None, -(r["platinum"] or 0), r["name"].lower())}
        rows.sort(key=key.get(sort, key["name"]))
        offset = max(0, int(offset or 0))
        limit = max(1, min(200, int(limit or 50)))
        pins = set(self.pins())
        page = [_row_json(r, r["uniqueName"] in pins) for r in rows[offset: offset + limit]]
        return {**self.status(), "rows": page, "matched": len(rows), "offset": offset, "limit": limit}

    def item(self, unique_name: str) -> dict | None:
        _, items = self.progress()
        hit = next((i for i in items if i["uniqueName"] == unique_name), None)
        if hit is None:
            return None
        return {**_row_json(hit, unique_name in self.pins()), "parts": hit["parts"]}

    def roadmap(self, limit: int = 50) -> dict:
        prog, base = self._base()
        if prog is None:
            return {**self.status(), "easy": [], "relics": [], "platinum": [], "easyXp": 0}
        plans = self.relic_plans(base)
        _, items = self.progress()
        rm = build_roadmap(items, plans=plans)
        limit = max(1, min(200, int(limit or 50)))
        out = {k: [_roadmap_json(r) for r in v[:limit]] for k, v in rm.items()}
        out["counts"] = {k: len(v) for k, v in rm.items()}
        out["easyXp"] = sum(r["masteryXpRemaining"] for r in rm["easy"])
        pm = prog["stats"].get("profileMastery") or {}
        if pm.get("rank") is not None and pm.get("totalXp") is not None:
            proj = max(xp_to_rank(pm["totalXp"] + out["easyXp"]), pm["rank"])
            out["easyRank"] = proj if proj > max(pm["rank"], xp_to_rank(pm["totalXp"])) else None
        return {**self.status(), **out, "unpricedSlugs": self.unpriced_slugs(items)}

    def planned(self) -> dict:
        _, items = self.progress()
        by_u = {i["uniqueName"]: i for i in items}
        rows = []
        for u in self.pins():
            it = by_u.get(u)
            if it is None:
                continue
            parts = it["parts"]
            done = sum(1 for p in parts if p["state"] in ("owned", "blueprint"))
            rows.append({**_row_json(it, True), "parts": parts,
                         "completeness": round(done / len(parts), 3) if parts else (1.0 if it["currentlyOwned"] else 0.0),
                         "craftableNow": build_readiness(parts) == "buildable" or it.get("foundryState") == "claimable"})
        rows.sort(key=lambda r: (not r["craftableNow"], -r["masteryXpRemaining"], r["name"].lower()))
        return {**self.status(), "rows": rows, "limit": PIN_LIMIT}

    def qam(self, limit: int = 5) -> dict:
        prog, items = self.progress()
        if prog is None:
            return {**self.status(), "summary": None, "easy": []}
        rm = build_roadmap(items, plans={})          # the QAM shows only the easy list
        s = summary(prog, self.ready_xp(items))
        s.pop("byCategory", None)
        s.pop("completion", None)
        return {**self.status(), "summary": s, "easy": [_roadmap_json(r) for r in rm["easy"][:limit]],
                "easyCount": len(rm["easy"])}


def _row_json(r: dict, pinned: bool = False) -> dict:
    return {k: r.get(k) for k in ("name", "uniqueName", "category", "status", "rank", "maxRank", "currentlyOwned",
                                  "masteryXp", "masteryXpRemaining", "isPrime", "vaulted", "marketBuyable",
                                  "needsGilding", "partsOwned", "partsTotal", "foundryState", "progressPct",
                                  "platinum", "estimatedCost", "masteryReq", "slug")} | {"pinned": pinned}


def _roadmap_json(r: dict) -> dict:
    out = _row_json(r)
    for k in ("access", "accessLabel", "relicProbability", "relevantRelicCount", "xpPerPlatinum"):
        if k in r:
            out[k] = r[k]
    out["missingParts"] = [p["name"] for p in r.get("parts") or [] if p["missing"] > 0]
    return out


def _default_inventory():
    from .. import inventory

    return inventory.inventory()


def _default_item_db():
    try:
        from ..data import items

        return items.items()
    except Exception:  # noqa: BLE001
        return None


def _default_price(slug: str) -> float | None:
    try:
        from ..data import market

        hit, p = market.get().cached(slug, None, max_age=float("inf"))
    except Exception:  # noqa: BLE001
        return None
    if not hit or p is None:
        return None
    return p.median if p.median else (float(p.low_sell) if p.low_sell else None)


def _default_owned_relics() -> dict:
    from .. import inventory

    return inventory.owned_relics()


def _default_relic_db():
    try:
        from ..data import relics

        return relics.relics()
    except Exception:  # noqa: BLE001
        return None


# ---- module wiring ---------------------------------------------------------------------------------

_svc: MasteryService | None = None


def service() -> MasteryService:
    global _svc
    if _svc is None:
        _svc = MasteryService()
    return _svc


def setup(app) -> None:
    global _svc
    _svc = MasteryService(app.settings)
    set_data_dir(app.data_dir)
    from .. import inventory

    def on_inv(_inv) -> None:
        _svc.invalidate()
        app.emit("mastery.updated", None)

    inventory.on_update(on_inv)
    last_hide = [_svc.hide_founder()]

    def on_settings(d: dict) -> None:
        # Pins are read per view and hide_founder is part of the cache key; only a
        # hide_founder change alters what the page shows.
        hide = bool(((d or {}).get("mastery") or {}).get("hide_founder", False))
        if hide != last_hide[0]:
            last_hide[0] = hide
            app.emit("mastery.updated", None)

    app.settings.on_change(on_settings)


def _app():
    from ..app import get_app

    return get_app()


@rpc.method("mastery.summary")
async def _rpc_summary() -> dict:
    return await _app().run_blocking(service().summary)


@rpc.method("mastery.items")
async def _rpc_items(category: str = "all", status: str = "all", sort: str = "name", offset: int = 0,
                     limit: int = 50, query: str = "") -> dict:
    return await _app().run_blocking(service().items, category, status, sort, offset, limit, query)


@rpc.method("mastery.item")
async def _rpc_item(unique_name: str) -> dict | None:
    return await _app().run_blocking(service().item, unique_name)


@rpc.method("mastery.roadmap")
async def _rpc_roadmap(limit: int = 50, prefetch: bool = True) -> dict:
    out = await _app().run_blocking(service().roadmap, limit)
    slugs = out.pop("unpricedSlugs", [])
    if prefetch and slugs:
        try:
            from ..data import market

            # notify: each landed price emits market.price; the page reloads (debounced).
            market.get().prefetch(slugs, notify=True)
        except Exception:  # noqa: BLE001
            _log.exception("mastery price prefetch failed")
    return out


@rpc.method("mastery.planned")
async def _rpc_planned() -> dict:
    return await _app().run_blocking(service().planned)


@rpc.method("mastery.pin")
async def _rpc_pin(unique_name: str, pinned: bool = True) -> dict:
    svc = service()
    pins = [p for p in svc.pins() if p != unique_name]
    if pinned:
        pins = [unique_name] + pins
    _app().settings.update({"mastery": {"pinned": pins[:PIN_LIMIT]}})
    return {"pinned": pins[:PIN_LIMIT]}


@rpc.method("mastery.qam")
async def _rpc_qam(limit: int = 5) -> dict:
    return await _app().run_blocking(service().qam, limit)


@rpc.method("mastery.configure")
async def _rpc_configure(hide_founder: bool | None = None) -> dict:
    if hide_founder is not None:
        _app().settings.update({"mastery": {"hide_founder": bool(hide_founder)}})
    return {"hide_founder": service().hide_founder()}


if __name__ == "__main__":
    if len(sys.argv) in (5, 6) and sys.argv[1] == "build":
        print(build(*sys.argv[2:]))
    else:
        print("usage: python -m wfh.menus.mastery build <pep package dir> <wfcd data/json dir> <out dir> [label]")
        sys.exit(2)
