"""Mastery, Syndicates and Missions view-models (menus/mastery.py, menus/syndicates.py, menus/missions.py).

Everything runs on small synthetic export tables / inventories so the tests do not need
the built data/ bundle; one test also checks the real bundle when scripts/fetch-data.sh
has built it.
"""

import asyncio
import gzip
import json
import os
from dataclasses import dataclass, field

import pytest

from wfh import rpc
from wfh.menus import mastery as m
from wfh.menus import missions as mi
from wfh.menus import syndicates as sy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- synthetic export tables ------------------------------------------------------------------

LEX = "/Lotus/Weapons/Tenno/Pistols/PrimeLex/PrimeLex"
BRATON = "/Lotus/Weapons/Tenno/Rifle/Rifle"
BRAMMA = "/Lotus/Weapons/Grineer/Bows/GrnBow/GrnBowWeapon"
ASH = "/Lotus/Powersuits/Ninja/Ninja"
ASHP = "/Lotus/Powersuits/Ninja/AshPrime"
PHANTASMA = "/Lotus/Weapons/Tenno/LongGuns/RevenantShotgun/RevenantShotgun"
EXALT = "/Lotus/Powersuits/Excalibur/ExcaliburBlade"
SKANA = "/Lotus/Weapons/Tenno/Melee/LongSword/LongSword"
SKANA_DUP = "/Lotus/Weapons/Tenno/Melee/LongSword/LongSwordDuplicate"
ZAW_STRIKE = "/Lotus/Weapons/Ostron/Melee/ModularMelee01/Tip/TipOne"

LEX_BP = "/Lotus/Types/Recipes/Weapons/LexPrimeBlueprint"
LEX_BARREL = "/Lotus/Types/Recipes/Weapons/WeaponParts/LexPrimeBarrel"
LEX_RECEIVER = "/Lotus/Types/Recipes/Weapons/WeaponParts/LexPrimeReceiver"
ASHP_BP = "/Lotus/Types/Recipes/WarframeRecipes/AshPrimeBlueprint"
ASHP_CHASSIS = "/Lotus/Types/Recipes/WarframeRecipes/AshPrimeChassisComponent"
ASHP_SYS = "/Lotus/Types/Recipes/WarframeRecipes/AshPrimeSystemsComponent"
BRATON_BP = "/Lotus/Types/Recipes/Weapons/BratonBlueprint"
PHANTASMA_BP = "/Lotus/Types/Recipes/Weapons/PhantasmaBlueprint"
PHANTASMA_BARREL = "/Lotus/Types/Recipes/Weapons/WeaponParts/PhantasmaBarrel"
CELL = "/Lotus/Types/Items/MiscItems/OrokinCell"

DICT = {"/L/Lex": "Lex Prime", "/L/Braton": "Braton", "/L/Bramma": "Kuva Bramma", "/L/Ash": "Ash",
        "/L/AshP": "Ash Prime", "/L/Phant": "Phantasma", "/L/Exalt": "Exalted Blade", "/L/Skana": "Skana",
        "/L/Balla": "Balla", "/L/Arch": "<ARCHWING> Odonata"}
PEP = {
    "ExportWarframes": {ASH: {"name": "/L/Ash", "productCategory": "Suits", "masteryReq": 0},
                        ASHP: {"name": "/L/AshP", "productCategory": "Suits", "masteryReq": 0},
                        "/Lotus/Powersuits/Archwing/StandardJetPack/StandardJetPack":
                            {"name": "/L/Arch", "productCategory": "SpaceSuits"}},
    "ExportWeapons": {LEX: {"name": "/L/Lex", "productCategory": "Pistols", "masteryReq": 8},
                      BRATON: {"name": "/L/Braton", "productCategory": "LongGuns"},
                      BRAMMA: {"name": "/L/Bramma", "productCategory": "LongGuns", "maxLevelCap": 40,
                               "masteryReq": 15},
                      PHANTASMA: {"name": "/L/Phant", "productCategory": "LongGuns", "masteryReq": 9},
                      EXALT: {"name": "/L/Exalt", "productCategory": "SpecialItems"},
                      SKANA: {"name": "/L/Skana", "productCategory": "Melee"},
                      SKANA_DUP: {"name": "/L/Skana", "productCategory": "Melee"},
                      ZAW_STRIKE: {"name": "/L/Balla", "productCategory": "Pistols"}},
    "ExportSentinels": {},
}
WFCD = {
    # Ash's `exalted` is a list of its exalted weapons: only `true` excludes (WFHelper `=== true`).
    "Warframes": [{"uniqueName": ASH, "name": "Ash", "masterable": True, "exalted": ["/x"]},
                  {"uniqueName": ASHP, "name": "Ash Prime", "masterable": True, "vaulted": True, "isPrime": True,
                   "components": [{"uniqueName": ASHP_BP}, {"uniqueName": ASHP_CHASSIS}, {"uniqueName": ASHP_SYS}]}],
    "Primary": [{"uniqueName": BRATON, "name": "Braton", "masterable": True,
                 "components": [{"uniqueName": BRATON_BP}]},
                {"uniqueName": PHANTASMA, "name": "Phantasma", "masterable": True,
                 "components": [{"uniqueName": PHANTASMA_BP}, {"uniqueName": PHANTASMA_BARREL}]},
                {"uniqueName": "/Lotus/Weapons/Tenno/LongGuns/Quest/QuestVersions/Bow", "name": "Quest Bow",
                 "masterable": True}],
    "Secondary": [{"uniqueName": LEX, "name": "Lex Prime", "masterable": True, "isPrime": True,
                   "components": [{"uniqueName": LEX_BARREL}, {"uniqueName": LEX_BP}, {"uniqueName": CELL},
                                  {"uniqueName": LEX_RECEIVER}]}],
    "Melee": [{"uniqueName": EXALT, "name": "Exalted Blade", "masterable": True, "exalted": True}],
    "Misc": [{"uniqueName": "/Lotus/Types/Game/Thing", "name": "Thing", "masterable": False}],
}
RECIPES = {
    LEX_BP: {"resultType": LEX, "ingredients": [], "excludeFromMarket": True},
    BRATON_BP: {"resultType": BRATON, "ingredients": [], "creditsCost": 25000},
    PHANTASMA_BP: {"resultType": PHANTASMA, "ingredients": [], "creditsCost": 25000},
    ASHP_BP: {"resultType": ASHP, "ingredients": [], "excludeFromMarket": True},
    "/Lotus/Types/Recipes/WarframeRecipes/AshPrimeChassisBlueprint": {"resultType": ASHP_CHASSIS, "ingredients": []},
}
REGIONS = {"SolNode1": {"missionType": "MT_DEFENSE", "masteryExp": 24},
           "SolNode2": {"missionType": "MT_JUNCTION", "masteryExp": 0},
           "SolNode3": {"missionType": "MT_SURVIVAL", "masteryExp": 0}}


