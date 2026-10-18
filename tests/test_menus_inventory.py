"""Inventory + Foundry view-models (wfh/inventory_view.py, menus/inventory.py, menus/foundry.py).

Runs on the bundled item data (data/items.json.gz + data/foundry/foundry.json.gz) and a DE-shaped
inventory with real uniqueNames; prices come from a fake cache, never the network.
"""

import asyncio
import copy
import json
import os

import pytest

from wfh import inventory_view as iv
from wfh import rpc
from wfh.data import items as items_mod
from wfh.inventory.model import Inventory

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = "/Lotus/Types/Recipes/WarframeRecipes/"
W = "/Lotus/Types/Recipes/Weapons/WeaponParts/"
M = "/Lotus/Types/Items/MiscItems/"
FORMA_BP = "/Lotus/Types/Recipes/Components/FormaBlueprint"
NOW = 1_800_000_000_000
FUTURE = NOW + 3_600_000

INV = {
    "Suits": [{"ItemType": "/Lotus/Powersuits/Mag/MagPrime", "XP": 1_600_000, "ItemId": {"$oid": "a1"},
               "Configs": [{"Upgrades": ["m1"]}]}],
    "LongGuns": [{"ItemType": "/Lotus/Weapons/Tenno/Rifle/BratonPrime", "XP": 0, "ItemId": {"$oid": "a2"}}],
    "Melee": [{"ItemType": "/Lotus/Weapons/Tenno/Melee/LongSword/LongSword", "XP": 450_000, "Favorite": True}],
    "MiscItems": [
        {"ItemType": M + "Ferrite", "ItemCount": 20000}, {"ItemType": M + "Rubedo", "ItemCount": 5000},
        {"ItemType": M + "ArgonCrystal", "ItemCount": 5}, {"ItemType": M + "NeuralSensor", "ItemCount": 4},
        {"ItemType": M + "OrokinCell", "ItemCount": 30}, {"ItemType": M + "Forma", "ItemCount": 4},
        {"ItemType": P + "MesaPrimeChassisComponent", "ItemCount": 1},
        {"ItemType": W + "BratonPrimeBarrel", "ItemCount": 2}, {"ItemType": W + "BratonPrimeReceiver", "ItemCount": 1},
        {"ItemType": W + "BratonPrimeStock", "ItemCount": 1},
        {"ItemType": "/Lotus/Types/Game/Projections/T1VoidProjectionGGold", "ItemCount": 2},
    ],
    "Recipes": [
        {"ItemType": P + "MesaPrimeBlueprint", "ItemCount": 1}, {"ItemType": P + "MesaPrimeHelmetBlueprint", "ItemCount": 2},
        {"ItemType": P + "MesaPrimeSystemsBlueprint", "ItemCount": 1},
        {"ItemType": "/Lotus/Types/Recipes/Weapons/BratonPrimeBlueprint", "ItemCount": 1},
        {"ItemType": FORMA_BP, "ItemCount": 3},
    ],
    "PendingRecipes": [
        {"ItemType": FORMA_BP, "CompletionDate": {"$date": {"$numberLong": str(NOW - 1000)}}},
        {"ItemType": P + "MesaPrimeHelmetBlueprint", "CompletionDate": {"$date": {"$numberLong": str(FUTURE)}}},
    ],
    "Upgrades": [{"ItemType": "/Lotus/Upgrades/Mods/Warframe/AvatarShieldMaxMod",
                  "UpgradeFingerprint": "{\"lvl\":10}", "ItemId": {"$oid": "m1"}}],
    "RawUpgrades": [{"ItemType": "/Lotus/Upgrades/Mods/Warframe/AvatarShieldMaxMod", "ItemCount": 3}],
    "Arcanes": [], "LevelKeys": [],
    "LastInventorySync": {"$oid": "68f1a2b3c4d5e6f708192a3b"},
}

PRICES = {"mesa_prime_chassis_blueprint": 12, "braton_prime_barrel": 4, "braton_prime_receiver": 3,
          "mesa_prime_neuroptics_blueprint": 25, "mesa_prime_set": 140, "redirection": 8, "lith_a1_relic": 6}


