"""Item/relic bundle build, loaders, runtime refresh and the OCR name matcher."""

import asyncio
import json
import os
import time

import pytest

from wfh import rpc
from wfh.data import items as items_mod
from wfh.data import names, relics as relics_mod
from wfh.data.names import match_single, rank_candidates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WFCD = os.path.join(ROOT, "tests", "fixtures", "data", "wfcd")


@pytest.fixture
def bundle(tmp_path):
    """Fixture WFCD subset built into a bundle dir, with loaders pointed at it."""
    out = tmp_path / "bundle"
    manifest = items_mod.build_bundle(items_mod.dir_reader(WFCD), str(out), "1.1276.18", "2026-10-08T19:33:06Z")
    items_mod.set_data_dirs(str(out), str(tmp_path / "runtime"))
    yield {"dir": str(out), "runtime": str(tmp_path / "runtime"), "manifest": manifest}
    items_mod._dirs_override = None
    items_mod.reset()


# ---- bundle build -----------------------------------------------------------

def test_build_manifest_and_sizes(bundle):
    m = bundle["manifest"]
    assert m["schema"] == items_mod.SCHEMA and m["version"] == "1.1276.18"
    assert m["counts"]["relics"] == 5
    assert m["counts"]["rewards"] == 29
    for f in (items_mod.ITEMS_FILE, items_mod.RELICS_FILE):
        assert os.path.getsize(os.path.join(bundle["dir"], f)) == m["files"][f]["gzip"]
    with open(os.path.join(bundle["dir"], "manifest.json")) as fh:
        assert json.load(fh)["counts"] == m["counts"]


def test_prime_parent_components_and_set_slug(bundle):
    db = items_mod.items()
    ash = db.by_name("Ash Prime")
    assert ash.category == "Warframes" and ash.vaulted is True and ash.tradable is False
    assert ash.wfm_slug == "ash_prime_set"
    comps = {c.name: c for c in ash.components}
    assert comps["Ash Prime Chassis"].ducats == 15
    assert comps["Ash Prime Blueprint"].ducats == 45
    assert comps["Orokin Cell"].ducats is None and not comps["Orokin Cell"].tradable
    braton = db.by_name("braton prime")
    assert braton.mastery_req == 8 and braton.wfm_slug == "braton_prime_set"
    bcomps = {c.name: c for c in braton.components}
    assert bcomps["Orokin Cell"].item_count == 10 and bcomps["Braton Prime Barrel"].tradable


def test_component_items_and_blueprint_alias(bundle):
    db = items_mod.items()
    part = db.by_unique("/Lotus/Types/Recipes/WarframeRecipes/EmberPrimeChassisComponent")
    assert part.name == "Ember Prime Chassis"
    assert part.wfm_slug == "ember_prime_chassis_blueprint"  # WFM trades the blueprint
    bp = db.by_unique("/Lotus/Types/Recipes/WarframeRecipes/EmberPrimeChassisBlueprint")
    assert bp.name == "Ember Prime Chassis Blueprint" and bp.relic_reward
    assert bp.ducats == part.ducats and bp.parent == "/Lotus/Powersuits/Ember/EmberPrime"


def test_reward_pool(bundle):
    db = items_mod.items()
    pool = {i.name: i for i in db.prime_parts()}
    assert len(pool) == 29
    names_by_len = [i.name for i in db.prime_parts()]
    assert names_by_len == sorted(names_by_len, key=lambda n: -len(n))
    assert pool["Rhino Prime Blueprint"].ducats == 100
    assert pool["Rhino Prime Blueprint"].wfm_slug == "rhino_prime_blueprint"
    assert pool["Forma Blueprint"].wfm_slug is None and pool["Forma Blueprint"].ducats is None
    two = pool["2X Forma Blueprint"]
    assert two.unique_name == pool["Forma Blueprint"].unique_name
    # by_unique keeps the catalog entry, by_name finds the counted reward.
    assert db.by_name("2x forma blueprint") is two
    assert pool["Braton Prime Barrel"].rarity == "Common"   # 25.33 intact, WFCD says Uncommon
    assert pool["Akstiletto Prime Blueprint"].rarity == "Rare"