def payload():
    return m.build_payload(PEP, DICT, WFCD, "test", recipes=RECIPES, regions=REGIONS)


@pytest.fixture
def md():
    return m.MasteryData(payload())


# ---- fake item db / inventory --------------------------------------------------------------------

@dataclass
class Comp:
    unique_name: str
    name: str
    item_count: int = 1
    tradable: bool = False
    ducats: int | None = None


@dataclass
class Item:
    unique_name: str
    name: str
    wfm_slug: str | None = None
    components: list = field(default_factory=list)
    tradable: bool = False
    ducats: int | None = None
    vaulted: bool | None = None


class FakeDb:
    def __init__(self, rows):
        self.rows = {r.unique_name: r for r in rows}

    def by_unique(self, u):
        return self.rows.get(u)


DB = FakeDb([
    Item(LEX, "Lex Prime", "lex_prime_set", [Comp(LEX_BARREL, "Lex Prime Barrel", tradable=True),
                                             Comp(LEX_BP, "Lex Prime Blueprint", tradable=True),
                                             Comp(CELL, "Orokin Cell", 10),
                                             Comp(LEX_RECEIVER, "Lex Prime Receiver", tradable=True)]),
    Item(LEX_BARREL, "Lex Prime Barrel", "lex_prime_barrel", tradable=True, ducats=15),
    Item(LEX_RECEIVER, "Lex Prime Receiver", "lex_prime_receiver", tradable=True, ducats=45),
    Item(LEX_BP, "Lex Prime Blueprint", "lex_prime_blueprint", tradable=True, ducats=15),
    Item(ASHP, "Ash Prime", "ash_prime_set", [Comp(ASHP_BP, "Ash Prime Blueprint"),
                                              Comp(ASHP_CHASSIS, "Ash Prime Chassis"),
                                              Comp(ASHP_SYS, "Ash Prime Systems")]),
    Item(BRATON, "Braton", None, [Comp(BRATON_BP, "Blueprint")]),
    Item(PHANTASMA, "Phantasma", None, [Comp(PHANTASMA_BP, "Phantasma Blueprint"),
                                        Comp(PHANTASMA_BARREL, "Phantasma Barrel")]),
    Item("/Lotus/Types/Items/MiscItems/Neurode", "Neurodes"),
    Item("/Lotus/Types/Game/Projections/T1VoidProjectionGaussPrimeDBronze", "Lith G1 Intact", "lith_g1_relic",
         tradable=True),
])
PRICES = {"lex_prime_set": 20.0, "lex_prime_barrel": 4.0, "lex_prime_receiver": 6.0, "ash_prime_set": 80.0,
          "lith_g1_relic": 3.0}


def oid(ms):
    return {"$oid": f"{ms // 1000:08x}" + "0" * 16}


def de_date(ms):
    return {"$date": {"$numberLong": str(ms)}}


NOW_MS = 1_760_000_000_000


def inventory():
    return {
        "PlayerLevel": 2, "RegularCredits": 50000, "FusionPoints": 100,
        "Suits": [{"ItemType": ASH, "XP": 900_000, "ItemId": {"$oid": "a1"}}],             # suit: rank 30
        "LongGuns": [{"ItemType": BRAMMA, "XP": 450_000 * 2, "Polarized": 2, "ItemId": {"$oid": "a2"}}],
        "Pistols": [],
        "Melee": [{"ItemType": SKANA, "XP": 500 * 25, "ItemId": {"$oid": "a3"}}],          # rank 5
        "XPInfo": [{"ItemType": BRATON, "XP": 450_000},                                      # sold, mastered
                   {"ItemType": SKANA, "XP": 500 * 400}],                                   # forma'd: banked 20
        "Recipes": [{"ItemType": LEX_BP, "ItemCount": 1}, {"ItemType": PHANTASMA_BP, "ItemCount": 0}],
        "MiscItems": [{"ItemType": LEX_BARREL, "ItemCount": 1}, {"ItemType": LEX_RECEIVER, "ItemCount": 1},
                      {"ItemType": CELL, "ItemCount": 20}],
        "PendingRecipes": [{"ItemType": ASHP_BP, "CompletionDate": de_date(NOW_MS - 1000)}],
        "Missions": [{"Tag": "SolNode1", "Completes": 3, "Tier": 1}, {"Tag": "SolNode2", "Completes": 1},
                     {"Tag": "SolNode3", "Completes": 0}],
        "PlayerSkills": {"LPS_PILOTING": 10, "LPS_DRIFT_COMBAT": 3, "LPP_SPACE": 5},
    }