def fake_price_of(slug, rank):
    if slug == "redirection":
        return 30.0 if rank == 10 else 8.0
    return PRICES.get(slug)


@pytest.fixture(scope="module", autouse=True)
def bundled_data():
    items_mod.set_data_dirs(os.path.join(ROOT, "data"), None)
    iv.set_extra_path(os.path.join(ROOT, "data", "foundry", iv.EXTRA_FILE))
    yield
    iv.set_extra_path(None)


def inventory(data=None, fetched=1.0):
    return Inventory(copy.deepcopy(data or INV), "file", fetched_at=fetched)


@pytest.fixture
def snap():
    from wfh.data.relics import relics

    return iv.snapshot(inventory(), iv.MarketIndex(), True, relics())


def by_name(rows, name):
    hits = [r for r in rows if r["name"] == name]
    assert hits, f"{name} not in {[r['name'] for r in rows]}"
    return hits


# ---- data bundle ----------------------------------------------------------------------------

def test_foundry_bundle_loaded():
    ext = iv.extra()
    assert len(ext.recipes) > 1500
    r = ext.recipes[P + "MesaPrimeBlueprint"]
    assert r.result == "/Lotus/Powersuits/Cowgirl/MesaPrime" and r.build_price == 25000
    assert (P + "MesaPrimeHelmetComponent", 1) in r.ingredients
    assert ext.max_rank["/Lotus/Upgrades/Mods/Warframe/AvatarShieldMaxMod"] == 10
    assert ext.category[M + "Ferrite"] == "Resource"
    assert any(x.reusable for x in ext.recipes.values())


def test_build_extra_roundtrip(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "ExportRecipes.json").write_text(json.dumps({
        "/Bp": {"resultType": "/Prod", "buildPrice": 100, "buildTime": 60, "num": 2, "consumeOnUse": False,
                "ingredients": [{"ItemType": "/Res", "ItemCount": 3}]}}))
    (pkg / "ExportResources.json").write_text(json.dumps({"/Res": {"name": "/L/Res"}}))
    (pkg / "dict.en.json").write_text(json.dumps({"/L/Res": "<RES> Thing"}))
    (pkg / "package.json").write_text(json.dumps({"version": "9.9.9"}))
    out = iv.build_extra(str(pkg), str(tmp_path / "out"))
    assert out["recipes"] == 1 and out["version"] == "9.9.9"
    import gzip

    ext = iv.Extra(json.load(gzip.open(tmp_path / "out" / iv.EXTRA_FILE)))
    assert ext.recipes["/Bp"].reusable and ext.recipes["/Bp"].num == 2
    assert ext.names["/Res"] == "Thing" and ext.category["/Res"] == "Resource"


# ---- parseInventory -------------------------------------------------------------------------

def test_parse_groups_ranks_and_flags(snap):
    rows = snap.base
    groups = {r["name"]: r["inventoryGroup"] for r in rows}
    assert groups["Mesa Prime Chassis"] == "all_parts"
    assert groups["Braton Prime Barrel"] == "all_parts"
    assert groups["Mesa Prime Neuroptics Blueprint"] == "all_parts"
    assert groups["Mag Prime"] == "equipment" and groups["Skana"] == "equipment"
    assert groups["Lith A1 Relic (Flawless)"] == "relics"
    assert groups["Ferrite"] == "resources" and groups["Forma"] == "resources"
    # foundry-pending copy hidden: 2 held - 1 building
    assert by_name(rows, "Mesa Prime Neuroptics Blueprint")[0]["amount"] == 1
    # mods split per rank; fingerprint rank, config-equipped instance, combined amount
    red = sorted(by_name(rows, "Redirection"), key=lambda r: r["rank"])
    assert [(r["rank"], r["amount"], r["maxRank"]) for r in red] == [(0, 3, 10), (10, 1, 10)]
    assert red[1]["equipped"] is True and red[0]["equipped"] is False
    assert {r["combinedAmount"] for r in red} == {4}
    skana = by_name(rows, "Skana")[0]
    assert skana["favorite"] is True and skana["rank"] == 30  # 450k XP / 6000, capped
    assert by_name(rows, "Lith A1 Relic (Flawless)")[0]["marketSlug"] == "lith_a1_relic"
    assert by_name(rows, "Lith A1 Relic (Flawless)")[0]["subtype"] == "flawless"
    # a built warframe never takes its set's slug
    assert by_name(rows, "Mag Prime")[0]["marketSlug"] != "mag_prime_set"