def test_reward_vaulted_is_and_over_relics(bundle):
    db = items_mod.items()
    pool = {i.name: i for i in db.prime_parts()}
    assert pool["Rhino Prime Blueprint"].vaulted is True
    # Axi C12 is a Citrine relic: WFCD flags it vaulted but lists drops -> not vaulted.
    assert pool["Kestrel Prime Blueprint"].vaulted is False


def test_relic_db(bundle):
    db = relics_mod.relics()
    assert len(db) == 5
    lith = db.get("Lith A1")
    assert db.get("lith a1 relic") is lith and db.get("Lith A1 Radiant") is lith
    assert lith.tier == "Lith" and lith.code == "A1" and lith.vaulted and lith.wfm_slug == "lith_a1_relic"
    barrel = next(r for r in lith.rewards if r.item_name == "Braton Prime Barrel")
    assert barrel.rarity == "Common"
    assert barrel.chance_by_refinement == {"intact": 25.33, "exceptional": 23.33, "flawless": 20.0, "radiant": 16.67}
    assert barrel.chance("Radiant") == 16.67 and barrel.wfm_slug == "braton_prime_barrel"
    assert barrel.item_unique == "/Lotus/Types/Recipes/Weapons/WeaponParts/BratonPrimeBarrel"
    rare = [r.item_name for r in lith.rewards if r.rarity == "Rare"]
    assert rare == ["Akstiletto Prime Blueprint"]
    assert db.get("Axi C12").vaulted is False
    assert db.by_unique(lith.unique_names["flawless"]) == (lith, "flawless")
    assert db.get(lith.unique_names["intact"]) is lith
    assert [r.name for r in db.containing("forma blueprint")] == ["Lith A1", "Neo R1"]
    assert db.get("Requiem I").rewards[0].item_name == "Exilus Weapon Adapter Blueprint"
    assert {r.name for r in db.unvaulted()} == {"Axi C12"}
    assert db.get("Lith Z99") is None and db.get("nonsense") is None


def test_relic_reward_rarity_table():
    rr = items_mod.relic_reward_rarity
    assert [rr("Intact", c) for c in (25.33, 11, 2)] == ["Common", "Uncommon", "Rare"]
    assert [rr("Exceptional", c) for c in (23.33, 13, 4)] == ["Common", "Uncommon", "Rare"]
    assert [rr("Flawless", c) for c in (20, 17, 6)] == ["Common", "Uncommon", "Rare"]
    assert [rr("Radiant", c) for c in (16.67, 20, 10)] == ["Common", "Uncommon", "Rare"]
    assert rr("requiem?", 25.33, "Uncommon") == "Uncommon"
    assert rr("intact", 50, "Uncommon") == "Uncommon"


def test_parse_relic_name():
    assert relics_mod.parse_relic_name("Meso F2 Relic") == ("Meso", "F2", None)
    assert relics_mod.parse_relic_name("axi c12 exceptional") == ("Axi", "C12", "exceptional")
    assert relics_mod.parse_relic_name("Void Relic") is None


def test_item_search_and_match(bundle):
    db = items_mod.items()
    found = [i.name for i in db.search("ash prime")]
    assert found[0] == "Ash Prime"
    assert "Ash Prime Chassis" in found
    assert db.search("a") == []
    item, conf = db.match_name("Rhino Prlme BlueDrint")
    assert item.name == "Rhino Prime Blueprint" and conf == 1.0
    item, conf = db.match_name("2 XForma Blueprint")
    assert item.name == "2X Forma Blueprint" and conf >= 0.98
    item, conf = db.match_name("Forma 81ueprint")
    assert item.name == "Forma Blueprint"
    item, conf = db.match_name("Ember Prime Chassis")  # first line of a wrapped title
    assert item.name == "Ember Prime Chassis Blueprint" and conf >= names.SLOT_ACCEPT_CONFIDENCE
    assert db.match_name("") == (None, 0.0)