def service(inv=None, md_=None, relics=None, relic_db=None):
    md_ = md_ or m.MasteryData(payload())
    return m.MasteryService(None, inventory=lambda: inv if inv is not None else inventory(), md=lambda: md_,
                            item_db=lambda: DB, price_of=PRICES.get,
                            owned_relics=lambda: relics or {}, relic_db=lambda: relic_db, now_ms=lambda: NOW_MS)


# ---- mastery ---------------------------------------------------------------------------------------

def test_rank_xp_roundtrip():
    assert m.rank_to_xp(1) == 2500 and m.rank_to_xp(30) == 2_250_000 and m.rank_to_xp(32) == 2_545_000
    assert m.xp_to_rank(2_249_999) == 29 and m.xp_to_rank(2_250_000) == 30
    assert m.xp_to_rank(2_250_000 + 147_500 * 3 + 1) == 33
    for r in range(0, 40):
        assert m.xp_to_rank(m.rank_to_xp(r)) == r
    assert m.mr_label(33) == "L3"


def test_build_payload_filters_and_fields(md):
    names = {r["n"]: r for r in md.items}
    assert "Ash" in names and "Ash Prime" in names                  # exalted list does not exclude
    assert "Exalted Blade" not in names                             # SpecialItems / exalted
    assert "Quest Bow" not in names and "Thing" not in names        # QuestVersions path, masterable false
    assert [r["n"] for r in md.items].count("Skana") == 1           # deduped by name
    assert names["Odonata"]["cat"] == "Archwing" and names["Odonata"]["rate"] == 200
    assert names["Kuva Bramma"]["max"] == 40 and names["Braton"]["max"] == 30
    assert names["Balla"]["cat"] == "Melee"                         # zaw strike, not Pistols
    assert names["Lex Prime"]["cat"] == "Secondary" and names["Lex Prime"]["req"] == 8
    assert names["Phantasma"].get("market") == 1 and names["Phantasma"]["credits"] == 25000
    assert "market" not in names["Lex Prime"]
    assert names["Ash Prime"].get("vaulted") == 1 and names["Ash"]["rate"] == 200
    assert "Plexus" in names                                         # synthetic
    assert md.blueprints[LEX_BP] == LEX and md.blueprints[ASHP_BP] == ASHP
    assert md.regions == {"SolNode1": [24, 0], "SolNode2": [1000, 1], "SolNode3": [0, 0]}


def test_compute_progress_ranks_and_xp(md):
    prog = m.compute_progress(inventory(), md)
    by = {i["name"]: i for i in prog["items"]}
    assert by["Ash"]["status"] == "mastered" and by["Ash"]["masteryXp"] == 6000
    # Kuva: 2 forma unlock rank 34 of 40; XP 900k at 500/rank^2 -> rank 42 capped at 34.
    assert by["Kuva Bramma"]["rank"] == 34 and by["Kuva Bramma"]["maxRank"] == 40
    assert by["Kuva Bramma"]["status"] == "progress" and by["Kuva Bramma"]["masteryXpRemaining"] == 600
    assert by["Braton"]["status"] == "mastered" and not by["Braton"]["currentlyOwned"]
    # Skana: owned at rank 5, XPInfo banks rank 20 (forma'd).
    assert by["Skana"]["rank"] == 5 and by["Skana"]["masteryXp"] == 2000
    assert by["Lex Prime"]["status"] == "missing" and by["Lex Prime"]["masteryXpRemaining"] == 3000
    st = prog["stats"]
    assert st["mastered"] == 2 and st["inProgress"] == 2 and st["total"] == len(prog["items"])
    gear = sum(i["masteryXp"] for i in prog["items"])
    # Node: 24 normal + 24 steel; junction 1000; intrinsics: (10 + 3) * 1500.
    assert st["profileMastery"]["totalXp"] == gear + 48 + 1000 + 13 * 1500
    assert st["byCategory"]["Primary"]["mastered"] == 1
    comp = st["completion"]
    assert comp["starChart"]["normal"] == {"done": 1, "total": 1}
    assert comp["starChart"]["junctions"] == {"done": 1, "total": 1}
    assert comp["starChart"]["steelPath"]["done"] == 1
    assert comp["intrinsics"]["railjack"] == {"done": 10, "total": 10}


def test_profile_mastery_states():
    pm = m.profile_mastery({"PlayerLevel": 2}, 20_000)
    assert pm["xpForNext"] == 12_500 and pm["xpIntoRank"] == 10_000 and pm["percentToNext"] == 80.0
    assert m.profile_mastery({"PlayerLevel": 2}, 30_000)["testReady"] is True
    assert m.profile_mastery({"PlayerLevel": 10}, 1000)["percentToNext"] is None
    s = m.summary({"stats": {"total": 4, "mastered": 1, "inProgress": 1, "missing": 2, "byCategory": {},
                             "completion": {}, "profileMastery": pm}}, ready_xp=3000)
    assert s["mrLine"] == "2,500 XP to MR 3"
    assert s["projection"]["projectedRank"] == 3 and "Foundry raises you to MR 3" in s["projection"]["text"]