def test_full_and_incomplete_sets(snap):
    sets = {r["name"]: r for r in snap.base if r["inventoryGroup"] in ("full_sets", "incomplete_sets")}
    assert sets["Mesa Prime Set"]["inventoryGroup"] == "full_sets"
    assert sets["Mesa Prime Set"]["completeSets"] == 1
    assert sets["Mesa Prime Set"]["marketSlug"] == "mesa_prime_set"
    assert sets["Braton Prime Set"]["inventoryGroup"] == "full_sets"
    data = copy.deepcopy(INV)
    data["Recipes"] = [r for r in data["Recipes"] if "MesaPrimeSystems" not in r["ItemType"]]
    from wfh.data.relics import relics

    s2 = iv.snapshot(inventory(data, fetched=2.0), iv.MarketIndex(), True, relics())
    mesa = by_name(s2.base, "Mesa Prime Set")[0]
    assert mesa["inventoryGroup"] == "incomplete_sets"
    assert (mesa["missingParts"], mesa["ownedPartTypes"], mesa["totalPartTypes"]) == (1, 3, 4)


def test_snapshot_cached_by_sync(snap):
    from wfh.data.relics import relics

    again = iv.snapshot(inventory(), iv.MarketIndex(), True, relics())
    assert again is snap
    newer = iv.snapshot(inventory(fetched=5.0), iv.MarketIndex(), True, relics())
    assert newer is not snap


def test_resources_sorted_by_count(snap):
    names = [r["name"] for r in snap.resources]
    assert names[:2] == ["Ferrite", "Rubedo"]
    assert "Mesa Prime Chassis" not in names and "Lith A1 Flawless" not in names


def test_market_catalog_demotes_unlisted_recipe_rows():
    from wfh.data.market import CatalogItem
    from wfh.data.relics import relics

    cat = [CatalogItem("mesa_prime_neuroptics_blueprint", "Mesa Prime Neuroptics Blueprint", P + "MesaPrimeHelmetBlueprint",
                       max_rank=None),
           CatalogItem("redirection", "Redirection", "/Lotus/Upgrades/Mods/Warframe/AvatarShieldMaxMod", max_rank=10)]
    s = iv.snapshot(inventory(fetched=7.0), iv.MarketIndex(cat), True, relics())
    groups = {r["name"]: r["inventoryGroup"] for r in s.base}
    # with a catalog, a recipe path WFM does not list is not a sellable part
    assert groups["Mesa Prime Systems Blueprint"] == "misc"
    assert groups["Mesa Prime Neuroptics Blueprint"] == "all_parts"


# ---- value totals / filters / sort ------------------------------------------------------------

def test_value_totals_verbatim():
    rows = [
        {"inventoryGroup": "all_parts", "isPrime": True, "marketSlug": "a", "platinum": 10, "ducats": 45, "amount": 2},
        {"inventoryGroup": "all_parts", "isPrime": True, "marketSlug": "b", "platinum": None, "ducats": 15, "amount": 1},
        {"inventoryGroup": "all_parts", "isPrime": True, "marketSlug": "c", "platinum": 3, "ducats": None, "amount": 0.5},
        {"inventoryGroup": "full_sets", "isPrime": True, "marketSlug": "s_set", "platinum": 100, "amount": 1},
        {"inventoryGroup": "mods", "isPrime": False, "marketSlug": "m", "platinum": 7, "amount": 3},
        {"inventoryGroup": "all_parts", "isPrime": True, "marketSlug": None, "platinum": 50, "amount": 1},
        {"inventoryGroup": "all_parts", "isPrime": True, "tradable": False, "marketSlug": "x", "platinum": 50},
    ]
    t = iv.compute_value_totals(rows, "prime")
    assert t == {"platinum": 23, "ducats": 105, "platinumUnpriced": 1, "ducatsUnpriced": 1, "unpriced": 2,
                 "counted": 3}
    t5 = iv.compute_value_totals(rows, "prime", 5)  # the 3p row drops, unpriced stays
    assert (t5["platinum"], t5["counted"], t5["unpriced"]) == (20, 2, 1)
    tt = iv.compute_value_totals(rows, "tradable")
    assert tt["platinum"] == 44 and tt["counted"] == 4
    assert iv.select_value_strip_rows(t5, t) == ["inView", "inventory"]
    assert iv.select_value_strip_rows(t, t) == ["inView"]
    zero = iv.compute_value_totals([], "prime")
    assert iv.select_value_strip_rows(zero, t) == ["inventory"]
    assert iv.select_value_strip_rows(zero, zero) == []