def test_empty_db_when_no_data(tmp_path):
    items_mod.set_data_dirs(str(tmp_path / "none"), None)
    try:
        assert len(items_mod.items()) == 0 and items_mod.items().match_name("Forma") == (None, 0.0)
        assert len(relics_mod.relics()) == 0
    finally:
        items_mod._dirs_override = None
        items_mod.reset()


# ---- runtime refresh --------------------------------------------------------

class FakeGithub:
    """Serves the fixture WFCD files like raw.githubusercontent.com, with ETags."""

    def __init__(self):
        self.calls = []
        self.etag = "v1"

    def __call__(self, url, etag):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, etag))
        if etag and etag == self.etag:
            return 304, b"", etag
        path = os.path.join(WFCD, name)
        body = open(path, "rb").read() if os.path.exists(path) else b"[]"
        return 200, body, self.etag


def test_refresh_flow(bundle, monkeypatch):
    monkeypatch.setattr(items_mod, "MIN_REFRESH_RELICS", 1)
    monkeypatch.setattr(items_mod, "MIN_REFRESH_ITEMS", 1)
    gh = FakeGithub()
    assert items_mod.active_data_dir() == (bundle["dir"], "bundle")
    before = items_mod.items()

    out = items_mod.refresh(fetch=gh)
    assert out["status"] == "updated" and out["counts"]["relics"] == 5
    assert len(gh.calls) == len(items_mod.SOURCE_FILES)
    # Built now from "master": newer source than the 2026-10-08 bundle, so it wins.
    assert items_mod.active_data_dir() == (bundle["runtime"], "runtime")
    after = items_mod.items()
    assert after is not before and after.source == "runtime"
    assert relics_mod.relics().source == "runtime"

    gh.calls.clear()
    assert items_mod.refresh(fetch=gh)["status"] == "fresh" and gh.calls == []

    # A week later: ETag probes answer 304, nothing else is downloaded.
    state = items_mod._read_state(bundle["runtime"])
    state["checked_at"] = time.time() - items_mod.REFRESH_INTERVAL - 1
    items_mod._write_state(bundle["runtime"], state)
    assert items_mod.refresh(fetch=gh)["status"] == "unchanged"
    assert gh.calls == [("Relics.json", "v1"), ("Components.json", "v1")]

    # Upstream changed: full download again.
    gh.calls.clear()
    gh.etag = "v2"
    assert items_mod.refresh(force=False, fetch=gh)["status"] == "fresh"  # checked just now
    assert items_mod.refresh(force=True, fetch=gh)["status"] == "updated"
    assert len(gh.calls) == len(items_mod.SOURCE_FILES)


def test_refresh_rejects_tiny_upstream(bundle):
    gh = FakeGithub()
    with pytest.raises(ValueError):
        items_mod.refresh(force=True, fetch=gh)
    assert items_mod.active_data_dir() == (bundle["dir"], "bundle")


def test_refresh_failure_keeps_bundle(bundle):
    def broken(url, etag):
        raise OSError("network down")

    with pytest.raises(OSError):
        items_mod.refresh(force=True, fetch=broken)
    assert items_mod.items().source == "bundle"


def test_newer_bundle_beats_stale_runtime(bundle, tmp_path):
    items_mod.build_bundle(items_mod.dir_reader(WFCD), bundle["runtime"], "github-master", "2026-01-01T00:00:00Z")
    assert items_mod.active_data_dir()[1] == "bundle"