def test_gilding_and_modular_part(md):
    inv = {"LongGuns": [], "OperatorAmps": [{"ItemType": "/Lotus/Weapons/Sentients/OperatorAmplifiers/Set1/Barrel/X",
                                             "XP": 10_000_000, "ModularParts": [ZAW_STRIKE]}],
           "Melee": [{"ItemType": "/Lotus/Weapons/Ostron/Melee/PlayerMeleeWeapon", "XP": 10_000_000,
                      "ModularParts": [ZAW_STRIKE], "Features": 8}]}
    by = {i["name"]: i for i in m.compute_progress(inv, md)["items"]}
    # The gilded build wins over the ungilded amp build for the same part.
    assert by["Balla"]["status"] == "mastered" and not by["Balla"].get("needsGilding")


def test_roadmap_access_and_lists(md):
    svc = service()
    rm = svc.roadmap()
    easy = {r["name"]: r for r in rm["easy"]}
    assert easy["Kuva Bramma"]["access"] == "owned"            # level it
    assert easy["Skana"]["access"] == "owned"
    assert easy["Ash Prime"]["access"] == "claimable"           # PendingRecipes done
    assert easy["Lex Prime"]["access"] == "buildable"           # every part + blueprint held
    assert "Phantasma" not in easy                              # barrel missing too
    order = [r["access"] for r in rm["easy"]]
    assert order == sorted(order, key=lambda a: m.ACCESS_PRIORITY[a])
    q = svc.qam()
    assert q["summary"]["mrLine"] and len(q["easy"]) <= 5
    assert q["summary"]["projection"]["readyXp"] == 6000       # Ash Prime claimable


def test_market_blueprint_and_platinum():
    inv = inventory()
    inv["MiscItems"].append({"ItemType": PHANTASMA_BARREL, "ItemCount": 1})
    inv["Recipes"] = [r for r in inv["Recipes"] if r["ItemType"] != LEX_BP]
    inv["PendingRecipes"] = []
    svc = service(inv)
    easy = {r["name"]: r for r in svc.roadmap()["easy"]}
    assert easy["Phantasma"]["access"] == "marketBlueprint"
    rm = svc.roadmap()
    plat = {r["name"]: r for r in rm["platinum"]}
    # Lex Prime misses only the blueprint: no price for it -> no estimate (issue #47 rule).
    assert "Lex Prime" not in plat
    PRICES["lex_prime_blueprint"] = 5.0
    try:
        svc.invalidate()
        plat = {r["name"]: r for r in svc.roadmap()["platinum"]}
        assert plat["Lex Prime"]["estimatedCost"] == 5.0 and plat["Lex Prime"]["xpPerPlatinum"] == 600
        assert "Ash Prime" not in plat                          # unpriced parts: no estimate
    finally:
        PRICES.pop("lex_prime_blueprint")


def test_estimate_purchase_cost():
    parts = [{"missing": 1, "name": "A"}, {"missing": 2, "name": "B"}, {"missing": 0, "name": "C"}]
    assert m.estimate_purchase_cost(30, parts, lambda p: {"A": 5, "B": 4}[p["name"]]) == 13
    assert m.estimate_purchase_cost(10, parts, lambda p: {"A": 5, "B": 4}[p["name"]]) == 10
    assert m.estimate_purchase_cost(10, parts, lambda p: None) is None
    assert m.estimate_purchase_cost(10, [{"missing": 0, "name": "C"}], lambda p: 1) is None
    assert m.estimate_purchase_cost(7, [], lambda p: 1) == 7


def test_relic_completion_probability():
    item = {"name": "Lex Prime", "parts": [{"uniqueName": LEX_BARREL, "name": "Lex Prime Barrel", "missing": 1}]}
    pool = [(LEX_BARREL, "Lex Prime Barrel", 10.0), ("/x", "Forma Blueprint", 25.33)]
    assert m.relic_completion(item, [(1, pool)])[0] == pytest.approx(0.1)
    p, n = m.relic_completion(item, [(2, pool)])
    assert p == pytest.approx(1 - 0.9 ** 2) and n == 2
    # Matched by name when the reward has no uniqueName.
    assert m.relic_completion(item, [(1, [("", "Lex Prime Barrel", 50.0)])])[0] == pytest.approx(0.5)
    # A part no owned relic drops makes the item unreachable.
    item2 = {"name": "Lex Prime", "parts": item["parts"] + [{"uniqueName": LEX_RECEIVER, "name": "Lex Prime Receiver",
                                                             "missing": 1}]}
    assert m.relic_completion(item2, [(5, pool)]) is None
    # Two parts from one relic: one reward per copy.
    pool2 = [(LEX_BARREL, "Lex Prime Barrel", 50.0), (LEX_RECEIVER, "Lex Prime Receiver", 50.0)]
    assert m.relic_completion(item2, [(1, pool2)]) is None or m.relic_completion(item2, [(1, pool2)])[0] == 0
    assert m.relic_completion(item2, [(2, pool2)])[0] == pytest.approx(0.5)