def test_sort_nulls_last_both_directions():
    rows = [{"name": "b", "platinum": 5}, {"name": "a", "platinum": None}, {"name": "c", "platinum": 9},
            {"name": "a2", "platinum": 5}]
    assert [r["name"] for r in iv.sort_rows(rows, "platinum", "desc")] == ["c", "a2", "b", "a"]
    assert [r["name"] for r in iv.sort_rows(rows, "platinum", "asc")] == ["a2", "b", "c", "a"]
    assert [r["name"] for r in iv.sort_rows(rows, "name", "asc")] == ["a", "a2", "b", "c"]
    assert iv.default_sort_direction("ducatonator") == "desc" and iv.default_sort_direction("missing_parts") == "asc"


def test_shared_filters():
    f = dict(iv.DEFAULT_FILTERS)
    row = {"name": "Redirection", "amount": 1, "combinedAmount": 4, "isPrime": False, "keywords": ["rawupgrades"]}
    assert iv.matches_shared_filters(row, {**f, "minimumAmount": 2})  # combinedAmount wins
    assert not iv.matches_shared_filters(row, {**f, "minimumPlatinum": 1})  # unpriced drops
    assert iv.matches_shared_filters(row, {**f, "search": "RAWUP"})
    assert not iv.matches_shared_filters(row, {**f, "partType": "prime"})
    assert iv.matches_shared_filters(row, {**f, "favorite": "no"}) and not iv.matches_shared_filters(row, {**f, "favorite": "yes"})
    fr = {"name": "x", "foundryState": "buildable", "looseComponent": True}
    assert iv.matches_shared_filters(fr, {**f, "foundryState": "buildable"})
    assert not iv.matches_shared_filters(fr, {**f, "foundryState": "buildable_sets"})
    assert not iv.matches_shared_filters(fr, {**f, "foundryState": "not_ready"})


def test_lookup_name_candidates():
    c = iv.lookup_name_candidates("Mesa Prime Helmet Blueprint")
    assert "Mesa Prime Neuroptics Blueprint" in c and "Mesa Prime Helmet" in c
    assert "Odonata Prime Wings" in iv.lookup_name_candidates("Prime Archwing Wings")


# ---- inventory page view ----------------------------------------------------------------------

def test_inventory_view_tabs_value_and_paging(snap):
    from wfh.menus import inventory as menu

    v = menu.build_view(snap, fake_price_of, tab="all_parts", sort="platinum", limit=3)
    assert v["hasInventory"] and v["sort"] == "platinum" and v["direction"] == "desc"
    assert [r["name"] for r in v["rows"]][:2] == ["Mesa Prime Neuroptics Blueprint", "Mesa Prime Chassis"]
    assert v["total"] > 3 and len(v["rows"]) == 3
    tabs = {t["id"]: t["count"] for t in v["tabs"]}
    assert tabs["mods"] == 2 and tabs["resources"] == 6 and tabs["full_sets"] == 2
    assert tabs["everything"] == sum(tabs[t] for t in iv.EVERYTHING_SOURCES if t != "full_sets")
    val = v["value"]
    # prime parts: chassis 12 + barrel 4x2 + receiver 3 + neuroptics bp 25 (+ unpriced rows)
    assert val["inView"]["platinum"] == 48
    assert val["rows"] == ["inView"]
    assert val["inView"]["ducats"] > 0
    nxt = menu.build_view(snap, fake_price_of, tab="all_parts", sort="platinum", offset=3, limit=3)
    assert not {r["key"] for r in nxt["rows"]} & {r["key"] for r in v["rows"]}
    # a sort the tab does not offer falls back to name
    assert menu.build_view(snap, fake_price_of, tab="resources", sort="platinum")["sort"] == "name"
    res = menu.build_view(snap, fake_price_of, tab="resources", sort="amount")
    assert res["rows"][0]["name"] == "Ferrite"