def test_rpcs(app, bundle):
    out = asyncio.run(rpc.call_local("data.status"))
    assert out["active"] == "bundle" and out["items"]["reward_pool"] == 29
    hits = asyncio.run(rpc.call_local("items.search", {"query": "rhino prime"}))
    assert hits[0]["name"].startswith("Rhino Prime")
    relic = asyncio.run(rpc.call_local("relics.get", {"name": "Neo R1 Relic"}))
    assert relic["name"] == "Neo R1" and len(relic["rewards"]) == 6


@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "data", "manifest.json")),
                    reason="bundled data not built (scripts/fetch-data.sh)")
def test_real_bundle_smoke():
    items_mod.set_data_dirs(os.path.join(ROOT, "data"), None)
    try:
        db = items_mod.items()
        assert len(db) > 5000 and len(db.prime_parts()) > 400
        assert len(relics_mod.relics()) > 500
        assert db.match_name("Wukon Chassis \u00e5lueorint")[0].name == "Wukong Prime Chassis Blueprint"
        sizes = json.load(open(os.path.join(ROOT, "data", "manifest.json")))["files"]
        assert sum(f["gzip"] for f in sizes.values()) < 1_000_000
    finally:
        items_mod._dirs_override = None
        items_mod.reset()


# ---- matcher (ported from WFHelper tests/main/rewardScannerMatch.test.ts) ----

ITEMS = [{"name": n} for n in [
    "Braton Prime Stock", "Trumna Prime Blueprint", "Forma Blueprint", "Caliban Prime Neuroptics Blueprint",
    "Nautilus Prime Systems", "Epitaph Prime Receiver", "Zephyr Prime Neuroptics Blueprint",
    "Wukong Prime Chassis Blueprint", "Rhino Prime Blueprint", "Trinity Prime Blueprint", "Limbo Prime Blueprint",
    "Saryn Prime Blueprint",
]]


def pool(*ns):
    return [{"name": n} for n in ns]


def top(text, items, limit=1):
    return rank_candidates(text, items, limit)


def test_exact_and_near_exact():
    assert match_single("Forma BlueDrint", ITEMS).name == "Forma Blueprint"
    assert match_single("E itaDh Prime Receiver", ITEMS).name == "Epitaph Prime Receiver"
    ranked = top("Caliban Blueprint Naut", ITEMS, 3)
    assert any(c.name == "Caliban Prime Neuroptics Blueprint" for c in ranked)


def test_short_names_inside_longer_titles():
    ranked = top("Paris Prime Upper Limb", pool("Paris Prime Upper Limb", "Ris"), 3)
    assert ranked[0].name == "Paris Prime Upper Limb"
    assert all(c.name != "Ris" for c in ranked)
    mods = ["Fass", "Jahu", "Khra", "Lohk", "Netra", "Ris", "Vome", "Xata", "Oull"]
    for n in mods:
        ranked = top(n.upper(), pool("Paris Prime Upper Limb", *mods), 3)
        assert ranked[0].name == n and ranked[0].mode == "exact"


def test_structural_boosts():
    yareli = pool("Yareli Prime Chassis Blueprint", "Yareli Prime Blueprint", "Yareli Prime Systems Blueprint",
                  "Yareli Prime Neuroptics Blueprint") + ITEMS
    r = top("Yareli Prime Chassis", yareli, 4)
    assert r[0].name == "Yareli Prime Chassis Blueprint" and r[0].mode == "substring" and r[0].confidence >= 0.92
    braton = pool("Braton Prime Stock", "Braton Prime Blueprint", "Braton Prime Receiver", "Braton Prime Barrel")
    assert all(c.confidence < 0.92 for c in top("Braton Prime", braton, 4) if c.mode == "substring")
    assert all(c.confidence < 0.92 for c in top("Yareli", pool("Yareli Prime Blueprint"), 3))
    r1 = top("2x Forma Blueprint", ITEMS, 4)
    assert r1[0].name == "Forma Blueprint" and r1[0].confidence >= 0.92
    r2 = top("Braton Prime Stock Trumna", ITEMS, 4)
    assert r2[0].name == "Braton Prime Stock" and r2[0].confidence >= 0.92
    assert all(c.confidence < 0.92 for c in top("Forma Blueprint Braton Prime Stock", ITEMS, 4))
    wk = pool("Wukong Prime Chassis Blueprint", "Wukong Prime Blueprint") + ITEMS
    for read in ("Wukong Chassis Blueprint", "Wukon Chassis \u00e5lueorint"):
        r = top(read, wk, 4)
        assert r[0].name == "Wukong Prime Chassis Blueprint" and r[0].confidence >= 0.92, read
    amb = pool("Wukong Prime Chassis Blueprint", "Yareli Prime Chassis Blueprint")
    assert all(c.confidence < 0.92 for c in top("Prime Chassis Blueprint", amb, 4))


