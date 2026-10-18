"""Synthetic but realistic player data for the preview harness (dev only, never packaged).

Everything is derived deterministically (fixed RNG seed) from the bundled game data in
data/ plus the recorded fixtures in tests/fixtures, so screenshots are stable run to run:

  build_inventory()      a DE-shaped inventory.php payload: ~60 owned/mastered gear items,
                         resources, prime parts, relics, mods, blueprints, a running foundry,
                         syndicate standings, star chart progress and intrinsics.
  stats_history()        30 days of stats-history.json rows plus a few trades.
  wfm_items()            /v2/items catalog built from the bundled wfmSlug data.
  wfm_top(slug, rank)    a plausible /v2/orders/item/<slug>/top response (prices hashed from slug).
  wfm_statistics(slug)   a plausible /v1/items/<slug>/statistics response.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FIX = os.path.join(ROOT, "tests", "fixtures")

_cache: dict[str, object] = {}


def _load_gz(name: str):
    if name not in _cache:
        with gzip.open(os.path.join(DATA, name)) as f:
            _cache[name] = json.load(f)
    return _cache[name]


def items() -> list[dict]:
    return _load_gz("items.json.gz")["items"]  # type: ignore[index]


def relics() -> list[dict]:
    return _load_gz("relics.json.gz")["relics"]  # type: ignore[index]


def _oid(rng: random.Random) -> dict:
    return {"$oid": "%024x" % rng.getrandbits(96)}


def _date(ms: int) -> dict:
    return {"$date": {"$numberLong": str(int(ms))}}


GEAR_KEYS = {"Warframes": "Suits", "Primary": "LongGuns", "Secondary": "Pistols", "Melee": "Melee",
             "Sentinels": "Sentinels", "SentinelWeapons": "SentinelWeapons", "Archwing": "SpaceSuits",
             "Arch-Gun": "SpaceGuns", "Arch-Melee": "SpaceMelee"}
MAX_XP = {"Suits": 1_600_000, "Sentinels": 1_600_000, "SpaceSuits": 1_600_000}


def build_inventory(now_ms: int) -> dict:
    rng = random.Random(20261017)
    base = json.load(open(os.path.join(FIX, "inventory", "inventory_api.json"), encoding="utf-8"))
    inv: dict = dict(base)
    all_items = items()

    # ---- built gear: a mix of mastered, levelling and freshly built ----
    by_cat: dict[str, list[dict]] = {}
    for it in all_items:
        key = GEAR_KEYS.get(it.get("category", ""))
        if key and it.get("uniqueName"):
            by_cat.setdefault(key, []).append(it)
    counts = {"Suits": 18, "LongGuns": 16, "Pistols": 12, "Melee": 14, "Sentinels": 3, "SentinelWeapons": 3,
              "SpaceSuits": 1, "SpaceGuns": 1, "SpaceMelee": 1}
    xpinfo = []
    for key, n in counts.items():
        pool = sorted(by_cat.get(key, []), key=lambda x: x["uniqueName"])
        picked = rng.sample(pool, min(n, len(pool)))
        have = {e.get("ItemType") for e in inv.get(key) or []}
        rows = list(inv.get(key) or [])
        cap = MAX_XP.get(key, 450_000)
        for it in picked:
            if it["uniqueName"] in have:
                continue
            roll = rng.random()
            xp = cap if roll < 0.55 else int(cap * rng.random() * 0.9) if roll < 0.9 else 0
            row = {"ItemType": it["uniqueName"], "XP": xp, "ItemId": _oid(rng)}
            if rng.random() < 0.15:
                row["Favorite"] = True
            rows.append(row)
        inv[key] = rows
        # sold gear still counts for mastery through XPInfo
        for it in rng.sample(pool, min(4, len(pool))):
            xpinfo.append({"ItemType": it["uniqueName"], "XP": cap})
    inv["XPInfo"] = xpinfo

    # ---- resources ----
    misc = {e["ItemType"]: e["ItemCount"] for e in inv.get("MiscItems") or []}
    res = [it for it in all_items if it.get("category") == "Resources" and it.get("uniqueName", "").startswith("/Lotus/Types/Items/MiscItems/")]
    for it in rng.sample(res, min(70, len(res))):
        misc.setdefault(it["uniqueName"], rng.choice([3, 12, 40, 150, 800, 2400, 9000, 31000, 120000]))
    for u, n in {"/Lotus/Types/Items/MiscItems/Forma": 7, "/Lotus/Types/Items/MiscItems/OrokinCell": 87,
                 "/Lotus/Types/Items/MiscItems/ArgonCrystal": 6, "/Lotus/Types/Items/MiscItems/Kuva": 48250,
                 "/Lotus/Types/Items/MiscItems/Ferrite": 251000, "/Lotus/Types/Items/MiscItems/Rubedo": 18420,
                 "/Lotus/Types/Items/MiscItems/NeuralSensor": 23, "/Lotus/Types/Items/MiscItems/Neurode": 41,
                 "/Lotus/Types/Items/MiscItems/ControlModule": 112, "/Lotus/Types/Items/MiscItems/Gallium": 19,
                 "/Lotus/Types/Items/MiscItems/Morphic": 34, "/Lotus/Types/Items/MiscItems/Tellurium": 9,
                 "/Lotus/Types/Items/MiscItems/PrimeBucks": 22}.items():
        misc[u] = n

    # ---- prime parts and relics: what a relic farmer's stash looks like ----
    recipes = {e["ItemType"]: e["ItemCount"] for e in inv.get("Recipes") or []}
    rel = sorted(relics(), key=lambda r: r["name"])
    owned_relics = rng.sample(rel, min(140, len(rel)))
    for r in owned_relics:
        un = r.get("uniqueNames") or {}
        if un.get("intact"):
            misc[un["intact"]] = misc.get(un["intact"], 0) + rng.choice([1, 1, 2, 3, 4, 6, 9, 14])
        if un.get("radiant") and rng.random() < 0.15:
            misc[un["radiant"]] = rng.choice([1, 2])
        for rw in r.get("rewards") or []:
            u = rw.get("uniqueName") or ""
            if "Prime" not in u or rng.random() > 0.18:
                continue
            n = rng.choice([1, 1, 1, 2, 2, 3, 5])
            if "/WarframeRecipes/" in u or u.endswith("Blueprint"):
                recipes[u] = recipes.get(u, 0) + n
            else:
                misc[u] = misc.get(u, 0) + n
    inv["MiscItems"] = [{"ItemType": u, "ItemCount": n} for u, n in misc.items()]

    # ---- normal blueprints ----
    comps = [it for it in all_items if it.get("category") == "Components" and str(it.get("uniqueName", "")).endswith("Blueprint")]
    for it in rng.sample(comps, min(25, len(comps))):
        recipes.setdefault(it["uniqueName"], 1)
    recipes["/Lotus/Types/Recipes/Components/FormaBlueprint"] = 4
    inv["Recipes"] = [{"ItemType": u, "ItemCount": n} for u, n in recipes.items()]

    # ---- foundry: some ready, some cooking ----
    pend = list(recipes)[:6]
    inv["PendingRecipes"] = [
        {"ItemType": u, "CompletionDate": _date(now_ms + (i - 2) * 3 * 3_600_000), "ItemId": _oid(rng)}
        for i, u in enumerate(pend)]
    inv["PendingRecipes"].append({"ItemType": "/Lotus/Types/Recipes/Components/FormaBlueprint",
                                  "CompletionDate": _date(now_ms - 600_000), "ItemId": _oid(rng)})

    # ---- mods ----
    mods = [it for it in all_items if it.get("category") == "Mods" and it.get("tradable")]
    inv["RawUpgrades"] = [{"ItemType": it["uniqueName"], "ItemCount": rng.choice([1, 1, 2, 3, 5])}
                          for it in rng.sample(mods, min(120, len(mods)))]
    inv["Upgrades"] = [{"ItemType": it["uniqueName"], "UpgradeFingerprint": json.dumps({"lvl": rng.choice([3, 5, 10])}),
                        "ItemId": _oid(rng)} for it in rng.sample(mods, min(30, len(mods)))]

    # ---- syndicates, star chart, intrinsics ----
    inv["Affiliations"] = [
        {"Tag": "SteelMeridianSyndicate", "Standing": 88_000, "Title": 4},
        {"Tag": "ArbitersSyndicate", "Standing": 41_500, "Title": 3},
        {"Tag": "CephalonSudaSyndicate", "Standing": 12_000, "Title": 2},
        {"Tag": "NewLokaSyndicate", "Standing": -4_000, "Title": -1},
        {"Tag": "CetusSyndicate", "Standing": 30_000, "Title": 3},
        {"Tag": "SolarisSyndicate", "Standing": 9_000, "Title": 1},
        {"Tag": "EntratiSyndicate", "Standing": 51_000, "Title": 5},
    ]
    regions = _load_gz("mastery.json.gz").get("regions") or {}  # type: ignore[union-attr]
    nodes = sorted(regions)
    inv["Missions"] = [{"Tag": n, "Completes": rng.randint(1, 30), **({"Tier": 1} if rng.random() < 0.35 else {})}
                       for n in nodes if rng.random() < 0.85]
    inv["PlayerSkills"] = {k: rng.randint(3, 10) for k in (
        "LPS_PILOTING", "LPS_GUNNERY", "LPS_TACTICAL", "LPS_ENGINEERING", "LPS_COMMAND",
        "LPS_DRIFT_COMBAT", "LPS_DRIFT_RIDING", "LPS_DRIFT_OPPORTUNITY", "LPS_DRIFT_ENDURANCE")}
    inv["PlayerLevel"] = 27
    inv["LastInventorySync"] = {"$oid": "%08x%016x" % (now_ms // 1000, 0x1d2c3b4a5968)}
    return inv


def stats_history(now_s: float, days: int = 30) -> tuple[dict, dict]:
    rng = random.Random(7)
    entries = []
    plat, ducats, credits, endo, kuva, aya, vitus = 1100, 3200, 17_500_000, 380_000, 41_000, 12, 160
    for back in range(days, 0, -1):
        t = time.localtime(now_s - back * 86400)
        day = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        if rng.random() < 0.2:
            continue  # days not played
        d = {"plat": rng.choice([0, 0, 15, -20, 45, 90, -60]), "ducats": rng.choice([0, 0, 100, 345, 700]),
             "credits": rng.randint(-200_000, 900_000), "endo": rng.randint(0, 9000), "kuva": rng.choice([0, 1200, 3600]),
             "aya": rng.choice([0, 0, 1, 2]), "vitus": rng.choice([0, 6, 12, 27])}
        plat += d["plat"]; ducats += d["ducats"]; credits += d["credits"]; endo += d["endo"]
        kuva += d["kuva"]; aya += d["aya"]; vitus += d["vitus"]
        abs_ = {"plat": plat, "ducats": ducats, "credits": credits, "endo": endo, "kuva": kuva, "aya": aya, "vitus": vitus}
        entries.append({"date": day, "resources": {k: {"delta": d[k], "abs": abs_[k]} for k in d},
                        "relicsOpened": rng.choice([0, 2, 5, 9, 16]), "dailyTrades": rng.choice([0, 0, 1, 3]),
                        "daysPlayed": 1})
    trades = []
    partners = ["Glaive_Tenno", "RelicRunner", "VoidTrader77", "Lotus_Child", "KuvaLich"]
    names = ["Mesa Prime Chassis Blueprint", "Braton Prime Barrel", "Ash Prime Neuroptics Blueprint",
             "Volt Prime Systems Blueprint", "Serration", "Lith A1 Relic"]
    for i in range(14):
        ts = now_s - i * 86400 * 1.7 - rng.randint(0, 40000)
        t = time.localtime(ts)
        typ = rng.choice(["sale", "sale", "purchase", "trade"])
        trades.append({"id": "t%011d" % i, "ts": ts, "date": f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}",
                       "type": typ, "platChange": 0 if typ == "trade" else rng.choice([5, 8, 12, 25, 40, 65]),
                       "partner": rng.choice(partners),
                       "items": [{"name": rng.choice(names), "count": 1,
                                  "direction": "out" if typ == "sale" else "in"}],
                       "stamp": None, "source": "live"})
    trades.sort(key=lambda x: x["ts"])
    return {"schemaVersion": 2, "entries": entries}, {"schemaVersion": 1, "trades": trades}


# ---- warframe.market -----------------------------------------------------------------------

def _slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_").replace("__", "_")


def wfm_items() -> dict:
    seen: dict[str, dict] = {}
    try:
        for e in json.load(open(os.path.join(FIX, "data", "wfm", "v2_items.json")))["data"]:
            seen[e["slug"]] = e
    except (OSError, ValueError, KeyError):
        pass

    def add(slug, name, game_ref, tags, ducats=0, max_rank=None):
        if not slug or slug in seen:
            return
        e = {"id": hashlib.md5(slug.encode()).hexdigest()[:24], "slug": slug, "gameRef": game_ref, "tags": tags,
             "i18n": {"en": {"name": name, "icon": f"items/images/en/{slug}.png", "thumb": f"items/images/en/thumbs/{slug}.128x128.png"}}}
        if ducats:
            e["ducats"] = ducats
        if max_rank is not None:
            e["maxRank"] = max_rank
        seen[slug] = e

    for it in items():
        cat = (it.get("category") or "").lower()
        if it.get("wfmSlug"):
            tags = ["prime", "set"] if it["wfmSlug"].endswith("_set") else [cat]
            add(it["wfmSlug"], it["name"] + (" Set" if it["wfmSlug"].endswith("_set") and not it["name"].endswith("Set") else ""),
                it["uniqueName"], tags, max_rank=10 if cat == "mods" else (5 if cat == "arcanes" else None))
        for c in it.get("components") or []:
            if c.get("tradable"):
                name = c.get("name") or ""
                full = name if name.startswith(it.get("name", "")) else f"{it.get('name', '')} {name}"
                add(c.get("wfmSlug") or _slugify(full), full, c.get("uniqueName"), ["prime", "component"], c.get("ducats", 0))
    for r in relics():
        add(r.get("wfmSlug"), r["name"] + " Relic", (r.get("uniqueNames") or {}).get("intact"), ["relic", r.get("tier", "").lower()])
        for rw in r.get("rewards") or []:
            add(rw.get("wfmSlug"), rw.get("name"), rw.get("uniqueName"), ["prime", "component"])
    return {"apiVersion": "0.18.3", "data": list(seen.values())}


USERS = ["Glaive_Tenno", "RelicRunner", "VoidTrader77", "Lotus_Child", "OrokinOracle", "KuvaLich", "ZarimanBoi",
         "Nightwave_Nora", "TennoTrade", "PrimeTimeTony", "Baro_Fan", "SolNodeSally"]


def _price(slug: str) -> int:
    h = int(hashlib.sha1(slug.encode()).hexdigest()[:8], 16)
    if slug.endswith("_set"):
        return 40 + h % 160
    if slug.endswith("_relic"):
        return 2 + h % 12
    if "prime" in slug:
        return 3 + h % 45
    return 5 + h % 60


def wfm_top(slug: str, rank: int | None, now_ms: int) -> dict:
    rng = random.Random(slug + str(rank))
    base = _price(slug) * (3 if rank and rank >= 5 else 1)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ms / 1000))

    def order(i, typ, plat):
        u = rng.choice(USERS)
        o = {"id": "%024x" % rng.getrandbits(96), "type": typ, "platinum": plat, "quantity": rng.choice([1, 1, 2, 3, 5]),
             "visible": True, "createdAt": iso, "updatedAt": iso, "itemId": hashlib.md5(slug.encode()).hexdigest()[:24],
             "user": {"id": "u_" + u.lower(), "ingameName": u, "slug": u.lower(), "avatar": None,
                      "reputation": rng.randint(0, 120), "platform": "pc", "crossplay": True, "locale": "en",
                      "status": rng.choice(["ingame", "ingame", "online"]), "activity": {"type": "UNKNOWN", "details": "unknown"},
                      "lastSeen": iso}}
        if rank is not None:
            o["rank"] = rank
        return o

    sell = [order(i, "sell", base + i // 2 + rng.randint(0, 2)) for i in range(5)]
    buy = [order(i, "buy", max(1, base - 1 - i // 2 - rng.randint(0, 2))) for i in range(5)]
    sell.sort(key=lambda o: o["platinum"])
    buy.sort(key=lambda o: -o["platinum"])
    return {"apiVersion": "0.18.3", "data": {"sell": sell, "buy": buy}, "error": None}


def wfm_statistics(slug: str, now_ms: int) -> dict:
    rng = random.Random("stats" + slug)
    base = _price(slug)

    def series(n, step_h):
        out = []
        p = float(base)
        for i in range(n):
            ts = now_ms / 1000 - (n - i) * step_h * 3600
            p = max(1.0, p + rng.uniform(-1.5, 1.5))
            lo, hi = max(1, int(p) - rng.randint(0, 3)), int(p) + rng.randint(0, 4)
            out.append({"datetime": time.strftime("%Y-%m-%dT%H:00:00.000+00:00", time.gmtime(ts)), "volume": rng.randint(1, 40),
                        "min_price": lo, "max_price": hi, "open_price": lo, "closed_price": hi, "avg_price": round(p, 1),
                        "wa_price": round(p, 1), "median": int(p), "moving_avg": round(p, 1), "donch_top": hi + 1,
                        "donch_bot": max(1, lo - 1), "id": f"s{i}"})
        return out

    closed = {"48hours": series(48, 1), "90days": series(90, 24)}
    return {"payload": {"statistics_closed": closed, "statistics_live": closed}}


# ---- arbitration runs ----------------------------------------------------------------------

ARBI_NODES = [("SolNode149", "MT_DEFENSE"), ("SolNode42", "MT_DEFENSE"), ("SolNode305", "MT_DEFENSE"),
              ("SolNode167", "MT_DEFENSE"), ("SolNode149", "MT_DEFENSE"), ("SolNode42", "MT_DEFENSE")]


def _ctime(ts: float) -> str:
    return time.strftime("%a %b %e %H:%M:%S %Y", time.gmtime(ts))


def arbi_logs(now_s: float) -> list[tuple[str, str]]:
    """EE.log excerpts with one Arbitration defense run each (shaped like
    tests/fixtures/arbi/stoefler-defense-ee.txt), spread over the last two weeks."""
    from wfh.data import solnodes

    rng = random.Random(11)
    out = []
    for i, (node_id, mt) in enumerate(ARBI_NODES):
        start_wall = now_s - (i * 2.3 + 0.4) * 86400 - rng.randint(0, 20000)
        label = solnodes.node_label(node_id) or node_id
        t0 = 100.0
        lines = [f"0.127 Sys [Diag]: Current time: {_ctime(start_wall)} [UTC: {_ctime(start_wall)}]",
                 "2.005 Sys [Info]: Warframe startup chatter",
                 f"{t0:.3f} Script [Info]: ThemedSquadOverlay.lua: Pending mission: {node_id}_EliteAlert",
                 f"{t0 + 6:.3f} Script [Info]: ThemedSquadOverlay.lua: Mission name: {label} - Arbitration",
                 f"{t0 + 9:.3f} Sys [Info]: SyncAutoPopulatedConsumables for mission {mt} with location {node_id}",
                 f"{t0 + 12:.3f} Game [Info]: OnStateStarted, mission type={mt}",
                 f"{t0 + 13:.3f} Script [Info]: WaveDefend.lua: WaveDefend - SetupDefense",
                 f"{t0 + 14:.3f} Script [Info]: WaveDefend.lua: Defense: Starting defense waves"]
        t = t0 + 18
        waves = rng.choice([10, 15, 20, 25, 30, 40])
        spawned = 0
        for w in range(1, waves + 1):
            lines.append(f"{t:.3f} Script [Info]: WaveDefend.lua: _SleepBetweenWaves(3)")
            t += 3
            lines.append(f"{t:.3f} Script [Info]: WaveDefend.lua: Defense wave: {w}")
            lines.append(f"{t:.3f} Script [Info]: WaveDefend.lua: Starting wave {w}, spawning a total of {40 + w * 2} "
                         f"tier-{min(3, w // 3)} enemies (29 simultaneous, {min(30, w)}% chance of eximus)")
            dur = rng.uniform(22, 40)
            for k in range(rng.randint(5, 9)):
                spawned += 1
                ts = t + dur * (k + 1) / 10
                live = rng.randint(8, 29)
                if rng.random() < 0.35:
                    lines.append(f"{ts:.3f} AI [Info]: OnAgentCreated /Npc/CorpusEliteShieldDroneAgent{spawned} Live {live} "
                                 f"Spawned {spawned} Ticking {live} Paused 0 IgnoredTicking 1 MonitoredTicking {live - 1} "
                                 "AllyLive 4 AllyActive 4 NeutralActive 0")
                else:
                    lines.append(f"{ts:.3f} AI [Info]: OnAgentCreated /Npc/ShieldLancer{spawned} Live {live} Spawned {spawned} "
                                 f"Ticking {live} Paused 0 IgnoredTicking 0 MonitoredTicking {live} AllyLive 4 AllyActive 4 NeutralActive 0")
            t += dur
            if w % 5 == 0:
                lines.append(f"{t:.3f} Sys [Info]: Created /Lotus/Interface/ProjectionsCountdown.swf")
                lines.append(f"{t + 0.01:.3f} Sys [Info]: Created /Lotus/Interface/DefenseReward.swf")
                t += 2
        lines.append(f"{t + 20:.3f} Script [Info]: EndOfMatch.lua: Initialize")
        lines.append(f"{t + 20.3:.3f} Sys [Info]: EOM missionLocationUnlocked=1")
        out.append((f"arbi-run-{i}.log", "\n".join(lines) + "\n"))
    return out


# ---- mission reward history (<runtime>/mission-rewards.json) -------------------------------

MISSION_NODES = [("MT_SURVIVAL", "SolNode64"), ("MT_DEFENSE", "SolNode149"), ("MT_EXTERMINATION", "SolNode23"),
                 ("MT_CAPTURE", "SolNode79"), ("MT_MOBILE_DEFENSE", "SolNode42"), ("MT_EXCAVATE", "SolNode130")]


def mission_history(now_s: float, n: int = 18) -> dict:
    rng = random.Random(23)
    pool = [it["uniqueName"] for it in items() if it.get("category") == "Resources"][:60]
    rewards = [rw["uniqueName"] for r in relics()[:200] for rw in r.get("rewards") or [] if "Prime" in rw.get("name", "")]
    relic_un = [(r.get("uniqueNames") or {}).get("intact") for r in relics()[:300] if (r.get("uniqueNames") or {}).get("intact")]
    out = []
    for i in range(n):
        ended = int((now_s - i * 3.3 * 3600 - rng.randint(0, 3000)) * 1000)
        mt, node = rng.choice(MISSION_NODES)
        its = [{"uniqueName": u, "count": rng.choice([3, 12, 40, 150, 600])} for u in rng.sample(pool, rng.randint(2, 5))]
        if rng.random() < 0.5:
            its.append({"uniqueName": rng.choice(rewards), "count": 1})
        if rng.random() < 0.6:
            its.append({"uniqueName": rng.choice(relic_un), "count": rng.choice([1, 2])})
        out.append({"id": f"{ended}-{ended + 4000}", "endedAt": ended, "readAt": ended + 4000,
                    "missionCount": rng.choice([1, 1, 1, 2]), "items": its, "credits": rng.randint(8000, 90000),
                    "endo": rng.choice([0, 0, 150, 400]), "missionType": mt, "node": node})
    out.sort(key=lambda s: s["endedAt"])
    return {"summaries": out}