def test_inventory_view_mods_use_rank_price(snap):
    from wfh.menus import inventory as menu

    v = menu.build_view(snap, fake_price_of, tab="mods", sort="platinum")
    assert [(r["rank"], r["platinum"]) for r in v["rows"]] == [(10, 30.0), (0, 8.0)]
    tradable = menu.build_view(snap, fake_price_of, tab="mods", scope="tradable")
    assert tradable["value"]["inView"]["platinum"] == 30 + 8 * 3


def test_inventory_view_everything_and_sets(snap):
    from wfh.menus import inventory as menu

    every = menu.build_view(snap, fake_price_of, tab="everything", limit=120)
    groups = {r["inventoryGroup"] for r in every["rows"]}
    assert "full_sets" not in groups and "resources" in groups
    every2 = menu.build_view(snap, fake_price_of, tab="everything", everything_hidden=["resources"], limit=120)
    assert "resources" not in {r["inventoryGroup"] for r in every2["rows"]}
    assert "full_sets" in {r["inventoryGroup"] for r in every2["rows"]}
    sets = menu.build_view(snap, fake_price_of, tab="full_sets")
    assert {c["id"] for c in sets["setCategories"]} == {"Warframe", "Primary"}
    only_frames = menu.build_view(snap, fake_price_of, tab="full_sets", full_sets_hidden=["Primary"])
    assert [r["name"] for r in only_frames["rows"]] == ["Mesa Prime Set"]
    # filtered view narrower than the whole inventory -> both value rows
    narrowed = menu.build_view(snap, fake_price_of, tab="all_parts", search="braton")
    assert narrowed["value"]["rows"] == ["inView", "inventory"]


def test_inventory_item_detail(snap):
    from wfh.menus import inventory as menu

    s = menu.build_item(snap, fake_price_of, "/Lotus/Powersuits/Cowgirl/MesaPrime#set")
    assert s["platinum"] == 140 and len(s["components"]) == 4
    assert all(c["owned"] >= c["needed"] for c in s["components"])
    part = menu.build_item(snap, fake_price_of, W + "BratonPrimeBarrel")
    assert part["parentSet"]["name"] == "Braton Prime Set"
    bp = menu.build_item(snap, fake_price_of, P + "MesaPrimeHelmetBlueprint")
    assert bp["recipe"]["product"] == "Mesa Prime Neuroptics" and bp["recipe"]["credits"] == 15000
    assert menu.build_item(snap, fake_price_of, "nope") is None


def test_inventory_qam(snap):
    from wfh.menus import inventory as menu

    p = {"valueScope": "prime", "valueMinPlat": 5, "hideFoundryClaims": True, "autoPrefetch": True}
    q = menu.qam_view(snap, fake_price_of, p)
    assert q["hasInventory"] and q["totals"]["platinum"] == 12 + 25  # 4p and 3p parts under the floor
    assert q["top"][0]["name"] == "Mesa Prime Neuroptics Blueprint"
    assert menu.qam_view(None, fake_price_of, p)["hasInventory"] is False


class FakeCache:
    """Market.cached stand-in: {(slug, rank): age_seconds}."""

    def __init__(self, ages):
        self.ages = ages

    def cached(self, slug, rank=None, max_age=30 * 60):
        a = self.ages.get((slug, rank))
        return (True, object()) if a is not None and a < max_age else (False, None)


def test_prefetch_slugs(snap):
    from wfh.menus import inventory as menu

    got = menu.prefetch_slugs(snap, "prime", market=FakeCache({}))
    assert "mesa_prime_chassis_blueprint" in got[None]
    assert all(not s.endswith("_set") for s in got[None])
    mods = menu.prefetch_slugs(snap, "mods", market=FakeCache({}))
    assert set(mods) == {0, 10}