def test_alias_table():
    assert match_single("Rhino Prlme BlueDrint", ITEMS).name == "Rhino Prime Blueprint"
    assert match_single("Zephyr Prime Neurootics Blueprint", ITEMS).name == "Zephyr Prime Neuroptics Blueprint"
    assert match_single("Nautilus Prime Svstems", ITEMS).name == "Nautilus Prime Systems"
    assert match_single("Trinlty Prime Blueprint", ITEMS).name == "Trinity Prime Blueprint"
    assert match_single("Wukong Prime Chassls Blueprint", ITEMS).name == "Wukong Prime Chassis Blueprint"
    hit = match_single("Forma Blueprint Braton Prime Stock", pool("Forma Blueprint", "Braton Prime Stock"))
    assert hit.confidence < 0.92


def test_short_pool_name_vs_long_read():
    p = pool("Khra", "Khora Prime Blueprint", "Khora Prime Chassis Blueprint")
    for read in ("Khora Prime Bluepr", "Khora Prime Bluepr ll"):
        r = top(read, p, 4)
        assert r[0].name == "Khora Prime Blueprint" and "Khra" not in [c.name for c in r]
    for read in ("Khra", "KHRA", "Khra "):
        hit = match_single(read, p)
        assert hit.name == "Khra" and hit.mode == "exact"
    fuzzy = match_single("Netro", pool("Netra", "Khora Prime Blueprint"))
    assert fuzzy.name == "Netra" and fuzzy.mode == "fuzzy"


WUKONG = pool("Wukong Prime Blueprint", "Wukong Prime Chassis Blueprint", "Wukong Prime Neuroptics Blueprint",
              "Wukong Prime Systems Blueprint")
FROST = pool("Frost Prime Blueprint", "Frost Prime Chassis Blueprint", "Frost Prime Neuroptics Blueprint",
             "Frost Prime Systems Blueprint")


def _conf(ranked, name):
    return next((c.confidence for c in ranked if c.name == name), 0)


def test_destroyed_component_word():
    r = top("Wukong Prime Cha55i5 Blueprint", WUKONG, 4)
    assert _conf(r, "Wukong Prime Blueprint") < 0.86 and r[0].name == "Wukong Prime Chassis Blueprint"
    r = top("Frost Prime Neur optics Blueprint", FROST, 4)
    assert _conf(r, "Frost Prime Blueprint") < 0.86 and r[0].name == "Frost Prime Neuroptics Blueprint"
    r = top("Wukong Prime Cha55i5 Blueprint ll", WUKONG, 4)
    assert _conf(r, "Wukong Prime Blueprint") < 0.86 and r[0].name == "Wukong Prime Chassis Blueprint"


def test_noise_tokens_do_not_cost_confidence():
    for read in ("Wukong Prime Systems Blueprin", "Wukong Prime Systems Blueprin ll",
                 "Wukong Prime Systems Blueprin ll vv"):
        hit = match_single(read, WUKONG)
        assert hit.name == "Wukong Prime Systems Blueprint" and hit.confidence >= 0.92, read


