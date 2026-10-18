"""Inventory and Foundry view-models: WFHelper's parseInventory / parseFoundry ports.

Ports (WFHelper, MIT; paths into /home/claude/wfhelper-src, see docs/port/menus.md §0.1, §3, §4):
  src/lib/inventory.ts                    parse_inventory
  src/lib/inventory/itemClassification.ts resolve_item, derive_group, should_hide, is_resource_item, ...
  src/lib/inventory/entryNormalization.ts pick_boolean, parse_amount, equip contexts, merge helpers
  src/lib/inventory/rankExtraction.ts     normalize_rank
  src/lib/inventory/fullSets.ts           build_full_set_items (fullSetOverrides.ts not ported)
  src/lib/inventory/foundryResources.ts   parse_foundry, parse_resources, classify_for_foundry,
                                          chain_buildable_blueprints, is_foundry_recipe_ready
  src/lib/inventory/valueTotals.ts        compute_value_totals, select_value_strip_rows
  src/lib/inventoryMarket.ts              build_base_items / build_view_items (slug resolution, prices)
  src/lib/filters.ts                      matches_shared_filters, compare_sort, default_sort_direction
  src/lib/pinnedSummary.ts                compute_pinned_totals

The item database is WFHelper's `itemDb` shape rebuilt from our bundled @wfcd/items data
(wfh.data.items) plus `data/foundry/foundry.json.gz`, built by scripts/fetch-data.sh from
warframe-public-export-plus (`python -m wfh.inventory_view build <pkg dir> <out dir>`):
foundry recipes (blueprint -> product, ingredients, credits, build time, reusable),
productCategory, mod/arcane max rank and English names for recipe items @wfcd lacks.
Without that file recipes fall back to @wfcd's component lists (no credits / build time).

Everything here is pure (no network): prices come from the market cache only.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import log
from .data import names as names_mod

_log = log.get("inventory_view")

EXTRA_SUBDIR = "foundry"
EXTRA_FILE = "foundry.json.gz"
EXTRA_SCHEMA = 1

# ---------------------------------------------------------------------------------------------
# bundle build (scripts/fetch-data.sh)
# ---------------------------------------------------------------------------------------------

_NAME_EXPORTS = ("ExportResources", "ExportWarframes", "ExportWeapons", "ExportSentinels", "ExportUpgrades",
                 "ExportArcanes", "ExportCustoms", "ExportGear", "ExportKeys", "ExportDrones")
_CATEGORY_EXPORTS = ("ExportWarframes", "ExportWeapons", "ExportSentinels")
# itemDatabase.ts exportMappings: the export a uniqueName comes from is its itemDb category.
EXPORT_CATEGORY = {"ExportWarframes": "Warframe", "ExportWeapons": "Weapon", "ExportSentinels": "Companion",
                   "ExportResources": "Resource", "ExportKeys": "Key", "ExportGear": "Gear", "ExportArcanes": "Arcane",
                   "ExportUpgrades": "Mod", "ExportDrones": "Gear"}


def build_extra(pkg_dir: str, out_dir: str) -> dict:
    """Trim warframe-public-export-plus into foundry.json.gz; returns the manifest-ish header."""
    def load(name: str) -> dict:
        path = os.path.join(pkg_dir, f"{name}.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    recipes_raw = load("ExportRecipes")
    if not recipes_raw:
        raise ValueError("ExportRecipes.json missing")
    dict_en = load("dict.en")
    version = ""
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as f:
            version = str(json.load(f).get("version") or "")
    except (OSError, ValueError):
        pass

    recipes: dict[str, list] = {}
    wanted: set[str] = set()
    for bp, r in recipes_raw.items():
        if not isinstance(r, dict) or not r.get("resultType"):
            continue
        ings = [[i.get("ItemType"), int(i.get("ItemCount") or 1)] for i in r.get("ingredients") or []
                if isinstance(i, dict) and i.get("ItemType")]
        recipes[bp] = [r["resultType"], int(r.get("buildPrice") or 0), int(r.get("buildTime") or 0),
                       int(r.get("num") or 1), 0 if r.get("consumeOnUse", True) else 1, ings]
        wanted.add(bp)
        wanted.add(r["resultType"])
        wanted.update(i[0] for i in ings)

    names: dict[str, str] = {}
    product_category: dict[str, str] = {}
    max_rank: dict[str, int] = {}
    category: dict[str, list[str]] = {}
    for exp in _NAME_EXPORTS:
        data = load(exp)
        for u, row in data.items():
            if not isinstance(row, dict):
                continue
            if exp in EXPORT_CATEGORY:
                category.setdefault(EXPORT_CATEGORY[exp], []).append(u)
            if exp in _CATEGORY_EXPORTS and isinstance(row.get("productCategory"), str):
                product_category[u] = row["productCategory"]
            if exp in ("ExportUpgrades", "ExportArcanes") and isinstance(row.get("fusionLimit"), int):
                max_rank[u] = row["fusionLimit"]
            if u in wanted and isinstance(row.get("name"), str):
                text = dict_en.get(row["name"]) if dict_en else None
                if isinstance(text, str) and text.strip():
                    names[u] = sanitize_display_name(text)
    body = {"schema": EXTRA_SCHEMA, "source": "warframe-public-export-plus", "version": version,
            "recipes": recipes, "productCategory": product_category, "maxRank": max_rank, "names": names,
            "category": {k: sorted(set(v)) for k, v in category.items()}}
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, EXTRA_FILE)
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    with open(path + ".tmp", "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    os.replace(path + ".tmp", path)
    return {"version": version, "recipes": len(recipes), "names": len(names),
            "productCategory": len(product_category), "maxRank": len(max_rank),
            "bytes": os.path.getsize(path)}


# ---------------------------------------------------------------------------------------------
# extra data + itemDb adapter
# ---------------------------------------------------------------------------------------------

@dataclass
class Recipe:
    blueprint: str
    result: str
    build_price: int = 0
    build_time: int = 0
    num: int = 1
    reusable: bool = False
    ingredients: list[tuple[str, int]] = field(default_factory=list)


class Extra:
    def __init__(self, data: dict | None = None):
        data = data or {}
        self.version = str(data.get("version") or "")
        self.recipes: dict[str, Recipe] = {}
        for bp, r in (data.get("recipes") or {}).items():
            try:
                self.recipes[bp] = Recipe(bp, r[0], int(r[1]), int(r[2]), int(r[3] or 1), bool(r[4]),
                                          [(str(u), int(c)) for u, c in r[5]])
            except (IndexError, TypeError, ValueError):
                continue
        self.product_category: dict[str, str] = dict(data.get("productCategory") or {})
        self.max_rank: dict[str, int] = dict(data.get("maxRank") or {})
        self.names: dict[str, str] = dict(data.get("names") or {})
        self.category: dict[str, str] = {}
        for cat, uniques in (data.get("category") or {}).items():
            for u in uniques or []:
                self.category.setdefault(u, cat)

    def __bool__(self) -> bool:
        return bool(self.recipes)


_extra_lock = threading.Lock()
_extra: Extra | None = None
_extra_path_override: str | None = None


def _default_extra_path() -> str:
    try:
        from .app import get_app

        base = get_app().data_dir
    except Exception:  # noqa: BLE001 - CLI / tests without an app
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
    return os.path.join(base, EXTRA_SUBDIR, EXTRA_FILE)


def set_extra_path(path: str | None) -> None:
    """Tests: point at another foundry.json.gz (None = default); drops caches."""
    global _extra_path_override, _extra
    with _extra_lock:
        _extra_path_override = path
        _extra = None
    reset_caches()


def extra() -> Extra:
    global _extra
    with _extra_lock:
        if _extra is None:
            path = _extra_path_override or _default_extra_path()
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    _extra = Extra(json.load(f))
            except (OSError, ValueError) as e:
                _log.warning("foundry data unavailable (%s): recipes fall back to @wfcd components", e)
                _extra = Extra()
        return _extra


# @wfcd category -> DE productCategory (collection name).
_WFCD_PRODUCT = {"Warframes": "Suits", "Primary": "LongGuns", "Secondary": "Pistols", "Melee": "Melee",
                 "Sentinels": "Sentinels", "SentinelWeapons": "SentinelWeapons", "Archwing": "SpaceSuits",
                 "Arch-Gun": "SpaceGuns", "Arch-Melee": "SpaceMelee", "Pets": "KubrowPets"}
_MASTERABLE = set(_WFCD_PRODUCT)
_PRIME_WORD = re.compile(r"\bPrime\b")


@dataclass
class Entry:
    """WFHelper ItemDbEntry, the fields the ports read."""
    unique_name: str
    name: str = ""
    category: str = ""          # WFHelper itemDb category (DE export: Resource, Mod, Weapon...), else @wfcd's
    type: str = ""
    product_category: str = ""
    is_prime: bool = False
    is_build_component: bool | None = None
    component_of: str | None = None
    mastery_req: int = 0
    vaulted: bool = False
    tradable: bool | None = None
    masterable: bool = False
    ducats: int | None = None
    wfm_slug: str | None = None
    image_name: str | None = None
    components: list[dict] = field(default_factory=list)
    recipe: Recipe | None = None
    reusable_blueprint: bool = False
    known: bool = True
    wfcd_category: str = ""


class Db:
    """Lazy uniqueName -> Entry view over ItemDb + Extra (WFHelper's itemDb record)."""

    def __init__(self, items_db, ext: Extra):
        self.items = items_db
        self.extra = ext
        self._cache: dict[str, Entry | None] = {}
        self._lock = threading.Lock()
        # recipe of each product, and blueprint -> product
        self.recipe_by_product: dict[str, Recipe] = {}
        self.blueprint_to_product: dict[str, str] = {}
        self.ingredient_set: set[str] = set()
        if ext:
            for bp, r in ext.recipes.items():
                self.blueprint_to_product.setdefault(bp, r.result)
                self.recipe_by_product.setdefault(r.result, r)
                for u, _ in r.ingredients:
                    self.ingredient_set.add(u)
        else:
            for it in items_db.all() if items_db is not None else []:
                comps = it.components or []
                bp = next((c for c in comps if c.unique_name.endswith("Blueprint")
                           and "/Types/Recipes/" in c.unique_name), None)
                if bp is None:
                    continue
                ings = [(c.unique_name, max(1, int(c.item_count or 1))) for c in comps if c is not bp]
                r = Recipe(bp.unique_name, it.unique_name, 0, 0, 1, False, ings)
                self.blueprint_to_product.setdefault(bp.unique_name, it.unique_name)
                self.recipe_by_product.setdefault(it.unique_name, r)
                for u, _ in ings:
                    self.ingredient_set.add(u)

    def get(self, u: str) -> Entry | None:
        if not u:
            return None
        with self._lock:
            if u in self._cache:
                return self._cache[u]
        e = self._build(u)
        with self._lock:
            self._cache[u] = e
        return e

    def _build(self, u: str) -> Entry | None:
        it = self.items.by_unique(u) if self.items is not None else None
        ext_name = self.extra.names.get(u)
        recipe = self.recipe_by_product.get(u)
        own_recipe = self.extra.recipes.get(u)
        de_cat = self.extra.category.get(u, "")
        if it is None and ext_name is None and recipe is None and own_recipe is None and not de_cat:
            return None
        if it is not None:
            cat = it.category or ""
            e = Entry(u, it.name or ext_name or "", de_cat or cat, it.type or "",
                      self.extra.product_category.get(u) or _WFCD_PRODUCT.get(cat, ""),
                      bool(_PRIME_WORD.search(it.name or "")),
                      True if cat == "Components" else None, it.parent, int(it.mastery_req or 0),
                      bool(it.vaulted), bool(it.tradable), cat in _MASTERABLE, it.ducats, it.wfm_slug,
                      it.image_name,
                      [{"uniqueName": c.unique_name, "name": c.name, "itemCount": c.item_count,
                        "tradable": c.tradable, "ducats": c.ducats} for c in it.components or []])
            e.wfcd_category = cat
        else:
            nm = ext_name or ""
            e = Entry(u, nm, de_cat, "", self.extra.product_category.get(u, ""), bool(_PRIME_WORD.search(nm)),
                      known=bool(nm))
        e.recipe = recipe
        if own_recipe is not None:
            e.reusable_blueprint = own_recipe.reusable
        return e

    def name_of(self, u: str) -> str:
        e = self.get(u)
        if e is not None and e.name:
            return sanitize_display_name(e.name)
        return fallback_name_from_unique_name(u)

    def all_entries(self) -> Iterable[Entry]:
        if self.items is None:
            return []
        out = []
        for it in self.items.all():
            e = self.get(it.unique_name)
            if e is not None:
                out.append(e)
        return out


# ---------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------

_LEADING_BRACKET = re.compile(r"^<[^>]{1,24}>\s*")


def sanitize_display_name(name: Any) -> str:
    raw = str(name or "").strip()
    return _LEADING_BRACKET.sub("", raw).strip() or raw


def fallback_name_from_unique_name(u: str | None) -> str:
    if not u:
        return "Unknown"
    raw = str(u)
    last = raw.split("/")[-1] or "Unknown"
    if re.search(r"/Lotus/Language/", raw, re.I):
        last = re.sub(r"Name$", "", last)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return sanitize_display_name(name)


@dataclass
class Resolved:
    name: str
    entry: Entry | None
    name_is_fallback: bool = False

    @property
    def is_prime(self) -> bool:
        return bool(self.entry.is_prime) if self.entry else False


def resolve_item(u: str, db: Db) -> Resolved:
    e = db.get(u)
    if e is not None and e.name:
        return Resolved(sanitize_display_name(e.name), e)
    if not u:
        return Resolved("Unknown", None)
    return Resolved(fallback_name_from_unique_name(u), e, True)


def _cat(e: Entry | None) -> str:
    return (e.category if e else "").lower()


def _typ(e: Entry | None) -> str:
    return (e.type if e else "").lower()


# ---------------------------------------------------------------------------------------------
# itemClassification.ts
# ---------------------------------------------------------------------------------------------

EQUIPMENT_COLLECTIONS = ("Suits", "LongGuns", "Pistols", "Melee", "Sentinels", "SentinelWeapons", "SpaceSuits",
                         "SpaceGuns", "SpaceMelee", "OperatorAmps", "MechSuits")
CATEGORY_BY_COLLECTION = {
    "Suits": ("warframes", "Warframe"), "LongGuns": ("primary", "Primary"), "Pistols": ("secondary", "Secondary"),
    "Melee": ("melee", "Melee"), "Sentinels": ("companions", "Companion"),
    "SentinelWeapons": ("companions", "Companion"), "SpaceSuits": ("archwing", "Archwing"),
    "SpaceGuns": ("archwing", "Archwing"), "SpaceMelee": ("archwing", "Archwing"), "OperatorAmps": ("amps", "Amp"),
    "MechSuits": ("necramech", "Necramech"),
}
CATEGORIES = [(k, *CATEGORY_BY_COLLECTION[k]) for k in EQUIPMENT_COLLECTIONS]
LABEL_BY_CAT = {}
for _k, _c, _l in CATEGORIES:
    LABEL_BY_CAT.setdefault(_c, _l)
SUPPLEMENTAL_COLLECTIONS = [
    ("MiscItems", "misc", "Misc"), ("FusionTreasures", "misc", "Misc"), ("Recipes", "misc", "Recipe"),
    ("LevelKeys", "relics", "Relic"), ("RawUpgrades", "misc", "Misc"), ("Upgrades", "mods", "Mod"),
    ("Arcanes", "arcanes", "Arcane"),
]
MODULAR_COLLECTION_KEYS = ("Hoverboards", "MoaPets", "KubrowPets")
PET_COLLECTION_KEY = "KubrowPets"


def _infested_mech_part(u: str) -> bool:
    # config/shared/componentNames.ts isInfestedMechPart (Necramech parts live outside Recipes)
    return bool(re.search(r"/InfestedMicroplanet/Resources/Mechs/", u, re.I))


def is_arcane_upgrade(u: str, e: Entry | None, r: Resolved) -> bool:
    if re.search(r"/Arcanes?/", u, re.I) or re.search(r"/CosmeticEnhancers?/", u, re.I):
        return True
    if "arcane" in _cat(e) or "arcane" in _typ(e):
        return True
    return r.name.lower().startswith("arcane ")


def is_focus_upgrade(u: str, e: Entry | None, r: Resolved) -> bool:
    if re.search(r"/Upgrades/Focus/", u, re.I):
        return True
    return "focus way" in _typ(e) or "focus" in _cat(e) or "waybound" in r.name.lower()


def is_likely_mod_upgrade(u: str, e: Entry | None, r: Resolved) -> bool:
    if re.search(r"/FusionBundles/", u, re.I):
        return False
    if re.search(r"/Upgrades/Mods/", u, re.I) or re.search(r"/Mods/", u, re.I):
        return True
    cat, typ, name = _cat(e), _typ(e), r.name.lower()
    if "resource" in cat or "fusion" in cat or "resource" in typ or "fusion" in typ:
        return False
    if "mod" in cat:
        return True
    if " mod" in typ or typ.endswith("mod") or "augment" in typ:
        return True
    return bool(re.search(r"\bmod\b", name))


def is_relic_like(u: str, e: Entry | None, r: Resolved | None = None) -> bool:
    if re.search(r"/Relics?/", u, re.I) or re.search(r"VoidProjection", u, re.I):
        return True
    name = (r.name if r else (e.name if e else "")).lower()
    return "relic" in _cat(e) or "relic" in _typ(e) or bool(re.search(r"\brelic\b", name))


def is_scene_like(u: str, e: Entry | None, r: Resolved | None = None) -> bool:
    if re.search(r"photobooth", u, re.I):
        return True
    name = (r.name if r else (e.name if e else "")).lower()
    typ = _typ(e)
    return "captura" in typ or "scene" in typ or name.endswith(" scene")


def is_auxiliary(u: str, e: Entry | None, r: Resolved | None = None) -> bool:
    if is_scene_like(u, e, r):
        return True
    if re.search(r"/Types/Items/ShipFeatureItems/|/Types/Items/SongItems/|QuestKeyChain", u, re.I):
        return True
    if re.search(r"/Types/Keys/.*Quest", u, re.I):
        return True
    cat, typ = _cat(e), _typ(e)
    return "quest" in cat or "quest" in typ or "song" in typ or "fragment" in typ


def is_ayatan_like(u: str, e: Entry | None, r: Resolved | None = None) -> bool:
    if re.search(r"/FusionTreasures/", u, re.I):
        return True
    typ = _typ(e)
    name = (r.name if r else (e.name if e else "")).lower()
    if "ayatan" in typ or "star" in typ or "sculpture" in typ:
        return True
    return "ayatan" in name or "amber star" in name or "cyan star" in name


def _build_part_path(u: str) -> bool:
    return bool(re.search(r"/Types/Recipes/|/WeaponParts?/|/WarframeParts?/|/LandingCraftRecipes/", u, re.I)) \
        or _infested_mech_part(u)


_PART_WORDS = re.compile(r"\b(blueprint|barrel|receiver|stock|blade|handle|hilt|chassis|systems|neuroptics|fuselage|"
                         r"engines|avionics|carapace|cerebrum|pod|wings|harness|link|disc|gauntlet|grip|ornament|"
                         r"rivet)\b", re.I)


def is_resource_item(u: str, e: Entry | None, r: Resolved | None = None) -> bool:
    if is_auxiliary(u, e, r) or is_ayatan_like(u, e, r) or is_relic_like(u, e, r):
        return False
    name = (r.name if r else (e.name if e else "")).lower()
    if _build_part_path(u) or _PART_WORDS.search(name):
        return False
    if "resource" in _cat(e) or "resource" in _typ(e):
        return True
    return bool(re.match(r"^/Lotus/Types/Items/(?!MiscItems/)[^/]+$", u, re.I))


def is_build_part_item(u: str, e: Entry | None, r: Resolved) -> bool:
    if is_scene_like(u, e, r) or is_ayatan_like(u, e, r) or is_relic_like(u, e, r):
        return False
    if re.search(r"/Types/Keys/", u, re.I):
        return False
    typ, cat, name = _typ(e), _cat(e), r.name.lower()
    if (not _infested_mech_part(u) and "resource" in typ) or any(w in typ for w in
                                                                ("booster", "key", "fish", "captura", "ayatan")):
        return False
    if "fish" in cat or "captura" in cat:
        return False
    if e is not None and e.tradable is False:
        return False
    path_part = _build_part_path(u)
    weapon_part_recipe = bool(re.search(r"/Types/Recipes/Weapons/WeaponParts?/", u, re.I))
    name_part = bool(_PART_WORDS.search(name))
    flagged = bool(e and e.is_build_component)
    if not path_part and not name_part and not flagged:
        return False
    prime_like = r.is_prime or bool(re.search(r"\bprime\b", name)) or bool(re.search(r"prime", u, re.I))
    return bool((e is not None and e.tradable is True) or (prime_like and path_part and name_part)
                or (weapon_part_recipe and name_part))


def canonical_build_part_name(u: str, name: str) -> str:
    out = name
    if re.search(r"/Types/Recipes/\S*Component$", u, re.I):
        out = re.sub(r"\s+Blueprint$", "", out, flags=re.I)
    if re.search(r"/Types/Recipes/WarframeRecipes/", u, re.I):
        out = re.sub(r"\bHelmet(?= Blueprint$|$)", "Neuroptics", out, flags=re.I)
    return out


def is_market_listed_mission_key(u: str, market_listed: bool) -> bool:
    return market_listed and bool(re.search(r"/Types/Keys/", u, re.I))


def is_ranked_group(group: str | None) -> bool:
    return group in ("mods", "arcanes")


_EXALTED_NAMES = re.compile(r"^(Exalted Blade|Regulators(?: Prime)?|Iron Staff(?: Prime)?|Dex Pixia(?: Prime)?|"
                            r"Artemis Bow(?: Prime)?|Desert Wind(?: Prime)?)$", re.I)


def should_hide(u: str, e: Entry | None, r: Resolved, market_listed: bool = False) -> bool:
    if is_market_listed_mission_key(u, market_listed):
        return False
    if is_auxiliary(u, e, r):
        return True
    if re.search(r"/Upgrades/Focus/|/Types/Boosters?/", u, re.I):
        return True
    if re.search(r"/Types/Keys/", u, re.I) and not is_relic_like(u, e, r):
        return True
    if e is not None and (e.product_category == "SpecialItems" or "exalted" in _typ(e)):
        return True
    if _EXALTED_NAMES.match(r.name):
        return True
    return bool(re.search(r"/ExaltedWeapons?/|/SpecialItems/", u))


_WEAPON_SLOTS = {"primary", "secondary", "melee"}
MODULAR_PART_PATH = re.compile(r"/(?:kdrives|zaws|kitguns|hoverboard|moapets|operatoramplifiers?)/|"
                               r"/(?:infkitgun|modularmelee|sumodular)[a-z0-9]*/", re.I)
PET_PART_PATH = re.compile(r"/(?:pets|zanukapets|creaturepets|catbrowpets|kubrowpets|sentinels)/", re.I)
_FILTER_BY_PRODUCT = {k: c for k, c, _ in CATEGORIES}


def infer_category(u: str, default_cat: str, e: Entry | None) -> str:
    if re.search(r"/OperatorAmplifiers?/", u, re.I):
        return "amps"
    mapped = _FILTER_BY_PRODUCT.get(e.product_category) if e and e.product_category else None
    if not mapped:
        return default_cat
    if mapped in _WEAPON_SLOTS and (MODULAR_PART_PATH.search(u) or PET_PART_PATH.search(u)):
        return default_cat
    return mapped


_MODULAR_KINDS = {
    "LongGuns": ("primary", "Kitgun", re.compile(r"/Barrels?/", re.I)),
    "Pistols": ("secondary", "Kitgun", re.compile(r"/Barrels?/", re.I)),
    "Melee": ("melee", "Zaw", re.compile(r"/Tips?/", re.I)),
    "OperatorAmps": ("amps", "Amp", re.compile(r"/Barrel/", re.I)),
    "Hoverboards": ("misc", "K-Drive", re.compile(r"Deck$", re.I)),
    "MoaPets": ("companions", "Moa", re.compile(r"MoaPetHead", re.I)),
    "KubrowPets": ("companions", "Companion", None),
}
_HOUND_KIND = ("companions", "Hound", re.compile(r"ZanukaPetPartHead", re.I))
_ALWAYS_MODULAR = {"Hoverboards", "MoaPets"}


def resolve_modular_build(source_key: str, entry: dict, u: str, db: Db) -> dict | None:
    kind = _HOUND_KIND if source_key == "MoaPets" and re.search(r"/ZanukaPets/", u, re.I) \
        else _MODULAR_KINDS.get(source_key)
    if not kind:
        return None
    raw = entry.get("ModularParts")
    parts = [p for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []
    if not parts and source_key not in _ALWAYS_MODULAR:
        return None
    cat, label, pattern = kind
    defining = next((p for p in parts if pattern.search(p)), None) if pattern else None
    de, be = db.get(defining or ""), db.get(u)
    naming = de if de and de.name else (be if be and be.name else None)
    return {"name": sanitize_display_name(naming.name if naming else label), "category": cat,
            "categoryLabel": label, "image": (de.image_name if de else None) or (be.image_name if be else None),
            "partNames": [db.name_of(p) for p in parts]}


def derive_group(source_key: str, u: str, e: Entry | None, r: Resolved) -> str:
    cat, typ = _cat(e), _typ(e)
    if is_focus_upgrade(u, e, r):
        return "misc"
    if source_key in EQUIPMENT_COLLECTIONS or source_key == PET_COLLECTION_KEY:
        return "equipment"
    if source_key == "LevelKeys":
        return "relics" if is_relic_like(u, e, r) else "misc"
    if source_key == "Arcanes":
        return "arcanes"
    if source_key in ("Upgrades", "RawUpgrades"):
        if is_arcane_upgrade(u, e, r):
            return "arcanes"
        return "mods" if is_likely_mod_upgrade(u, e, r) else "misc"
    if is_relic_like(u, e, r):
        return "relics"
    if is_arcane_upgrade(u, e, r):
        return "arcanes"
    arc, mod = re.compile(r"\barcane\b"), re.compile(r"\bmods?\b")
    if arc.search(cat):
        return "arcanes"
    if mod.search(cat):
        return "mods"
    if arc.search(typ):
        return "arcanes"
    if mod.search(typ):
        return "mods"
    if is_build_part_item(u, e, r):
        return "all_parts"
    return "misc"


# ---------------------------------------------------------------------------------------------
# entryNormalization.ts + rankExtraction.ts
# ---------------------------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
            return f if math.isfinite(f) else None
        except ValueError:
            return None
    return None


def pick_numeric(entry: dict, keys: Iterable[str]) -> float | None:
    for k in keys:
        v = _num(entry.get(k))
        if v is not None:
            return v
    return None


def _norm_key(k: str) -> str:
    return re.sub(r"[^a-z]", "", k.lower())


def deep_find_numeric(value: Any, keys: set[str], max_depth: int = 3, depth: int = 0) -> float | None:
    if depth > max_depth or value is None:
        return None
    if isinstance(value, list):
        for v in value:
            f = deep_find_numeric(v, keys, max_depth, depth + 1)
            if f is not None:
                return f
        return None
    if not isinstance(value, dict):
        return None
    for k, v in value.items():
        if _norm_key(k) in keys:
            n = _num(v)
            if n is not None:
                return n
    for v in value.values():
        f = deep_find_numeric(v, keys, max_depth, depth + 1)
        if f is not None:
            return f
    return None


def has_any_rank_signal(value: Any) -> bool:
    if isinstance(value, list):
        return any(has_any_rank_signal(v) for v in value)
    if not isinstance(value, dict):
        return False
    for k, v in value.items():
        nk = _norm_key(k)
        if "fingerprint" in nk:
            continue
        if "rank" in nk or "level" in nk or "fusion" in nk:
            n = _num(v)
            if n is not None and n > 0:
                return True
        if isinstance(v, (dict, list)) and has_any_rank_signal(v):
            return True
    return False


_RANK_KEYS = {"rank", "rnk", "level", "lvl", "lv", "modrank", "upgraderank", "upgradelvl", "fusionlevel", "currentrank",
              "currentlevel", "currentlvl", "itemlevel", "itemlvl", "arcanerank", "arcanelvl"}
_MAX_RANK_KEYS = {"maxrank", "maxlevel", "maxlvl", "maxlv", "itemmaxrank", "maxupgraderank", "maxupgradelvl",
                  "maxupgradelevel", "maxmodrank", "maxarcanelvl", "maxarcanelv", "maxarcanerank"}
_FP_RANK_KEYS = {"lvl", "level", "rank", "modrank", "upgraderank", "upgradelvl", "fusionlevel", "currentrank",
                 "currentlevel", "itemlevel", "arcanerank"}
MAX_ITEM_RANK = 30
XP_PER_RANK = 6_000  # config/game/constants.ts


def _fingerprint_rank(entry: dict) -> int | None:
    raw = entry.get("UpgradeFingerprint", entry.get("upgradeFingerprint"))
    cur = raw
    for _ in range(3):
        if not isinstance(cur, str):
            break
        s = cur.strip()
        if not s:
            return None
        try:
            cur = json.loads(s)
        except ValueError:
            return None
    if not isinstance(cur, dict):
        return None
    rank = pick_numeric(cur, ["lvl", "level", "rank", "ModRank", "FusionLevel", "UpgradeLevel", "CurrentLevel",
                              "CurrentRank", "ArcaneRank", "ItemLevel", "ItemRank", "UpgradeRank"])
    if rank is None:
        rank = deep_find_numeric(cur, _FP_RANK_KEYS, 2)
    if rank is None or rank < 0:
        return None
    return int(rank // 1)


def normalize_rank(entry: dict, group: str, max_rank_db: int | None) -> tuple[int, int]:
    explicit_max = pick_numeric(entry, ["MaxRank", "ItemMaxRank", "UpgradeMax", "UpgradeMaxRank", "MaxUpgradeLevel",
                                        "MaxLevel", "MaxArcaneLevel"])
    if explicit_max is None:
        explicit_max = deep_find_numeric(entry, _MAX_RANK_KEYS)
    fallback = 10 if group == "mods" else 5 if group == "arcanes" else MAX_ITEM_RANK
    if explicit_max is not None and explicit_max > 0:
        max_rank = int(explicit_max // 1)
    elif max_rank_db:
        max_rank = int(max_rank_db)
    else:
        max_rank = fallback
    rank = pick_numeric(entry, ["Rank", "ItemLevel", "Level", "ModRank", "FusionLevel", "UpgradeLevel", "CurrentLevel",
                                "CurrentRank", "ArcaneRank", "ItemRank", "UpgradeRank"])
    if rank is None:
        rank = deep_find_numeric(entry, _RANK_KEYS)
    if rank is None:
        fr = _fingerprint_rank(entry)
        rank = float(fr) if fr is not None else None
    if rank is not None:
        return min(max(0, int(rank // 1)), max_rank), max_rank
    if group in ("mods", "arcanes"):
        return 0, max_rank
    xp = _num(entry.get("XP")) or 0
    r = int(xp // XP_PER_RANK) if xp > 0 else 0
    return min(r, max_rank), max_rank


def pick_boolean(entry: dict, keys: Iterable[str]) -> bool | None:
    for k in keys:
        v = entry.get(k)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("true", "yes", "1"):
                return True
            if low in ("false", "no", "0"):
                return False
    return None


def parse_amount(entry: dict) -> int:
    raw = pick_numeric(entry, ["ItemCount", "Count", "StackCount", "Quantity"])
    if raw is None:
        raw = deep_find_numeric(entry, {"itemcount", "count", "stackcount", "quantity"})
    if raw is None:
        raw = 1
    return int(raw // 1) if raw > 0 else 1


_EQUIP_KEYS = {"equippedon", "installedon", "ownername", "hostitemname", "weaponname", "warframename",
               "companionname"}


def _displayable_context(v: str) -> bool:
    t = v.strip()
    if len(t) < 3 or len(t) > 60 or t.startswith("/Lotus/"):
        return False
    if re.match(r"^[A-Za-z]:\\", t) or re.match(r"^https?://", t, re.I):
        return False
    if any(c in t for c in '[]{}"`'):
        return False
    if re.fullmatch(r"[a-f0-9]{16,}", t, re.I):
        return False
    if re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", t, re.I):
        return False
    return bool(re.search(r"[A-Za-z]{3,}", t))


def _collect_contexts(value: Any, out: list, max_depth: int = 3, depth: int = 0, capture: bool = False) -> None:
    if depth > max_depth or value is None:
        return
    if isinstance(value, str):
        if capture and _displayable_context(value) and value.strip() not in out:
            out.append(value.strip())
        return
    if isinstance(value, list):
        for v in value:
            _collect_contexts(v, out, max_depth, depth + 1, capture)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _collect_contexts(v, out, max_depth, depth + 1, capture or _norm_key(k) in _EQUIP_KEYS)


def extract_equip_contexts(entry: dict) -> list[str]:
    out: list[str] = []
    _collect_contexts(entry, out)
    return out[:4]


def collect_equipped_upgrade_ids(data: dict) -> set[str]:
    ids: set[str] = set()
    for coll in data.values():
        if not isinstance(coll, list):
            continue
        for gear in coll:
            if not isinstance(gear, dict) or not isinstance(gear.get("Configs"), list):
                continue
            for cfg in gear["Configs"]:
                if not isinstance(cfg, dict):
                    continue
                for key in ("Upgrades", "PvpUpgrades"):
                    refs = cfg.get(key)
                    if isinstance(refs, list):
                        ids.update(r for r in refs if isinstance(r, str) and r)
    return ids


def entry_instance_id(entry: dict) -> str | None:
    raw = entry.get("ItemId")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("$oid"), str) and raw["$oid"]:
        return raw["$oid"]
    return None


def normalize_collection_entries(value: Any, max_depth: int = 4, depth: int = 0) -> list[dict]:
    if depth > max_depth or value is None:
        return []
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(normalize_collection_entries(v, max_depth, depth + 1))
        return out
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("ItemType"), str):
        return [value]
    out = []
    for v in value.values():
        out.extend(normalize_collection_entries(v, max_depth, depth + 1))
    return out


_GROUP_PRIORITY = {"misc": 1, "equipment": 2, "all_parts": 3, "arcanes": 4, "mods": 5, "relics": 6,
                   "incomplete_sets": 7, "full_sets": 8}


def prefer_group(current: str | None, nxt: str) -> str:
    if not current:
        return nxt
    return nxt if _GROUP_PRIORITY.get(nxt, 0) > _GROUP_PRIORITY.get(current, 0) else current


def merge_optional_bool(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    if a is False or b is False:
        return False
    return None


def merge_equip_contexts(a: list | None, b: list | None) -> list | None:
    merged: list[str] = []
    for v in (a or []) + (b or []):
        if _displayable_context(v) and v.strip() not in merged:
            merged.append(v.strip())
    return merged[:6] or None


# ---------------------------------------------------------------------------------------------
# parseInventory (inventory.ts)
# ---------------------------------------------------------------------------------------------

def parse_inventory(data: dict, db: Db, market_refs: set[str] | frozenset = frozenset()) -> list[dict]:
    """Parsed rows (WFHelper ParsedItem as dicts) + synthesized full / incomplete set rows."""
    item_map: dict[str, dict] = {}
    sellable_equipment: dict[str, int] = {}
    equipped_ids = collect_equipped_upgrade_ids(data)

    def add_entry(entry: dict, source_key: str, default_cat: str, default_label: str) -> None:
        u = entry.get("ItemType")
        if not isinstance(u, str) or not u:
            return
        r = resolve_item(u, db)
        e = db.get(u)
        modular = resolve_modular_build(source_key, entry, u, db)
        account_bound = modular is not None or source_key == PET_COLLECTION_KEY
        listed = u.lower() in market_refs
        if not modular and should_hide(u, e, r, listed):
            return
        if not modular and is_resource_item(u, e, r):
            return
        group = derive_group(source_key, u, e, r)
        recipe_path = bool(re.search(r"/Types/Recipes/", u, re.I))
        if group == "all_parts" and recipe_path and market_refs and not listed:
            group = "misc"
        cat = infer_category(u, default_cat, e)
        label = LABEL_BY_CAT.get(cat, default_label)
        if modular:
            group, cat, label = "equipment", modular["category"], modular["categoryLabel"]
        elif group == "arcanes":
            cat, label = "arcanes", "Arcane"
        elif group == "mods":
            cat, label = "mods", "Mod"
        elif group == "relics":
            cat, label = "relics", "Relic"
        elif group == "misc" and (source_key == "LevelKeys" or is_market_listed_mission_key(u, listed)):
            cat, label = "misc", "Key"
        elif group == "misc" and source_key in ("Upgrades", "RawUpgrades"):
            cat, label = "misc", "Misc"
        elif is_focus_upgrade(u, e, r):
            cat, label = "misc", "Focus"

        rank, max_rank = normalize_rank(entry, group, db.extra.max_rank.get(u))
        amount = parse_amount(entry)
        equipped_in = extract_equip_contexts(entry)
        favorite = pick_boolean(entry, ["Favorite", "IsFavorite", "favorite", "isFavorite"])
        equipped = pick_boolean(entry, ["Equipped", "IsEquipped", "Installed", "IsInstalled", "InUse"])
        iid = entry_instance_id(entry)
        cfg_eq = True if iid and iid in equipped_ids else None
        inferred_eq = equipped if equipped is not None else (cfg_eq if cfg_eq is not None else (True if equipped_in else None))
        name = modular["name"] if modular else canonical_build_part_name(u, r.name)
        ducats = e.ducats if e is not None and isinstance(e.ducats, (int, float)) else None
        key = (f"{u}#b{iid or '|'.join(modular['partNames'])}" if modular
               else (f"{u}#r{rank}m{max_rank}" if group in ("mods", "arcanes") else u))
        if recipe_path:
            tradable = listed if market_refs else bool(e.tradable if e and e.tradable is not None else r.is_prime)
        else:
            tradable = (is_market_listed_mission_key(u, listed) or (listed and is_ranked_group(group))
                        or bool(e.tradable if e and e.tradable is not None else r.is_prime))
        xp = _num(entry.get("XP")) or 0
        if not account_bound and group == "equipment" and rank == 0 and xp <= 0:
            sellable_equipment[u] = sellable_equipment.get(u, 0) + amount
        item = {
            "name": name, "internalName": u, "category": cat, "categoryLabel": label, "rank": rank,
            "maxRank": max_rank, "image": (modular["image"] if modular else (e.image_name if e else None)),
            "isPrime": r.is_prime, "partType": "prime" if r.is_prime else "normal",
            "masteryReq": e.mastery_req if e else 0, "vaulted": bool(e.vaulted) if e else False,
            "tradable": False if account_bound else tradable, "amount": amount, "inventoryGroup": group,
            "leveledUp": rank > 0 or has_any_rank_signal(entry), "ducats": None if account_bound else ducats,
            "keywords": [source_key.lower()], "inventoryKey": key,
        }
        if r.name_is_fallback and not modular:
            item["nameIsFallback"] = True
        if modular and modular["partNames"]:
            item["modularParts"] = modular["partNames"]
        if favorite is not None:
            item["favorite"] = favorite
        if inferred_eq is not None:
            item["equipped"] = inferred_eq
        if equipped_in:
            item["equippedIn"] = equipped_in
        prev = item_map.get(key)
        if prev is None:
            item_map[key] = item
            return
        prev["amount"] = (prev.get("amount") or 0) + (item["amount"] or 0)
        prev["rank"] = max(prev["rank"], item["rank"])
        prev["maxRank"] = max(prev["maxRank"], item["maxRank"])
        prev["leveledUp"] = bool(prev.get("leveledUp") or item["leveledUp"])
        for k in ("favorite", "equipped"):
            m = merge_optional_bool(prev.get(k), item.get(k))
            if m is not None:
                prev[k] = m
        m = merge_equip_contexts(prev.get("equippedIn"), item.get("equippedIn"))
        if m:
            prev["equippedIn"] = m
        prev["inventoryGroup"] = prefer_group(prev.get("inventoryGroup"), item["inventoryGroup"] or "misc")
        if prev["category"] == "misc" and item["category"] != "misc":
            prev["category"], prev["categoryLabel"] = item["category"], item["categoryLabel"]
        for kw in item["keywords"]:
            if kw not in prev["keywords"]:
                prev["keywords"].append(kw)

    for key, cat, label in CATEGORIES:
        for entry in normalize_collection_entries(data.get(key)):
            add_entry(entry, key, cat, label)
    for key in MODULAR_COLLECTION_KEYS:
        for entry in normalize_collection_entries(data.get(key)):
            if resolve_modular_build(key, entry, entry["ItemType"], db):
                add_entry(entry, key, "misc", "Misc")
            elif key == PET_COLLECTION_KEY:
                add_entry(entry, key, "companions", "Companion")
    for key, cat, label in SUPPLEMENTAL_COLLECTIONS:
        for entry in normalize_collection_entries(data.get(key)):
            add_entry(entry, key, cat, label)

    owned_counts: dict[str, float] = {}
    combined: dict[str, int] = {}
    for key, item in item_map.items():
        owned_counts[key] = item.get("amount") or 0
        if item["inventoryGroup"] in ("mods", "arcanes"):
            combined[item["internalName"]] = combined.get(item["internalName"], 0) + (item.get("amount") or 0)
    for item in item_map.values():
        if item["internalName"] in combined:
            item["combinedAmount"] = combined[item["internalName"]]
    return list(item_map.values()) + build_full_set_items(db, owned_counts, sellable_equipment)


# ---------------------------------------------------------------------------------------------
# fullSets.ts
# ---------------------------------------------------------------------------------------------

FULL_SET_SUFFIX = "#set"


def set_root_of(internal_name: str) -> str:
    return internal_name[: -len(FULL_SET_SUFFIX)] if internal_name.endswith(FULL_SET_SUFFIX) else internal_name


def component_aliases(u: str) -> list[str]:
    from .inventory.model import component_aliases as aliases

    return aliases(u)


def owned_component_count(u: str | None, owned: dict) -> float:
    if not u:
        return 0
    return max((owned.get(a, 0) or 0) for a in component_aliases(u))


def is_crafting_resource(u: str) -> bool:
    return bool(re.search(r"/(MiscItems|Research)/", u, re.I))


def _generic_set_component(c: dict, db: Db, prime_root: bool) -> bool:
    u = c.get("uniqueName") or ""
    if not u or is_crafting_resource(u):
        return False
    e = db.get(u)
    if (e is not None and e.is_build_component is False) or (e is not None and e.masterable):
        return False
    if not (e is not None and e.is_build_component) and not re.search(r"/Types/Recipes/", u, re.I):
        return False
    if c.get("tradable") is True or (e is not None and e.tradable is True) or prime_root:
        return True
    return bool(re.search(r"/Types/Recipes/Weapons/WeaponParts?/", u, re.I))


def _eligible_set_root(u: str, e: Entry, r: Resolved, n: int) -> bool:
    if n < 2 or is_ayatan_like(u, e, r) or is_scene_like(u, e, r) or is_relic_like(u, e, r):
        return False
    typ, name = _typ(e), r.name.lower()
    if any(w in typ for w in ("captura", "ayatan", "resource", "booster")):
        return False
    if "ayatan" in name or name.endswith(" scene") or "booster" in name or "quest" in name:
        return False
    if r.is_prime or re.search(r"\bprime\b", r.name, re.I):
        return True
    if re.search(r"(warframe|rifle|shotgun|sniper|bow|pistol|melee|companion|sentinel|archwing|necramech|orbiter|"
                 r"landing craft)", typ):
        return True
    return bool(re.search(r"(warframe|weapon|primary|secondary|melee|sentinel|pet|companion|archwing|necramech|arch)",
                          _cat(e)))


def _merge_dupes(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    idx: dict[str, int] = {}
    for row in rows:
        k = row.get("uniqueName") or ""
        n = row.get("itemCount") or 1
        if k and k in idx:
            t = out[idx[k]]
            out[idx[k]] = {**t, "itemCount": (t.get("itemCount") or 1) + n}
            continue
        if k:
            idx[k] = len(out)
        out.append({**row, "itemCount": n})
    return out


def _set_roots(db: Db) -> list[Entry]:
    cache = getattr(db, "_set_roots", None)
    if cache is not None:
        return cache
    roots = []
    for e in db.all_entries():
        comps = e.components or []
        if not comps:
            continue
        r = Resolved(sanitize_display_name(e.name), e)
        prime_root = r.is_prime or bool(re.search(r"\bPrime\b", r.name))
        parts = _merge_dupes([c for c in comps if _generic_set_component(c, db, prime_root)])
        if not parts or not _eligible_set_root(e.unique_name, e, r, len(parts)):
            continue
        roots.append((e, r, prime_root, parts))
    db._set_roots = roots  # type: ignore[attr-defined]
    return roots


def build_full_set_items(db: Db, owned_counts: dict, sellable_equipment: dict | None = None) -> list[dict]:
    out = []
    for e, r, prime_root, parts in _set_roots(db):
        complete = math.inf
        hydrated = []
        for c in parts:
            u = c.get("uniqueName") or ""
            need = c.get("itemCount") if isinstance(c.get("itemCount"), int) and c["itemCount"] > 0 else 1
            ce = db.get(u)
            pool = sellable_equipment if ce is not None and ce.masterable and sellable_equipment is not None \
                else owned_counts
            own = owned_component_count(u, pool)
            complete = min(complete, int(own // need))
            hydrated.append({"name": c.get("name") or db.name_of(u), "uniqueName": u, "itemCount": need,
                             "ownedCount": int(own), "owned": own >= need, "ducats": c.get("ducats")})
        if not math.isfinite(complete):
            complete = 0
        total = len(hydrated)
        owned_types = sum(1 for c in hydrated if c["owned"])
        set_name = r.name if r.name.endswith(" Set") else f"{r.name} Set"
        common = {"name": set_name, "internalName": e.unique_name + FULL_SET_SUFFIX, "rank": 0, "maxRank": 1,
                  "image": e.image_name, "isPrime": prime_root, "masteryReq": e.mastery_req,
                  "vaulted": bool(e.vaulted), "tradable": True, "components": hydrated,
                  "partType": "prime" if prime_root else "normal", "leveledUp": False,
                  "setSlug": e.wfm_slug if e.wfm_slug and e.wfm_slug.endswith("_set") else None,
                  "inventoryKey": e.unique_name + FULL_SET_SUFFIX}
        if complete >= 1:
            out.append({**common, "category": "full_sets", "categoryLabel": "Full Set", "amount": complete,
                        "completeSets": complete, "inventoryGroup": "full_sets",
                        "keywords": ["set", "full set", r.name.lower()]})
        elif owned_types > 0:
            out.append({**common, "category": "incomplete_sets", "categoryLabel": "Incomplete Set", "amount": 0,
                        "completeSets": 0, "missingParts": total - owned_types, "ownedPartTypes": owned_types,
                        "totalPartTypes": total, "inventoryGroup": "incomplete_sets",
                        "keywords": ["set", "incomplete set", "missing", r.name.lower()]})
    return out


# ---------------------------------------------------------------------------------------------
# foundryResources.ts
# ---------------------------------------------------------------------------------------------

EQUIPMENT_CATEGORY_ORDER = ["Warframe", "Primary", "Secondary", "Melee", "Archwing", "Companion", "Appearance",
                            "Gear", "Modular", "Misc"]


def de_date_ms(value: Any) -> int | None:
    """Mongo {"$date": {"$numberLong": "..."}} / ISO / epoch ms -> epoch ms."""
    if not value:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            n = _num(value)
            return int(n) if n is not None else None
    if isinstance(value, dict):
        d = value.get("$date", value)
        if isinstance(d, dict) and "$numberLong" in d:
            n = _num(d["$numberLong"])
            return int(n) if n is not None else None
        if d is value:
            return None
        return de_date_ms(d)
    return None


def classify_for_foundry(product_un: str | None, blueprint_un: str, db: Db) -> str:
    pe = db.get(product_un) if product_un else None
    pc = (pe.product_category if pe else "").lower()
    cat = _cat(pe)
    parent_un = pe.component_of if pe else None
    par = db.get(parent_un) if parent_un else None
    ppc = (par.product_category if par else "").lower()
    pcat = _cat(par)
    joined = f"{product_un or ''} {parent_un or ''} {blueprint_un}".lower()
    if MODULAR_PART_PATH.search(joined):
        return "Modular"
    if PET_PART_PATH.search(joined):
        return "Companion"
    space = ("spacesuits", "spaceguns", "spacemelee")
    if pc in space or ppc in space or cat.startswith("arch") or pcat.startswith("arch") \
            or re.search(r"/(archwing|spacesuits|spaceguns|spacemelee)/", joined):
        return "Archwing"
    if pc in ("suits", "mechsuits") or ppc in ("suits", "mechsuits") or cat in ("warframe", "warframes") \
            or pcat in ("warframe", "warframes") or re.search(r"/(warframerecipes|powersuits)/", joined):
        return "Warframe"
    if pc in ("sentinels", "kubrowpets") or ppc in ("sentinels", "kubrowpets") \
            or cat in ("companion", "sentinels", "pets") or pcat == "companion":
        return "Companion"
    if pc == "longguns" or ppc == "longguns" or cat == "primary" or pcat == "primary":
        return "Primary"
    if pc == "pistols" or ppc == "pistols" or cat == "secondary" or pcat == "secondary":
        return "Secondary"
    if pc == "melee" or ppc == "melee" or cat == "melee" or pcat == "melee":
        return "Melee"
    if cat == "gear" or pcat == "gear" or "/gear/" in joined:
        return "Gear"
    if cat in ("cosmetic", "appearance") or "/customs/" in joined:
        return "Appearance"
    return "Misc"


def _resolve_product(bp: str, db: Db) -> dict:
    product = db.blueprint_to_product.get(bp)
    category = classify_for_foundry(product, bp, db)
    if product:
        return {"name": db.name_of(product), "productUniqueName": product, "category": category,
                "image": (db.get(product).image_name if db.get(product) else None)}
    r = resolve_item(bp, db)
    name = re.sub(r"\s+Blueprint\s*$", "", r.name, flags=re.I).strip() or r.name
    return {"name": name, "productUniqueName": None, "category": category,
            "image": r.entry.image_name if r.entry else None}


def _recipe_details(product: str | None, bp: str, db: Db) -> dict:
    src = (db.recipe_by_product.get(product) if product else None) or db.extra.recipes.get(bp)
    if src is None:
        return {"ingredients": [], "buildPrice": 0, "buildTime": 0}
    return {"ingredients": [{"uniqueName": u, "count": c} for u, c in src.ingredients],
            "buildPrice": src.build_price, "buildTime": src.build_time}


def parse_foundry(data: dict, db: Db) -> dict:
    building, recipes = [], []
    for e in data.get("PendingRecipes") or []:
        if not isinstance(e, dict) or not isinstance(e.get("ItemType"), str):
            continue
        bp = e["ItemType"]
        p = _resolve_product(bp, db)
        d = _recipe_details(p["productUniqueName"], bp, db)
        building.append({**p, "endMs": de_date_ms(e.get("CompletionDate")), "uniqueName": bp, **d})
    for e in data.get("Recipes") or []:
        if not isinstance(e, dict) or not isinstance(e.get("ItemType"), str):
            continue
        bp = e["ItemType"]
        p = _resolve_product(bp, db)
        pu = p["productUniqueName"]
        d = _recipe_details(pu, bp, db)
        pe = db.get(pu) if pu else None
        count = e.get("ItemCount")
        recipes.append({**p, "count": count if isinstance(count, (int, float)) and not isinstance(count, bool) else 1,
                        "uniqueName": bp,
                        "isIngredient": bool(pu and pu in db.ingredient_set and not (pe and pe.component_of)), **d})
    return {"building": building, "recipes": recipes}


def parse_resources(data: dict, db: Db) -> list[dict]:
    out = []
    for item in data.get("MiscItems") or []:
        if not isinstance(item, dict):
            continue
        u = item.get("ItemType") or ""
        r = resolve_item(u, db)
        if not is_resource_item(u, r.entry, r):
            continue
        c = item.get("ItemCount")
        out.append({"name": r.name, "internalName": u, "image": r.entry.image_name if r.entry else None,
                    "count": c if isinstance(c, (int, float)) and not isinstance(c, bool) else 0})
    out.sort(key=lambda x: -x["count"])
    return out


CHAIN_MAX_DEPTH = 4


def chain_buildable_blueprints(recipes: list[dict], owned: dict, db: Db) -> set[str]:
    by_product: dict[str, dict] = {}
    for r in recipes:
        if r["count"] <= 0 or not r.get("productUniqueName"):
            continue
        by_product.setdefault(r["productUniqueName"], r)

    def satisfy(ings, mult, pool, blueprints, stack) -> bool:
        for ing in ings:
            u, needed = ing["uniqueName"], ing["count"] * mult
            avail = pool.get(u, 0)
            used = min(avail, needed)
            if used > 0:
                pool[u] = avail - used
            deficit = needed - used
            if deficit <= 0:
                continue
            sub = by_product.get(u)
            if not sub or not sub["ingredients"]:
                return False
            if u in stack or len(stack) >= CHAIN_MAX_DEPTH:
                return False
            rec = db.recipe_by_product.get(u)
            per = (rec.num if rec else 1) or 1
            builds = math.ceil(deficit / per)
            bp = sub.get("uniqueName")
            bpe = db.get(bp) if bp else None
            reusable = bool((rec and rec.reusable) or (bpe and bpe.reusable_blueprint))
            if not reusable:
                copies = blueprints.get(u, sub["count"])
                if copies < builds:
                    return False
                blueprints[u] = copies - builds
            stack.add(u)
            ok = satisfy(sub["ingredients"], builds, pool, blueprints, stack)
            stack.discard(u)
            if not ok:
                return False
        return True

    out = set()
    for r in recipes:
        if not r.get("uniqueName") or r["count"] <= 0 or not r["ingredients"]:
            continue
        pu = r.get("productUniqueName")
        pe = db.get(pu) if pu else None
        if not pu or (pe and pe.component_of):
            continue
        if satisfy(r["ingredients"], 1, dict(owned), {}, set()):
            out.add(r["uniqueName"])
    return out


def is_foundry_recipe_ready(recipe: dict, owned: dict, chain: set[str]) -> bool:
    if not recipe["ingredients"]:
        return False
    if all(owned.get(i["uniqueName"], 0) >= i["count"] for i in recipe["ingredients"]):
        return True
    return bool(recipe.get("uniqueName") and recipe["uniqueName"] in chain)


def compute_pinned_totals(entries: list[dict], pinned: set[str], owned_of: Callable[[str], float]) -> dict:
    needed: dict[str, int] = {}
    seen: set[str] = set()
    credits = count = 0
    for e in entries:
        if e.get("source") != "blueprint" or not e.get("uniqueName"):
            continue
        if e["uniqueName"] not in pinned or e["uniqueName"] in seen:
            continue
        seen.add(e["uniqueName"])
        count += 1
        credits += e.get("buildPrice") or 0
        for ing in e.get("ingredients") or []:
            needed[ing["uniqueName"]] = needed.get(ing["uniqueName"], 0) + ing["count"]
    resources = sorted(({"uniqueName": u, "needed": n, "owned": owned_of(u)} for u, n in needed.items()),
                       key=lambda r: -r["needed"])
    return {"count": count, "credits": credits, "resources": resources,
            "missing": [r for r in resources if r["owned"] < r["needed"]]}


# ---------------------------------------------------------------------------------------------
# valueTotals.ts
# ---------------------------------------------------------------------------------------------

VALUE_MIN_PLATINUM_PRESETS = (0, 5, 10, 15)
_AGGREGATE_GROUPS = {"full_sets", "incomplete_sets"}


def _is_prime_row(e: dict) -> bool:
    return e.get("partType") == "prime" or e.get("isPrime") is True


def _is_excluded_slug(slug: str) -> bool:
    from .data.market import is_excluded

    return is_excluded(slug)


def _is_sellable(e: dict) -> bool:
    if e.get("tradable") is False or e.get("inventoryGroup") in _AGGREGATE_GROUPS:
        return False
    slug = e.get("marketSlug")
    return isinstance(slug, str) and bool(slug) and not _is_excluded_slug(slug)


def _stack_size(e: dict) -> int:
    a = e.get("amount")
    if a is None:
        return 1
    n = _num(a)
    if n is None or n <= 0:
        return 0
    return max(1, int(n // 1))


def is_counted_for_value(e: dict, scope: str) -> bool:
    if not _is_sellable(e):
        return False
    if scope == "tradable":
        return True
    return e.get("inventoryGroup") == "all_parts" and _is_prime_row(e)


def _passes_floor(e: dict, min_plat: float) -> bool:
    if not (min_plat > 0):
        return True
    m = _num(e.get("platinum"))
    if m is None or m <= 0:
        return True
    return m >= min_plat


def compute_value_totals(entries: Iterable[dict], scope: str, min_plat: float = 0) -> dict:
    plat = ducats = 0.0
    pu = du = unpriced = counted = 0
    for e in entries:
        if not is_counted_for_value(e, scope) or not _passes_floor(e, min_plat):
            continue
        q = _stack_size(e)
        if q <= 0:
            continue
        counted += 1
        hole = False
        m = _num(e.get("platinum"))
        if m is not None and m > 0:
            plat += m * q
        else:
            pu += 1
            hole = True
        if _is_prime_row(e):
            d = _num(e.get("ducats"))
            if d is not None and d > 0:
                ducats += d * q
            else:
                du += 1
                hole = True
        if hole:
            unpriced += 1
    return {"platinum": round(plat), "ducats": round(ducats), "platinumUnpriced": pu, "ducatsUnpriced": du,
            "unpriced": unpriced, "counted": counted}


def select_value_strip_rows(in_view: dict, inventory: dict) -> list[str]:
    if in_view["counted"] == 0:
        return ["inventory"] if inventory["counted"] > 0 else []
    if in_view["counted"] >= inventory["counted"]:
        return ["inView"]
    return ["inView", "inventory"]


# ---------------------------------------------------------------------------------------------
# filters.ts
# ---------------------------------------------------------------------------------------------

DESC_DEFAULT = {"platinum", "ducats", "amount", "count", "owned", "disposition", "rerolls", "grade", "attr_grade",
                "ducatonator", "complete_sets", "mastery_xp", "parts_owned", "ev", "ducat"}

DEFAULT_FILTERS = {"search": "", "primeMode": "all", "sortBy": "name", "sortDirection": "asc", "vaulted": "all",
                   "partType": "all", "favorite": "all", "equipped": "all", "leveledUp": "all",
                   "minimumPlatinum": 0, "minimumAmount": 0, "foundryState": "all"}


def default_sort_direction(sort_by: str) -> str:
    return "desc" if sort_by in DESC_DEFAULT else "asc"


def matches_search(item: dict, search: str) -> bool:
    q = (search or "").strip().lower()
    if not q:
        return True
    for k in ("name", "displayName", "category", "categoryLabel", "internalName"):
        v = item.get(k)
        if isinstance(v, str) and q in v.lower():
            return True
    kws = item.get("keywords")
    return isinstance(kws, list) and any(isinstance(k, str) and q in k.lower() for k in kws)


def _yes_no(mode: str, value: Any) -> bool:
    if mode == "yes":
        return value is True
    if mode == "no":
        return value is not True
    return True


def matches_shared_filters(item: dict, f: dict) -> bool:
    if not matches_search(item, f.get("search", "")):
        return False
    pm = f.get("primeMode", "all")
    if pm == "prime" and item.get("isPrime") is not True:
        return False
    if pm == "non_prime" and item.get("isPrime") is not False:
        return False
    if not _yes_no(f.get("vaulted", "all"), item.get("vaulted")):
        return False
    pt = f.get("partType", "all")
    if pt != "all" and (item.get("partType") or ("prime" if item.get("isPrime") else "normal")) != pt:
        return False
    for k in ("favorite", "equipped", "leveledUp"):
        if not _yes_no(f.get(k, "all"), item.get(k)):
            return False
    fs = f.get("foundryState", "all")
    st = item.get("foundryState")
    if fs == "claimable" and st != "claimable":
        return False
    if fs == "buildable" and st != "buildable":
        return False
    if fs == "buildable_sets" and (st != "buildable" or item.get("looseComponent") is True):
        return False
    if fs == "not_ready" and st in ("claimable", "buildable"):
        return False
    mp = _num(f.get("minimumPlatinum")) or 0
    if mp > 0:
        p = item.get("platinum")
        if not isinstance(p, (int, float)) or p < mp:
            return False
    ma = _num(f.get("minimumAmount")) or 0
    if ma > 0:
        a = item.get("combinedAmount") if isinstance(item.get("combinedAmount"), (int, float)) else item.get("amount")
        if not isinstance(a, (int, float)) or a < ma:
            return False
    return True


def _metric(item: dict, sort_by: str) -> float | None:
    if sort_by in ("platinum", "ducats", "amount", "count", "time"):
        v = item.get(sort_by)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if sort_by == "owned":
        a = item.get("amount")
        return (1 if a > 0 else 0) if isinstance(a, (int, float)) else None
    if sort_by == "ducatonator":
        if isinstance(item.get("ducatonator"), (int, float)):
            return item["ducatonator"]
        d, p = item.get("ducats"), item.get("platinum")
        if isinstance(d, (int, float)) and isinstance(p, (int, float)) and p > 0:
            return d / p
        return None
    if sort_by == "complete_sets":
        c = item.get("completeSets")
        if isinstance(c, bool):
            return 1 if c else 0
        return c if isinstance(c, (int, float)) else None
    if sort_by == "missing_parts":
        v = item.get("missingParts")
        return v if isinstance(v, (int, float)) else None
    if sort_by == "parts_owned":
        v = item.get("ownedPartTypes")
        return v if isinstance(v, (int, float)) else None
    return None


def _name_key(s: str) -> str:
    return (s or "").casefold()


def sort_rows(rows: list[dict], sort_by: str, direction: str) -> list[dict]:
    """compareSharedFilterSort: name collation, else numeric with nulls last either way, ties by name."""
    rev = direction == "desc"
    if sort_by == "name":
        return sorted(rows, key=lambda r: _name_key(r.get("name", "")), reverse=rev)
    with_v, without = [], []
    for r in rows:
        m = _metric(r, sort_by)
        (without if m is None else with_v).append((m, r))
    by_name = sorted(with_v, key=lambda t: _name_key(t[1].get("name", "")))
    by_name.sort(key=lambda t: t[0], reverse=rev)  # stable: ties keep name order
    return [r for _, r in by_name] + sorted((r for _, r in without), key=lambda r: _name_key(r.get("name", "")))


# ---------------------------------------------------------------------------------------------
# inventoryMarket.ts: base rows (slug) + view rows (prices)
# ---------------------------------------------------------------------------------------------

TABS = ("everything", "all_parts", "relics", "mods", "arcanes", "full_sets", "equipment", "resources", "misc")
TAB_LABELS = {"everything": "Everything", "all_parts": "Prime parts", "relics": "Relics", "mods": "Mods",
              "arcanes": "Arcanes", "full_sets": "Full sets", "equipment": "Equipment", "resources": "Resources",
              "misc": "Misc"}
EVERYTHING_SOURCES = ("all_parts", "relics", "mods", "arcanes", "full_sets", "equipment", "resources", "misc")
SORTS_BY_TAB = {
    "all_parts": ("name", "platinum", "ducats", "amount", "ducatonator", "complete_sets", "missing_parts"),
    "full_sets": ("name", "platinum", "ducats", "amount", "ducatonator", "complete_sets", "missing_parts"),
    "resources": ("name", "amount"),
}
DEFAULT_SORTS = ("name", "platinum", "amount")

_PART_WORD_RE = (r"chassis|systems|neuroptics|helmet|barrel|receiver|stock|blade|handle|hilt|string|disc|grip|link|"
                 r"gauntlet|ornament|harness|carapace|cerebrum|pod|wings|fuselage|engines|avionics")


def lookup_name_candidates(name: str) -> list[str]:
    base = name.strip()
    out = [base]

    def add(v: str) -> None:
        if v and v not in out:
            out.append(v)

    if re.search(rf"\b({_PART_WORD_RE}) blueprint$", base, re.I):
        add(re.sub(r"\s+blueprint$", "", base, flags=re.I))
    elif re.search(rf"\b({_PART_WORD_RE})$", base, re.I):
        add(f"{base} Blueprint")
    if re.match(r"^zariman ship ", base, re.I):
        add(re.sub(r"^zariman ship ", "Parallax ", base, flags=re.I))
    if re.match(r"^prime archwing ", base, re.I):
        add(re.sub(r"^prime archwing ", "Odonata Prime ", base, flags=re.I))
    if re.search(r"\bbane of\b", base, re.I):
        add(re.sub(r"\bbane of\b", "Cleanse ", base, count=1, flags=re.I))
    if re.search(r"\bcleanse\b", base, re.I):
        add(re.sub(r"\bcleanse\b", "Bane of ", base, count=1, flags=re.I))
    if re.search(r"\borokin\b", base, re.I):
        add(re.sub(r"\borokin\b", "Corrupted", base, count=1, flags=re.I))
    if re.search(r"\bcorrupted\b", base, re.I):
        add(re.sub(r"\bcorrupted\b", "Orokin", base, count=1, flags=re.I))
    if re.search(r"\bhelmet blueprint$", base, re.I):
        add(re.sub(r"\bhelmet blueprint$", "Neuroptics Blueprint", base, flags=re.I))
        add(re.sub(r"\bhelmet blueprint$", "Neuroptics", base, flags=re.I))
    if re.search(r"\bneuroptics blueprint$", base, re.I):
        add(re.sub(r"\bneuroptics blueprint$", "Helmet Blueprint", base, flags=re.I))
        add(re.sub(r"\bneuroptics blueprint$", "Neuroptics", base, flags=re.I))
    for c in list(out):
        alias = re.sub(r"\s+", " ", re.sub(r"[-_–]+", " ", c)).strip()
        add(alias)
    return out


class MarketIndex:
    """warframe.market catalog lookups (empty when the catalog is not loaded)."""

    def __init__(self, catalog: list | None = None):
        self.by_ref: dict[str, Any] = {}
        self.by_name: dict[str, Any] = {}
        for c in catalog or []:
            if getattr(c, "game_ref", ""):
                self.by_ref.setdefault(c.game_ref.lower(), c)
            if getattr(c, "name", ""):
                self.by_name.setdefault(names_mod.normalize_for_search(c.name), c)
        self.refs = frozenset(self.by_ref)

    def __len__(self) -> int:
        return len(self.by_ref) + len(self.by_name)

    def by_game_ref(self, u: str):
        return self.by_ref.get((u or "").lower())

    def by_item_name(self, name: str):
        for cand in lookup_name_candidates(name):
            hit = self.by_name.get(names_mod.normalize_for_search(cand))
            if hit is not None:
                return hit
        return None


def _db_slug(u: str, db: Db) -> str | None:
    """The bundled slug of the item (or its Blueprint/Component alias); a "_set" slug
    belongs to the set row, never to one built copy."""
    for a in component_aliases(u):
        e = db.get(a)
        if e is not None and e.wfm_slug and not e.wfm_slug.endswith("_set"):
            return e.wfm_slug
    return None


def build_base_items(parsed: list[dict], db: Db, mi: MarketIndex, relic_db=None) -> list[dict]:
    """Every parsed row with its group, display name, market slug and clamped rank (all tabs)."""
    out = []
    for item in parsed:
        group = item.get("inventoryGroup") or "misc"
        u = item["internalName"]
        relic_quality = None
        display = item["name"]
        if group == "relics" and relic_db is not None:
            hit = relic_db.by_unique(u)
            if hit:
                relic, relic_quality = hit
                display = relic.name if relic.name.endswith(" Relic") else f"{relic.name} Relic"
        set_group = group in _AGGREGATE_GROUPS
        by_ref = mi.by_game_ref(u) if not set_group else None
        by_name = mi.by_item_name(display)
        ref_slug = getattr(by_ref, "slug", None)
        name_slug = getattr(by_name, "slug", None)
        if set_group:
            # resolveSnapshotSetSlug without the snapshot: the bundled set slug, else the catalog's
            # "<name> Set" listing; with no catalog only prime sets get a generated slug.
            slug = item.get("setSlug") or (name_slug if name_slug and name_slug.endswith("_set") else None)
            if slug is None:
                if len(mi) or not item.get("isPrime"):
                    continue
                gen = names_mod.normalize_for_slug(item["name"])
                slug = gen if not gen or gen.endswith("_set") else f"{gen}_set"
        else:
            slug = ref_slug or name_slug or _db_slug(u, db)
            if slug is None and group == "relics":
                slug = names_mod.normalize_for_slug(display)
            if slug is None and not is_ranked_group(group) and not (group == "all_parts" and item.get("tradable") is not True):
                slug = names_mod.normalize_for_slug(item["name"])
        if group == "all_parts" and not (ref_slug or name_slug or _db_slug(u, db)) and item.get("tradable") is not True:
            continue
        modular = bool(item.get("modularParts"))
        ranked = is_ranked_group(group)
        excluded_ranked = ranked and (display.lower() in ("blood for ammo", "blood for energy", "blood for life")
                                      or "veiled" in display.lower())
        can_index = not modular and (not ranked or (item.get("tradable") is True and not excluded_ranked))
        market_slug = slug if can_index else None
        lookup_max = getattr(by_ref, "max_rank", None) or getattr(by_name, "max_rank", None)
        max_rank = lookup_max if ranked and lookup_max else item.get("maxRank")
        cap = max_rank if isinstance(max_rank, int) and max_rank > 0 else (10 if group == "mods" else 5 if group == "arcanes" else 30)
        rank = max(0, min(int(item.get("rank") or 0), cap))
        visible = f"{display} ({relic_quality.capitalize()})" if relic_quality else display
        row = dict(item)
        row.update({"name": visible, "key": item.get("inventoryKey") or u, "rank": rank, "maxRank": max_rank,
                    "inventoryGroup": group, "partType": item.get("partType") or ("prime" if item.get("isPrime") else "normal"),
                    "amount": item["amount"] if isinstance(item.get("amount"), (int, float)) else 1,
                    "favorite": bool(item.get("favorite")), "equipped": bool(item.get("equipped")),
                    "completeSets": item.get("completeSets"), "marketSlug": market_slug, "subtype": relic_quality})
        out.append(row)
    return out


def resource_base_rows(resources: list[dict]) -> list[dict]:
    return [{"name": r["name"], "internalName": r["internalName"], "key": "res:" + r["internalName"],
             "category": "resources", "categoryLabel": "Resource", "inventoryGroup": "resources",
             "amount": r["count"], "count": r["count"], "image": r.get("image"), "tradable": False,
             "isPrime": False, "partType": "normal", "marketSlug": None, "keywords": ["resource"], "rank": 0,
             "maxRank": 0} for r in resources]


def price_rank(row: dict) -> int | None:
    if not is_ranked_group(row.get("inventoryGroup")):
        return None
    mx = row.get("maxRank") or (10 if row.get("inventoryGroup") == "mods" else 5)
    return mx if (row.get("rank") or 0) >= mx else 0


def attach_prices(rows: list[dict], price_of: Callable[[str, int | None], float | None]) -> list[dict]:
    """buildInventoryViewItems: platinum (cache only), ducats, ducatonator, equippedSummary."""
    out = []
    for r in rows:
        slug = r.get("marketSlug")
        plat = price_of(slug, price_rank(r)) if slug else None
        d = _num(r.get("ducats"))
        dn = round(d / plat, 2) if d is not None and plat is not None and plat > 0 else None
        eq = r.get("equippedIn") or []
        summary = f"Equipped in {', '.join(eq[:2])}{' +' if len(eq) > 2 else ''}" if eq else None
        out.append({**r, "platinum": plat, "ducats": d if d is None else int(d), "ducatonator": dn,
                    "equippedSummary": summary})
    return out


def market_price_of(market=None) -> tuple[Callable[[str, int | None], float | None], list[tuple[str, int | None]]]:
    """Price lookup over the shared market cache (any age, no network) + the misses it saw."""
    from .data import market as market_mod

    m = market or market_mod.get()
    missing: list[tuple[str, int | None]] = []
    memo: dict[tuple[str, int | None], float | None] = {}

    def price_of(slug: str, rank: int | None) -> float | None:
        k = (slug, rank)
        if k in memo:
            return memo[k]
        p = None
        # rank-specific only (WFHelper platinumR0 / platinumRmax): a ranked row never borrows
        # the rankless price, so it stays "missing" and gets queued with its own rank
        hit, pr = m.cached(slug, rank, max_age=float("inf"))
        if hit and pr is not None:
            v = pr.median if pr.median else (float(pr.low_sell) if pr.low_sell else None)
            if v:
                p = float(v)
        if p is None:
            missing.append(k)
        memo[k] = p
        return p

    return price_of, missing


def matches_tab(row: dict, tab: str, hidden_sources: Iterable[str] = ()) -> bool:
    g = row.get("inventoryGroup") or "misc"
    if tab == "everything":
        return g != "incomplete_sets" and g not in set(hidden_sources)
    return g == tab


# public row shape --------------------------------------------------------------------------

_ROW_KEYS = ("key", "name", "internalName", "category", "categoryLabel", "inventoryGroup", "rank", "maxRank",
             "amount", "combinedAmount", "isPrime", "vaulted", "tradable", "favorite", "equipped", "leveledUp",
             "marketSlug", "subtype", "platinum", "ducats", "ducatonator", "equippedSummary", "completeSets",
             "missingParts", "ownedPartTypes", "totalPartTypes", "masteryReq", "image")


def public_row(r: dict) -> dict:
    return {k: r.get(k) for k in _ROW_KEYS}


# ---------------------------------------------------------------------------------------------
# foundry entries (FoundryView.svelte)
# ---------------------------------------------------------------------------------------------

STATUS_RANK = {"claimable": 0, "in-progress": 1, "ready-to-build": 2, "not-ready": 3}
FOUNDRY_STATE_BY_STATUS = {"claimable": "claimable", "in-progress": "building", "ready-to-build": "buildable",
                           "not-ready": "missing"}
FOUNDRY_SORTS = ("count", "time", "name")
PINNED_MAX = 60


def foundry_entries(foundry: dict, owned: dict, db: Db, now_ms: int, product_owned: dict | None = None) -> list[dict]:
    chain = chain_buildable_blueprints(foundry["recipes"], owned, db)
    rows = []
    for b in foundry["building"]:
        end = b.get("endMs")
        status = "claimable" if end is not None and end <= now_ms else "in-progress"
        rows.append({**b, "source": "building", "count": None, "buildTime": 0, "status": status,
                     "isIngredient": False, "time": max(end - now_ms, 0) if end is not None else None})
    for r in foundry["recipes"]:
        status = "ready-to-build" if is_foundry_recipe_ready(r, owned, chain) else "not-ready"
        rows.append({**r, "source": "blueprint", "endMs": None, "status": status, "time": None,
                     "chainOnly": status == "ready-to-build" and not all(
                         owned.get(i["uniqueName"], 0) >= i["count"] for i in r["ingredients"])})
    for row in rows:
        pu = row.get("productUniqueName")
        pe = db.get(pu) if pu else None
        row["category"] = (row.get("category") or "").strip() or "Misc"
        row["isPrime"] = bool((pe and pe.is_prime) or re.search(r"\bprime\b", row["name"], re.I))
        row["vaulted"] = bool(pe and pe.vaulted)
        row["foundryState"] = FOUNDRY_STATE_BY_STATUS[row["status"]]
        row["looseComponent"] = bool(pe and pe.component_of)
        row["keywords"] = [db.name_of(i["uniqueName"]).lower() for i in row.get("ingredients") or []]
        row["productOwned"] = int((product_owned or {}).get(pu, 0)) if pu else 0
        row["key"] = f"{row['source']}:{row.get('uniqueName') or row['name']}"
        row["ingredients"] = [{**i, "name": db.name_of(i["uniqueName"]), "owned": int(owned.get(i["uniqueName"], 0))}
                              for i in row.get("ingredients") or []]
    return rows


def passes_foundry_tab(row: dict, tab: str) -> bool:
    if tab == "all":
        return True
    if tab == "status:in-progress":
        return row["status"] in ("in-progress", "claimable")
    if tab.startswith("cat:"):
        return row["category"] == tab[4:]
    return True


def sort_foundry(rows: list[dict], sort_by: str, direction: str, pinned: set[str]) -> list[dict]:
    rows = sort_rows(rows, sort_by, direction)
    return sorted(rows, key=lambda r: (0 if r["source"] == "blueprint" and r.get("uniqueName") in pinned else 1,
                                       STATUS_RANK[r["status"]]))


def public_foundry_row(r: dict) -> dict:
    keys = ("key", "source", "name", "uniqueName", "productUniqueName", "category", "status", "count", "endMs",
            "time", "buildPrice", "buildTime", "ingredients", "isIngredient", "isPrime", "vaulted", "chainOnly",
            "productOwned", "image")
    return {k: r.get(k) for k in keys}


# ---------------------------------------------------------------------------------------------
# cached snapshot (one per inventory sync)
# ---------------------------------------------------------------------------------------------

@dataclass
class Snapshot:
    key: tuple
    parsed: list[dict]
    base: list[dict]
    resources: list[dict]
    foundry: dict
    owned: dict
    product_owned: dict
    db: Db


_snap_lock = threading.Lock()
_snap: Snapshot | None = None
_db_cache: tuple[Any, Any, Db] | None = None


def reset_caches() -> None:
    global _snap, _db_cache
    with _snap_lock:
        _snap = None
        _db_cache = None


def get_db() -> Db:
    global _db_cache
    from .data.items import items

    idb, ext = items(), extra()
    with _snap_lock:
        if _db_cache is not None and _db_cache[0] is idb and _db_cache[1] is ext:
            return _db_cache[2]
    db = Db(idb, ext)
    with _snap_lock:
        _db_cache = (idb, ext, db)
    return db


def usable_inventory(data: dict, db: Db, hide_claims: bool = True) -> dict:
    from .inventory.model import without_foundry_pending

    if not hide_claims:
        return data

    def reusable(u: str) -> bool:
        r = db.extra.recipes.get(u)
        return bool(r and r.reusable)
    return without_foundry_pending(data, reusable)


def snapshot(inv, market_index: MarketIndex | None = None, hide_claims: bool = True, relic_db=None) -> Snapshot:
    """Parsed rows, base rows, resources, foundry for `inv`, cached by its sync time."""
    global _snap
    from .inventory.model import aggregate_component_ownership

    db = get_db()
    mi = market_index or MarketIndex()
    key = (inv.sync_time, inv.fetched_at, inv.origin, id(db), len(mi), bool(hide_claims), id(relic_db))
    with _snap_lock:
        if _snap is not None and _snap.key == key:
            return _snap
    data = inv.data if isinstance(inv.data, dict) else {}
    usable = usable_inventory(data, db, hide_claims)
    parsed = parse_inventory(usable, db, mi.refs)
    base = build_base_items(parsed, db, mi, relic_db)
    resources = parse_resources(usable, db)
    owned = aggregate_component_ownership(usable_inventory(data, db, True))
    foundry = parse_foundry(usable, db)  # stores/data.ts: foundryData is built from usableInventory
    product_owned: dict[str, int] = {}
    for p in parsed:
        a = p.get("amount") or 0
        if a > 0:
            product_owned[p["internalName"]] = product_owned.get(p["internalName"], 0) + a
    snap = Snapshot(key, parsed, base + resource_base_rows(resources), resources, foundry, owned, product_owned, db)
    with _snap_lock:
        _snap = snap
    return snap


def _main(argv: list[str]) -> int:
    """python -m wfh.inventory_view build <warframe-public-export-plus package dir> <out dir>"""
    if len(argv) < 3 or argv[0] != "build":
        print(_main.__doc__)
        return 2
    print(json.dumps(build_extra(argv[1], argv[2]), indent=1))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