def test_prefetch_slugs_refreshes_stale_prices(snap):
    # a price that exists but is old is queued again; a fresh one is not (Market.prefetch dedups)
    from wfh.menus import inventory as menu

    cache = FakeCache({("mesa_prime_chassis_blueprint", None): 10, ("braton_prime_barrel", None): 3 * 86400})
    got = menu.prefetch_slugs(snap, "prime", market=cache)[None]
    assert "mesa_prime_chassis_blueprint" not in got and "braton_prime_barrel" in got
    cache.ages[("mesa_prime_chassis_blueprint", None)] = 3600  # past PRICE_TTL, inside the auto window
    assert "mesa_prime_chassis_blueprint" in menu.prefetch_slugs(snap, "prime", market=cache)[None]
    auto = menu.prefetch_slugs(snap, "prime", max_age=menu.AUTO_PREFETCH_MAX_AGE, market=cache)[None]
    assert "mesa_prime_chassis_blueprint" not in auto and "braton_prime_barrel" in auto


def test_market_price_of_is_rank_specific():
    from wfh.data.market import Price

    class M:
        def cached(self, slug, rank=None, max_age=0):
            if (slug, rank) == ("redirection", None):
                return True, Price(median=8, low_sell=8, volume=1, fetched_at=0)
            if (slug, rank) == ("redirection", 0):
                return True, Price(median=5, low_sell=5, volume=1, fetched_at=0)
            return False, None

    price_of, missing = iv.market_price_of(M())
    assert price_of("redirection", 0) == 5
    assert price_of("redirection", 10) is None  # no R10 price: missing, never the rankless 8
    assert price_of("redirection", None) == 8
    assert missing == [("redirection", 10)]


def test_market_catalog_if_loaded():
    from wfh.data import market

    m = market.Market.__new__(market.Market)
    m._catalog = None
    assert m.catalog_if_loaded() is None
    m._catalog = []
    assert m.catalog_if_loaded() == []


# ---- foundry --------------------------------------------------------------------------------

def test_foundry_statuses_and_chain(snap):
    rows = iv.foundry_entries(snap.foundry, snap.owned, snap.db, NOW, snap.product_owned)
    st = {(r["source"], r["name"]): r["status"] for r in rows}
    assert st[("building", "Forma")] == "claimable"
    assert st[("building", "Mesa Prime Neuroptics")] == "in-progress"
    assert st[("blueprint", "Mesa Prime Neuroptics")] == "ready-to-build"
    assert st[("blueprint", "Braton Prime")] == "ready-to-build"
    assert st[("blueprint", "Mesa Prime Systems")] == "not-ready"
    assert st[("blueprint", "Mesa Prime")] == "not-ready"  # systems resources missing
    # give the Systems resources: Mesa Prime becomes chain-buildable (parts from owned blueprints)
    data = copy.deepcopy(INV)
    for u in ("Morphic", "ControlModule", "Circuits", "Salvage"):
        data["MiscItems"].append({"ItemType": M + u, "ItemCount": 10_000})
    from wfh.data.relics import relics

    s2 = iv.snapshot(inventory(data, fetched=9.0), iv.MarketIndex(), True, relics())
    rows2 = iv.foundry_entries(s2.foundry, s2.owned, s2.db, NOW, s2.product_owned)
    mesa = next(r for r in rows2 if r["name"] == "Mesa Prime" and r["source"] == "blueprint")
    assert mesa["status"] == "ready-to-build" and mesa["chainOnly"] is True
    chassis = next(i for i in mesa["ingredients"] if i["name"] == "Mesa Prime Chassis")
    assert chassis["owned"] == 1 and chassis["count"] == 1


def _bp_counts(s):
    return {r["uniqueName"]: r["count"] for r in s.foundry["recipes"]}