def test_items_paging_and_pins(md):
    svc = service()
    out = svc.items(category="Primary", status="all", sort="mastery_xp", limit=2)
    assert out["matched"] == 3 and len(out["rows"]) == 2
    assert out["rows"][0]["masteryXpRemaining"] >= out["rows"][1]["masteryXpRemaining"]
    det = svc.item(LEX)
    states = {p["name"]: p["state"] for p in det["parts"]}
    assert states == {"Lex Prime Barrel": "owned", "Lex Prime Blueprint": "owned", "Lex Prime Receiver": "owned"}
    ash = svc.item(ASHP)
    assert {p["state"] for p in ash["parts"]} == {"missing"}


def test_real_bundle_when_built():
    path = os.path.join(ROOT, "data", m.DATA_FILE)
    if not os.path.exists(path):
        pytest.skip("data/mastery.json.gz not built (scripts/fetch-data.sh)")
    with gzip.open(path) as f:
        md = m.MasteryData(json.load(f))
    names = {r["n"]: r for r in md.items}
    assert len(md.items) > 700
    assert names["Kuva Bramma"]["max"] == 40 and names["Ash"]["cat"] == "Warframes"
    assert names["Lex Prime"]["cat"] == "Secondary" and "Exalted Blade" not in names
    assert len(md.regions) > 300


# ---- syndicates ------------------------------------------------------------------------------------

def table():
    return sy.load_table()


def syn_inv():
    return {"PlayerLevel": 20, "RegularCredits": 120_000, "DailyAffiliation": 8000, "DailyAffiliationCetus": 26000,
            "Affiliations": [{"Tag": "SteelMeridianSyndicate", "Title": 2, "Standing": 30_000, "Initiated": True},
                             {"Tag": "CetusSyndicate", "Title": 1, "Standing": 3000},
                             {"Tag": "RedVeilSyndicate", "Title": -1, "Standing": -10_000}],
            "MiscItems": []}


def meta(tag):
    return next(t for t in table() if t["tag"] == tag)


def test_syndicate_status_and_cap():
    inv = syn_inv()
    assert sy.daily_cap(inv) == 26_000 and sy.daily_cap(None) == 16_000
    sm = sy.syndicate_status(inv, meta("SteelMeridianSyndicate"))
    assert sm["level"] == 2 and sm["nextLevel"] == 3 and sm["dailyRemaining"] == 8000
    thr3 = sy.rank_threshold(meta("SteelMeridianSyndicate"), 3)
    assert sm["standingToNext"] == thr3 - 30_000 and 0 <= sm["progress"] <= 1
    rv = sy.syndicate_status(inv, meta("RedVeilSyndicate"))
    assert rv["level"] == -1 and rv["nextLevel"] == 0
    assert sy.rank_threshold(meta("RedVeilSyndicate"), 0) == -5000
    nl = sy.syndicate_status(inv, meta("NewLokaSyndicate"))
    assert nl["level"] == 0 and nl["joined"] is False


def test_plan_steps_and_needs():
    inv = syn_inv()
    nl = meta("NewLokaSyndicate")
    steps = sy.plan_steps(inv, nl, 2)
    assert [s["level"] for s in steps] == [1, 2]
    assert steps[0]["initiation"] is True and steps[0]["credits"] >= nl["initiation"]["credits"]
    assert steps[0]["standingNeeded"] == sy.rank_threshold(nl, 1)
    assert steps[1]["standingNeeded"] == sy.rank_threshold(nl, 2) - sy.rank_threshold(nl, 1)
    rv = sy.plan_steps(inv, meta("RedVeilSyndicate"), 0)
    assert rv[0]["level"] == 0 and rv[0]["standingNeeded"] == 5000 and rv[0]["credits"] > 0
    plans = [{"meta": nl, "targetLevel": 2, "steps": steps},
             {"meta": meta("SteelMeridianSyndicate"), "targetLevel": 3,
              "steps": sy.plan_steps(inv, meta("SteelMeridianSyndicate"), 3)}]
    needs = sy.aggregate_needs(plans, inv, lambda u: 0)
    pool = needs["standing"][0]
    assert pool["bin"] == "NORMAL" and pool["tags"] == ["NewLokaSyndicate", "SteelMeridianSyndicate"]
    assert pool["remainingToday"] == 8000 and pool["dailyCap"] == 26_000
    import math as _m
    assert pool["daysEstimate"] == _m.ceil(max(0, pool["needed"] - 8000) / 26_000)
    assert needs["credits"]["owned"] == 120_000
    conflicts = sy.alignment_conflicts([nl, meta("SteelMeridianSyndicate"), meta("RedVeilSyndicate")])
    pairs = {frozenset((c["a"], c["b"])) for c in conflicts}
    assert frozenset(("NewLokaSyndicate", "SteelMeridianSyndicate")) in pairs or conflicts


def test_syndicate_view_and_qam():
    inv = syn_inv()
    goals = sy.normalize_goals([["NewLokaSyndicate", 2], ["Nope", 3], ["CetusSyndicate", "x"]], table())
    assert goals == {"NewLokaSyndicate": 2}
    v = sy.view_model(inv, table(), goals)
    assert [g["kind"] for g in v["groups"]] == ["normal", "openWorld", "other"]
    assert v["plans"][0]["tag"] == "NewLokaSyndicate" and v["needs"]["standing"]
    normal = next(p for p in v["pools"] if p["bin"] == "NORMAL")
    assert normal["remaining"] == 8000 and len(normal["tags"]) == 6
    q = sy.qam_model(inv, table(), goals, limit=2)
    assert [r["tag"] for r in q["rows"]] == ["SteelMeridianSyndicate", "CetusSyndicate"] and q["more"] == 1
    assert {p["bin"] for p in q["pools"]} == {"NORMAL", "CETUS"}