def test_misread_does_not_raise_confidence():
    titania = pool("Titania Prime Blueprint", "Titania Prime Chassis Blueprint",
                   "Titania Prime Neuroptics Blueprint", "Titania Prime Systems Blueprint")
    clean = match_single("Titania Prime", titania)
    assert clean.confidence < 0.92
    bad = match_single("Tltanla Prlme", titania)
    assert bad.confidence < 0.86 and bad.confidence < clean.confidence


def test_quantity_prefix_pairs():
    pair = pool("Forma Blueprint", "2X Forma Blueprint")
    hit = match_single("Forma Blueprin", pair)
    assert hit.name == "Forma Blueprint" and hit.confidence >= 0.86
    for read in ("2X Forma Blueprint", "2 X Forma Blueprint", "2x forma blueprint",
                 "2 XForma Blueprint", "2XForma Blueprint", "2 X Forma 81ueprint"):
        hit = match_single(read, pair)
        assert hit.name == "2X Forma Blueprint" and hit.mode == "exact", read
    for read in ("Forma Blueprint", "Forma 81ueprint", "forma blueprint"):
        assert match_single(read, pair).name == "Forma Blueprint", read
    kuva = pool("1200X Kuva", "Forma Blueprint")
    assert match_single("1200 X Kuva", kuva).name == "1200X Kuva"
    assert match_single("1200X Kuv", kuva).name == "1200X Kuva"


def test_merged_glyph_reads():
    p = pool("Caliban Prime Blueprint", "Caliban Prime Chassis Blueprint", "Caliban Prime Neuroptics Blueprint",
             "Ash Prime Chassis Blueprint", "Xaku Prime Blueprint", "Sarofang Prime Handle") + ITEMS
    hit = match_single("Caliban Prime81ueprint", p)
    assert hit.name == "Caliban Prime Blueprint" and hit.mode == "exact" and hit.confidence >= 0.98
    hit = match_single("Caliban Prime\u00fclueprint", p)
    assert hit.name == "Caliban Prime Blueprint" and hit.confidence >= 0.92
    for read in ("Caliban Prime81ueprint", "Caliban Prime\u00fclueprint"):
        for c in top(read, p, 4):
            if c.name != "Caliban Prime Blueprint":
                assert c.confidence < 0.92, (read, c.name)
    assert match_single("Caliban Prime Chassis 81ueprint", p).name == "Caliban Prime Chassis Blueprint"
    counted = pool("2X Forma Blueprint", "5X Forma Blueprint")
    assert match_single("5X Forma Blueprint", counted).name == "5X Forma Blueprint"
    for c in top("5X Forma Blueprint", counted, 4):
        if c.name != "5X Forma Blueprint":
            assert c.confidence < 0.92


def test_empty_read_and_objects():
    assert match_single("", ITEMS).mode == "none"
    assert match_single("   ", ITEMS).item is None

    class Obj:
        def __init__(self, name):
            self.name = name

    objs = [Obj("Forma Blueprint"), Obj("Rhino Prime Blueprint")]
    assert match_single("rhino prime blueprint", objs).item is objs[1]
    assert match_single("Forma Blueprint", objs).to_json()["mode"] == "exact"


def test_text_helpers():
    assert names.normalize_for_slug("Medi-Ray") == "medi_ray"
    assert names.normalize_for_slug("Akbronco Prime’s Blueprint") == "akbronco_primes_blueprint"
    assert names.normalize_wfm_slug("zid-an_asheir") == "zid-an_asheir"
    assert names.normalize_wfm_slug("Ash Prime Set") == "ash_prime_set"
    assert names.normalize_reward_text("2 x Forma BLUEDRINT") == "2x forma blueprint"
    assert names.fold_ocr_confusions("Prime81ueprint") == "primeblueprint"
    assert names.has_quantity_prefix("2x forma") and not names.has_quantity_prefix("x2 forma")
    assert names.similarity("prme", "prime") == 0.8
    assert names.levenshtein("kitten", "sitting") == 3