@pytest.mark.parametrize("hide_claims", [True, False])
def test_foundry_blueprints_exclude_pending(hide_claims):
    # stores/data.ts builds foundryData from usableInventory: copies already in the foundry are not
    # spare blueprints (Forma 3 - 1 pending, Neuroptics 2 - 1 pending)
    from wfh.data.relics import relics

    s = iv.snapshot(inventory(fetched=21.0 + hide_claims), iv.MarketIndex(), hide_claims, relics())
    c = _bp_counts(s)
    if hide_claims:
        assert c[FORMA_BP] == 2 and c[P + "MesaPrimeHelmetBlueprint"] == 1
        assert len(s.foundry["building"]) == 2  # the pending rows themselves are unchanged
    else:
        assert c[FORMA_BP] == 3 and c[P + "MesaPrimeHelmetBlueprint"] == 2


def test_foundry_only_copy_building_is_not_buildable():
    # building your only Neuroptics blueprint: it shows as in progress, never also as a BUILD row
    from wfh.data.relics import relics
    from wfh.menus import foundry as fm

    data = copy.deepcopy(INV)
    for r in data["Recipes"]:
        if r["ItemType"] == P + "MesaPrimeHelmetBlueprint":
            r["ItemCount"] = 1
    s = iv.snapshot(inventory(data, fetched=23.0), iv.MarketIndex(), True, relics())
    rows = iv.foundry_entries(s.foundry, s.owned, s.db, NOW, s.product_owned)
    neuro = [r for r in rows if r["name"] == "Mesa Prime Neuroptics"]
    assert [r["source"] for r in neuro] == ["building"]
    assert fm.build_view(s, NOW, [])["counts"]["ready"] == 1  # Braton Prime only


def test_chain_needs_blueprint_copies(snap):
    # Mesa Prime needs one Neuroptics; with zero spare Neuroptics blueprints the chain fails.
    data = copy.deepcopy(INV)
    for u in ("Morphic", "ControlModule", "Circuits", "Salvage"):
        data["MiscItems"].append({"ItemType": M + u, "ItemCount": 10_000})
    data["Recipes"] = [r for r in data["Recipes"] if "MesaPrimeHelmet" not in r["ItemType"]]
    from wfh.data.relics import relics

    s = iv.snapshot(inventory(data, fetched=11.0), iv.MarketIndex(), True, relics())
    rows = iv.foundry_entries(s.foundry, s.owned, s.db, NOW, s.product_owned)
    mesa = next(r for r in rows if r["name"] == "Mesa Prime" and r["source"] == "blueprint")
    assert mesa["status"] == "not-ready"


def test_pinned_totals():
    entries = [
        {"source": "blueprint", "uniqueName": "/a", "buildPrice": 100, "ingredients": [{"uniqueName": "/x", "count": 2}]},
        {"source": "blueprint", "uniqueName": "/a", "buildPrice": 100, "ingredients": [{"uniqueName": "/x", "count": 2}]},
        {"source": "blueprint", "uniqueName": "/b", "buildPrice": 50,
         "ingredients": [{"uniqueName": "/x", "count": 1}, {"uniqueName": "/y", "count": 5}]},
        {"source": "building", "uniqueName": "/b", "buildPrice": 999, "ingredients": []},
    ]
    t = iv.compute_pinned_totals(entries, {"/a", "/b"}, {"/x": 3, "/y": 9}.get)
    assert t["count"] == 2 and t["credits"] == 150
    assert t["resources"] == [{"uniqueName": "/y", "needed": 5, "owned": 9}, {"uniqueName": "/x", "needed": 3, "owned": 3}]
    assert t["missing"] == []