# ---- missions ---------------------------------------------------------------------------------------

def mission_inv(sync_s, credits=1000, endo=10, neurodes=0, relic=0, gear=()):
    inv = {"LastInventorySync": {"$oid": f"{sync_s:08x}" + "0" * 16}, "RegularCredits": credits,
           "FusionPoints": endo,
           "MiscItems": [{"ItemType": "/Lotus/Types/Items/MiscItems/Neurode", "ItemCount": neurodes}],
           "LevelKeys": [], "Recipes": [],
           "PendingRecipes": [{"ItemType": "/x/Foundry", "ItemId": {"$oid": "p1"}}],
           "Melee": [{"ItemType": g, "ItemId": {"$oid": f"g{i}"}} for i, g in enumerate(gear)]}
    if relic:
        inv["MiscItems"].append({"ItemType": "/Lotus/Types/Game/Projections/T1VoidProjectionGaussPrimeDBronze",
                                 "ItemCount": relic})
    return inv


def test_diff_gains_only():
    a = mi.snapshot(mission_inv(100, credits=5000, endo=50, neurodes=3))
    b = mi.snapshot(mission_inv(200, credits=4000, endo=80, neurodes=5, relic=1, gear=["/w/Skana"]))
    d = mi.diff(a, b)
    assert d["credits"] == 0 and d["endo"] == 30
    assert {i["uniqueName"]: i["count"] for i in d["items"]} == {
        "/Lotus/Types/Items/MiscItems/Neurode": 2,
        "/Lotus/Types/Game/Projections/T1VoidProjectionGaussPrimeDBronze": 1, "/w/Skana": 1}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_tracker_records_fresh_reads(tmp_path):
    clock = Clock(1_760_000_000.0)
    seen = []
    t = mi.MissionTracker(str(tmp_path), clock=clock, line_source=lambda: mi.REALTIME, on_record=seen.append)
    t.on_inventory(mission_inv(1_759_999_000, credits=1000, neurodes=1))
    assert t.baseline is not None
    t.on_line("1.0 Sys [Info]: SyncAutoPopulatedConsumables for mission MT_CAPTURE with location SolNode10")
    t.on_line("2.0 Game [Info]: OnStateStarted, mission type=MT_CAPTURE")
    t.on_line("3.0 Sys [Info]: EOM missionLocationUnlocked=1")
    t.on_line("4.0 Script [Info]: TopMenu.lua: Abort: something")          # same mission, joined
    assert t.pending["count"] == 1
    # A copy synced long before the end is not fresh.
    assert t.on_inventory(mission_inv(1_759_999_100, credits=1200)) is None and t.pending["count"] == 1
    clock.t += 4
    s = t.on_inventory(mission_inv(1_759_999_995, credits=13_400, endo=330, neurodes=3, relic=1))
    assert s is not None and seen == [s]
    assert s["missionType"] == "MT_CAPTURE" and s["node"] == "SolNode10" and s["missionCount"] == 1
    assert s["credits"] == 12_400 and s["endo"] == 320 and len(s["items"]) == 2
    # Persisted.
    t2 = mi.MissionTracker(str(tmp_path))
    assert len(t2.history) == 1 and t2.history[0]["id"] == s["id"]


def test_tracker_hub_and_carry(tmp_path):
    clock = Clock(1_760_000_000.0)
    t = mi.MissionTracker(str(tmp_path), clock=clock, line_source=lambda: mi.REALTIME)
    t.on_inventory(mission_inv(1_759_990_000))
    t.on_line("TopMenu.lua: Abort: in hub")
    assert t.pending["count"] == 0
    t.on_line("Sys [Info]: SyncAutoPopulatedConsumables for mission MT_LANDSCAPE with location CetusHub4")
    t.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert t.pending["count"] == 0                               # leaving Cetus
    t.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    clock.t += 60
    t.on_line("Sys [Info]: EOM missionLocationUnlocked=1")       # a second mission, not read in between
    assert t.pending["count"] == 2
    s = t.on_inventory(mission_inv(int(clock.t), neurodes=4))
    assert s["missionCount"] == 2 and "missionType" not in s


def test_tracker_disabled_and_rewind(tmp_path):
    clock = Clock(1_760_000_000.0)
    on = {"v": False}
    t = mi.MissionTracker(str(tmp_path), enabled=lambda: on["v"], clock=clock, line_source=lambda: mi.REALTIME)
    t.on_inventory(mission_inv(1_759_990_000))
    t.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert t.baseline is None and t.pending["count"] == 0
    on["v"] = True
    t.on_inventory(mission_inv(1_759_990_000, neurodes=1))
    # A regular load with the rewards lands before the (late) end line ...
    clock.t += 10
    t.on_inventory(mission_inv(1_760_000_005, neurodes=5))
    t.on_mission_end("Sys [Info]: EOM missionLocationUnlocked=1", ended_at=1_760_000_000.0)
    # ... and the end rewinds to the pre-mission baseline, so the next fresh read still sees the gain.
    clock.t += 5
    s = t.on_inventory(mission_inv(1_760_000_012, neurodes=5))
    assert s is not None and s["items"] == [{"uniqueName": "/Lotus/Types/Items/MiscItems/Neurode", "count": 4}]


def test_mission_page_rows_and_totals(tmp_path):
    val = mi.Valuer(DB.by_unique, PRICES.get)
    now = NOW_MS
    hist = [
        {"id": "a", "endedAt": now - 3600_000, "readAt": now, "missionCount": 1, "missionType": "MT_CAPTURE",
         "node": "SolNode10", "credits": 12_400, "endo": 320,
         "items": [{"uniqueName": "/Lotus/Types/Game/Projections/T1VoidProjectionGaussPrimeDBronze", "count": 2},
                   {"uniqueName": LEX_BARREL, "count": 1}, {"uniqueName": LEX_RECEIVER, "count": 1},
                   {"uniqueName": "/Lotus/Types/Items/MiscItems/Neurode", "count": 2}]},
        {"id": "b", "endedAt": now - 10 * mi.DAY_MS, "readAt": now, "missionCount": 2, "credits": 5,
         "endo": 0, "items": []},
    ]
    PRICES_BEFORE = dict(PRICES)
    PRICES.pop("lex_prime_receiver")
    try:
        pg = mi.page(hist, val, "7d", now_ms=now)
    finally:
        PRICES.clear()
        PRICES.update(PRICES_BEFORE)
    assert pg["matched"] == 1 and pg["recorded"] == 2
    s = pg["summaries"][0]
    rows = s["rows"]
    assert [r["name"] for r in rows][:2] == ["Lith G1 Intact", "Lex Prime Barrel"]
    assert rows[0]["platinum"] == 6.0 and rows[1]["platinum"] == 4.0
    rec = next(r for r in rows if r["name"] == "Lex Prime Receiver")
    assert rec["unpriced"] is True and rec["ducats"] == 45
    assert s["platinum"] == 10.0 and s["ducats"] == 60 and s["unpriced"] == 1
    assert s["missionTypeLabel"] == "Capture"
    assert pg["totals"]["missions"] == 1 and pg["totals"]["credits"] == 12_400
    allp = mi.page(hist, val, "all", now_ms=now, mission_type="MT_CAPTURE")
    assert allp["matched"] == 1 and allp["missionTypes"][0]["id"] == "MT_CAPTURE"
    assert mi.page(hist, val, "all", now_ms=now)["totals"]["missions"] == 3


# ---- RPC wiring --------------------------------------------------------------------------------------

def test_rpcs_registered(app):
    names = set(rpc.methods())
    for n in ("mastery.summary", "mastery.items", "mastery.item", "mastery.roadmap", "mastery.planned",
              "mastery.pin", "mastery.qam", "mastery.configure", "syndicates.view", "syndicates.qam",
              "syndicates.set_goal", "syndicates.clear_goals", "missions.page", "missions.last", "missions.qam",
              "missions.status", "missions.set_enabled", "missions.delete", "missions.clear"):
        assert n in names, n
    assert not any("mastery" in f or "syndicates" in f or "missions" in f for f in app.services["failed_modules"])
    q = asyncio.run(rpc.call_local("syndicates.qam", {}))
    assert "pools" in q and q["dailyCap"] >= 16000
    v = asyncio.run(rpc.call_local("syndicates.set_goal", {"tag": "CetusSyndicate", "level": 3}))
    assert any(p["tag"] == "CetusSyndicate" for p in v["plans"])
    v = asyncio.run(rpc.call_local("syndicates.set_goal", {"tag": "CetusSyndicate", "level": None}))
    assert not v["plans"]
    st = asyncio.run(rpc.call_local("missions.qam", {}))
    assert "status" in st and st["today"]["missions"] >= 0
    ms = asyncio.run(rpc.call_local("mastery.summary", {}))
    assert "data" in ms
    pins = asyncio.run(rpc.call_local("mastery.pin", {"unique_name": LEX, "pinned": True}))
    assert pins["pinned"][0] == LEX
    asyncio.run(rpc.call_local("mastery.pin", {"unique_name": LEX, "pinned": False}))


# ---- review fixes: caching, prices at view time, relic index, caps ----------------------------------

def test_known_variant_not_overcapped():
    pep = json.loads(json.dumps(PEP))
    kuva_var = "/Lotus/Weapons/Grineer/KuvaLich/LongGuns/KuvaVariant"
    pep["ExportWeapons"][kuva_var] = {"name": "/L/Bramma", "productCategory": "LongGuns"}   # name-dedupe drop
    p = m.build_payload(pep, DICT, WFCD, "t", recipes=RECIPES, regions=REGIONS)
    md_ = m.MasteryData(p)
    assert kuva_var in p["known30"] and kuva_var not in md_.by_unique
    assert md_.max_rank(kuva_var, name="Kuva Bramma") == 30
    assert md_.max_rank("/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Unknown", name="Kuva Thing") == 40
    old = m.MasteryData({k: v for k, v in p.items() if k != "known30"})       # bundle built before known30
    assert old.max_rank(kuva_var, name="Kuva Bramma") == 40


def _lex_needs_blueprint():
    inv = inventory()
    inv["Recipes"] = [r for r in inv["Recipes"] if r["ItemType"] != LEX_BP]
    inv["PendingRecipes"] = []
    return inv