def test_foundry_view_and_qam(snap):
    from wfh.menus import foundry as fm

    pinned = [P + "MesaPrimeSystemsBlueprint"]
    v = fm.build_view(snap, NOW, pinned)
    assert v["rows"][0]["name"] == "Mesa Prime Systems" and v["rows"][0]["pinned"]
    assert [r["status"] for r in v["rows"][1:3]] == ["claimable", "in-progress"]
    assert v["counts"] == {"claimable": 1, "building": 1, "ready": 2, "missing": 3}
    tabs = {t["id"]: t["count"] for t in v["tabs"]}
    assert tabs["status:in-progress"] == 2 and tabs["cat:Warframe"] == 4 and tabs["cat:Primary"] == 1
    assert v["pinnedTotals"]["credits"] == 15000
    assert {m["name"] for m in v["pinnedTotals"]["missing"]} >= {"Morphics", "Salvage"}
    w = fm.build_view(snap, NOW, [], tab="cat:Primary")
    assert [r["name"] for r in w["rows"]] == ["Braton Prime"]
    b = fm.build_view(snap, NOW, [], state="buildable_sets")
    assert {r["name"] for r in b["rows"]} == {"Braton Prime"}  # the Neuroptics bp is a loose part
    s = fm.build_view(snap, NOW, [], search="argon")  # matches ingredient names
    assert {r["name"] for r in s["rows"]} == {"Mesa Prime Neuroptics"}
    t = fm.build_view(snap, NOW, [], tab="status:in-progress", sort="time")
    assert [r["status"] for r in t["rows"]] == ["claimable", "in-progress"]
    q = fm.qam_view(snap, NOW, pinned, limit=3)
    assert [r["status"] for r in q["rows"]] == ["claimable", "in-progress", "ready-to-build"]
    assert q["nextReadyMs"] == FUTURE and q["counts"]["ready"] == 2
    assert fm.build_view(None, NOW, [])["hasInventory"] is False


# ---- RPCs through the worker ------------------------------------------------------------------

def test_rpcs(app, monkeypatch):
    from wfh import inventory as inv_mod
    from wfh.menus import inventory as menu

    monkeypatch.setattr(menu, "market_index", lambda: iv.MarketIndex())
    monkeypatch.setattr(iv, "market_price_of", lambda market=None: (fake_price_of, []))
    monkeypatch.setattr(inv_mod, "inventory", lambda: None)
    empty = asyncio.run(rpc.call_local("inventory.view", {}))
    assert empty["hasInventory"] is False and empty["home"] and "status" in empty
    assert asyncio.run(rpc.call_local("foundry.view", {}))["hasInventory"] is False
    assert asyncio.run(rpc.call_local("inventory.qam", {}))["hasInventory"] is False

    the_inv = inventory(fetched=42.0)
    monkeypatch.setattr(inv_mod, "inventory", lambda: the_inv)
    v = asyncio.run(rpc.call_local("inventory.view", {"tab": "all_parts", "sort": "platinum", "limit": 5}))
    assert v["hasInventory"] and v["rows"][0]["platinum"] == 25 and v["inventory"]["origin"] == "file"
    item = asyncio.run(rpc.call_local("inventory.item", {"key": v["rows"][0]["key"]}))
    assert item["name"] == v["rows"][0]["name"]
    p = asyncio.run(rpc.call_local("inventory.ui_set", {"value_scope": "tradable", "value_min_plat": 7}))
    assert p["valueScope"] == "tradable" and p["valueMinPlat"] == 0  # 7 is not a preset
    p = asyncio.run(rpc.call_local("inventory.ui_set", {"value_scope": "prime", "value_min_plat": 10}))
    q = asyncio.run(rpc.call_local("inventory.qam", {}))
    assert q["minPlat"] == 10 and q["totals"]["platinum"] == 37
    asyncio.run(rpc.call_local("inventory.ui_set", {"value_min_plat": 0}))

    from wfh.menus import foundry as fm

    monkeypatch.setattr(fm, "_now", lambda: NOW)  # fixture dates are relative to NOW
    pin = asyncio.run(rpc.call_local("foundry.pin", {"unique_name": FORMA_BP, "pinned": True}))
    assert pin["pinned"] == [FORMA_BP]
    fv = asyncio.run(rpc.call_local("foundry.view", {"state": "all"}))
    assert fv["rows"][0]["pinned"] and fv["rows"][0]["name"] == "Forma"
    fq = asyncio.run(rpc.call_local("foundry.qam", {"limit": 4}))
    assert fq["counts"] == {"claimable": 1, "building": 1, "ready": 2, "missing": 3}
    assert fq["pinnedTotals"]["count"] == 1
    assert asyncio.run(rpc.call_local("foundry.pin", {"unique_name": FORMA_BP, "pinned": False}))["pinned"] == []