def test_prices_applied_at_view_time():
    svc = service(_lex_needs_blueprint())
    assert "Lex Prime" not in {r["name"] for r in svc.roadmap()["platinum"]}
    assert "lex_prime_blueprint" not in PRICES
    PRICES["lex_prime_blueprint"] = 5.0
    try:
        svc._priced = None                      # skip the short view TTL; no inventory change
        plat = {r["name"]: r for r in svc.roadmap()["platinum"]}
        assert plat["Lex Prime"]["estimatedCost"] == 5.0
    finally:
        PRICES.pop("lex_prime_blueprint")


def test_unpriced_slugs_cover_missing_parts():
    svc = service(_lex_needs_blueprint())
    slugs = svc.unpriced_slugs(svc.progress()[1])
    assert "lex_prime_blueprint" in slugs           # a missing part, not only root slugs
    assert "lex_prime_set" not in slugs             # already priced
    assert len(svc.unpriced_slugs(svc.progress()[1], limit=1)) == 1


def test_progress_single_flight():
    import threading
    import time as _t

    calls = []
    real = m.compute_progress

    def slow(*a, **k):
        calls.append(1)
        _t.sleep(0.1)
        return real(*a, **k)

    inv = inventory()
    svc = service(inv)
    m.compute_progress = slow
    try:
        ts = [threading.Thread(target=svc.summary) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    finally:
        m.compute_progress = real
    assert len(calls) == 1


def test_relic_plans_cached_and_fast():
    import time as _t

    # 400 pools of junk + a few that drop Ash Prime parts: the index skips the junk.
    junk = [(3, [(f"/Lotus/Types/Recipes/X/Part{i}_{j}", f"Thing{i} Prime Part{j}", 25.33) for j in range(6)])
            for i in range(400)]
    ash = [(6, [(ASHP_BP, "Ash Prime Blueprint", 11.0), (ASHP_CHASSIS, "Ash Prime Chassis", 25.33),
                (ASHP_SYS, "Ash Prime Systems", 2.0)])]
    pools = junk + ash
    item = {"name": "Ash Prime", "parts": [{"uniqueName": u, "name": n, "missing": 1, "state": "missing", "building": False} for u, n in
                                           ((ASHP_BP, "Ash Prime Blueprint"), (ASHP_CHASSIS, "Ash Prime Chassis"),
                                            (ASHP_SYS, "Ash Prime Systems"))]}
    rp = m.RelicPools(pools)
    assert m.relic_completion(item, rp) == m.relic_completion(item, pools)
    items = [dict(item, uniqueName=f"/u{i}", status="missing", masteryXpRemaining=3000, currentlyOwned=False)
             for i in range(800)]
    t = _t.perf_counter()
    plans = m.relic_plans(items, rp)
    assert _t.perf_counter() - t < 2.0 and len(plans) == 800

    relics_seen = []

    class RDb:
        def get(self, name):
            relics_seen.append(name)
            return None

    owned = {("Lith A1", "intact"): 1}
    svc = service(inventory(), relics=owned, relic_db=RDb())
    svc.roadmap()
    n = len(relics_seen)
    svc.roadmap()
    assert len(relics_seen) == n                     # plans cached per inventory + owned relics
    svc.invalidate()
    svc.roadmap()
    assert len(relics_seen) == 2 * n


def test_data_missing_cached(tmp_path, caplog):
    m.set_data_dir(str(tmp_path))
    try:
        with caplog.at_level("WARNING"):
            assert m.data() is None and m.data() is None
        assert sum("mastery data unavailable" in r.message for r in caplog.records) == 1
        with gzip.open(tmp_path / m.DATA_FILE, "wb") as f:
            f.write(json.dumps(payload()).encode())
        assert m.data() is None                     # remembered until set_data_dir / retry window
        m.set_data_dir(str(tmp_path))
        assert m.data() is not None
    finally:
        m.set_data_dir(None)


def test_tracker_ignores_ends_without_inventory_source(tmp_path):
    readable = {"v": False}
    t = mi.MissionTracker(str(tmp_path), clock=Clock(1_760_000_000.0), line_source=lambda: mi.REALTIME,
                          can_read=lambda: readable["v"])
    t.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert t.pending["count"] == 0 and t.status()["phase"] == "idle"
    readable["v"] = True
    t.on_mission_end("Sys [Info]: EOM missionLocationUnlocked=1", ended_at=1_760_000_100.0)
    assert t.pending["count"] == 1


def test_missions_setting_switch_resets_tracker(app, monkeypatch):
    from wfh import inventory

    t = mi.tracker()
    assert t is not None
    app.settings.update({"missions": {"enabled": True}})
    t.pending = {"count": 2, "endedAt": 1.0, "info": None}
    t.baseline = {"snap": {"stale": True}, "sync": None, "taken": 0.0}
    app.settings.update({"missions": {"enabled": False}})          # e.g. the Settings page toggle
    assert t.pending["count"] == 0 and t.baseline is None
    fresh = mission_inv(1_760_000_000, neurodes=2)
    monkeypatch.setattr(inventory, "inventory", lambda: type("Inv", (), {"data": fresh})())
    app.settings.update({"missions": {"enabled": True}})
    assert t.baseline is not None and t.baseline["snap"] == mi.snapshot(fresh)
    monkeypatch.setattr(inventory, "status", lambda: {"source": "none"})
    assert mi._status()["blocked"] == "source-none"
    monkeypatch.setattr(inventory, "status", lambda: {"source": "file"})
    assert mi._status()["blocked"] == "source-not-memory"
