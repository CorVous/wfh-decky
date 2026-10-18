"""Warframe world state: fissures, traders, sortie, nightwave, cycles and resets.

Port of WFHelper ``services/worldStateParser.ts`` + ``worldStateSteelPath.ts``
(docs/port/data-sources.md §3, menus.md §10). Shape of ``state()`` follows
WFHelper's ``WorldState`` (src/types/world.ts) so the World page can port the
Svelte view functions as-is: dates are ISO strings ("2026-10-17T12:00:00.000Z").
Additions for the Deck are marked below (``resets``, ``circuit``,
``zarimanCycle``, ``planet``/``nodeId`` on rows, cycle ``activation``,
``fetchedAt``/``stale``/``error``/``source``).

Sources, in order:
1. DE ``api.warframe.com/cdn/worldState.php``, then ``content.warframe.com``,
   then the browse.wf oracle copy (same DE format).
2. Enrichment, in parallel and optional (setting ``world.enrich``): oracle
   ``bounty-cycle`` (Cetus/Cambion expiry, seed bounties for Zariman/Cavia/Hex)
   and warframestat ``/pc``. WFHelper asks warframestat three times
   (``/pc``, ``/pc/invasions``, ``/pc/syndicateMissions``); ``/pc`` carries all
   three, so one request does here. A host that fails is skipped for 5 min so a
   blocked network does not add timeouts to every refresh.

Cycle math is the spec's (vallis/duviri pure math; cetus/cambion from the
bounty-cycle expiry). When the oracle is unreachable the Cetus bounty
``SyndicateMissions`` expiry from DE's own payload is the same 150-min boundary,
so Cetus/Cambion still resolve; warframestat's ``cetusCycle`` is the last resort.

Cache: memory only, 90 s for the RPC (``state(max_age)`` for callers), single
flight, stale-on-error; nothing is fetched until something asks (battery).
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import log, net, rpc
from . import arbi_schedule as _arbi
from . import solnodes
from .solnodes import enum_tail, title_case

_log = log.get("worldstate")

FETCH_URLS = (
    "https://api.warframe.com/cdn/worldState.php",
    "https://content.warframe.com/dynamic/worldState.php",
)
ORACLE_WORLDSTATE_URL = "https://oracle.browse.wf/worldState.json"
ORACLE_BOUNTY_CYCLE_URL = "https://oracle.browse.wf/bounty-cycle"
WARFRAMESTAT_URL = "https://api.warframestat.us/pc"
FETCH_TIMEOUT = 20.0
CYCLE_FETCH_TIMEOUT = 4.0
HOST_BACKOFF = 300.0
FAIL_BACKOFF = 60.0                  # after a failed refresh, serve stale/empty without refetching
FETCH_BUDGET = 45.0                  # all main sources together; RPC timeout (main.py) is 60 s
RPC_MAX_AGE = 90.0

VALLIS_EPOCH_MS = 1541837628000      # "November 10, 2018 08:13:48 UTC"
VALLIS_PERIOD_MS = 1_600_000
VALLIS_WARM_MS = 400_000
POE_NIGHT_MS = 3_000_000
POE_CYCLE_MS = 9_000_000             # 100 min day + 50 min night
DUVIRI_MOOD_PERIOD_MS = 7_200_000
DUVIRI_MOODS = ("Sorrow", "Fear", "Joy", "Anger", "Envy")

STEEL_PATH_EPOCH_MS = 1605484800000  # 2020-11-16T00:00:00Z
STEEL_PATH_EIGHT_WEEKS_S = 4_838_400
STEEL_PATH_SEVEN_DAYS_S = 604_800
STEEL_PATH_ROTATION = (
    {"name": "Umbra Forma Blueprint", "cost": 150},
    {"name": "50,000 Kuva", "cost": 55},
    {"name": "Kitgun Riven Mod", "cost": 75},
    {"name": "3x Forma", "cost": 75},
    {"name": "Zaw Riven Mod", "cost": 75},
    {"name": "30,000 Endo", "cost": 150},
    {"name": "Rifle Riven Mod", "cost": 75},
    {"name": "Shotgun Riven Mod", "cost": 75},
)
STEEL_PATH_EVERGREENS = (
    {"name": "Veiled Riven Cipher", "cost": 20},
    {"name": "Bishamo Pauldrons Blueprint", "cost": 15},
    {"name": "Bishamo Cuirass Blueprint", "cost": 25},
    {"name": "Bishamo Helmet Blueprint", "cost": 20},
    {"name": "Bishamo Greaves Blueprint", "cost": 25},
    {"name": "10k Kuva", "cost": 15},
    {"name": "Primary Arcane Adapter", "cost": 15},
    {"name": "Secondary Arcane Adapter", "cost": 15},
    {"name": "Relic Pack", "cost": 15},
    {"name": "Stance Forma Blueprint", "cost": 10},
)

# src/lib/world.ts, wiki-sourced weekly groups in cycle order.
CIRCUIT_NORMAL_ROTATION = (
    ("Excalibur", "Trinity", "Ember"), ("Loki", "Mag", "Rhino"), ("Ash", "Frost", "Nyx"),
    ("Saryn", "Vauban", "Nova"), ("Nekros", "Valkyr", "Oberon"), ("Hydroid", "Mirage", "Limbo"),
    ("Mesa", "Chroma", "Atlas"), ("Ivara", "Inaros", "Titania"), ("Nidus", "Octavia", "Harrow"),
    ("Gara", "Khora", "Revenant"), ("Garuda", "Baruuk", "Hildryn"),
)
CIRCUIT_HARD_ROTATION = (
    ("Braton", "Lato", "Skana", "Paris", "Kunai"),
    ("Boar", "Gammacor", "Angstrum", "Gorgon", "Anku"),
    ("Bo", "Latron", "Furis", "Furax", "Strun"),
    ("Lex", "Magistar", "Boltor", "Bronco", "Ceramic Dagger"),
    ("Torid", "Dual Toxocyst", "Dual Ichor", "Miter", "Atomos"),
    ("Ack & Brunt", "Soma", "Vasto", "Nami Solo", "Burston"),
    ("Zylok", "Sibear", "Dread", "Despair", "Hate"),
    ("Dera", "Sybaris", "Cestra", "Sicarus", "Okina"),
    ("Vectis", "Stug", "Ballistica", "Destreza", "Obex"),
)

HUB_NODE = {
    "SaturnHUB": "Kronia Relay (Saturn)",
    "MarsHUB": "Strata Relay (Mars)",
    "CerberusHUB": "Orcus Relay (Pluto)",
    "PlutoHUB": "Orcus Relay (Pluto)",
    "EarthHUB": "Larunda Relay (Earth)",
    "VenusHUB": "Vesper Relay (Venus)",
    "EuropaHUB": "Leonov Relay (Europa)",
    "NeptuneHUB": "Maroo's Bazaar (Mars)",
    "Relay Node 0": "Larunda Relay (Earth)",
    "Relay Node 4": "Strata Relay (Mars)",
    "Relay Node 9": "Vesper Relay (Venus)",
    "Relay Node 12": "Kronia Relay (Saturn)",
    "Relay Node 17": "Orcus Relay (Pluto)",
    "Relay Node 20": "Leonov Relay (Europa)",
}

VOID_TIER = {"VoidT1": "Lith", "VoidT2": "Meso", "VoidT3": "Neo", "VoidT4": "Axi", "VoidT5": "Requiem",
             "VoidT6": "Omnia"}
VOID_TIER.update({k + "Hard": v for k, v in list(VOID_TIER.items())})

SORTIE_MODIFIER = {
    "SORTIE_MODIFIER_ARMOR": "Enhanced Enemy Armor",
    "SORTIE_MODIFIER_SHIELDS": "Augmented Enemy Shields",
    "SORTIE_MODIFIER_HEALTH": "Enhanced Enemy Health",
    "SORTIE_MODIFIER_EXIMUS": "Eximus Stronghold",
    "SORTIE_MODIFIER_LOW_ENERGY": "Energy Reduction",
    "SORTIE_MODIFIER_LOW_GRAVITY": "Low Gravity",
    "SORTIE_MODIFIER_HIGH_GRAVITY": "High Gravity",
    "SORTIE_MODIFIER_RADIATION": "Enemy Elemental Enhancement: Radiation",
    "SORTIE_MODIFIER_VIRAL": "Enemy Elemental Enhancement: Viral",
    "SORTIE_MODIFIER_CORROSIVE": "Enemy Elemental Enhancement: Corrosive",
    "SORTIE_MODIFIER_MAGNETIC": "Enemy Elemental Enhancement: Magnetic",
    "SORTIE_MODIFIER_GAS": "Enemy Elemental Enhancement: Gas",
    "SORTIE_MODIFIER_EXPLOSION": "Enemy Elemental Enhancement: Blast",
    "SORTIE_MODIFIER_FIRE": "Enemy Elemental Enhancement: Heat",
    "SORTIE_MODIFIER_FREEZE": "Enemy Elemental Enhancement: Cold",
    "SORTIE_MODIFIER_ELECTRICITY": "Enemy Elemental Enhancement: Electricity",
    "SORTIE_MODIFIER_POISON": "Enemy Elemental Enhancement: Toxin",
    "SORTIE_MODIFIER_IMPACT": "Enemy Physical Enhancement: Impact",
    "SORTIE_MODIFIER_PUNCTURE": "Enemy Physical Enhancement: Puncture",
    "SORTIE_MODIFIER_SLASH": "Enemy Physical Enhancement: Slash",
    "SORTIE_MODIFIER_SECONDARY_ONLY": "Secondary Only",
    "SORTIE_MODIFIER_RIFLE_ONLY": "Assault Rifle Only",
    "SORTIE_MODIFIER_SHOTGUN_ONLY": "Shotgun Only",
    "SORTIE_MODIFIER_SNIPER_ONLY": "Sniper Only",
    "SORTIE_MODIFIER_BOW_ONLY": "Bow Only",
    "SORTIE_MODIFIER_MELEE_ONLY": "Melee Only",
    "SORTIE_MODIFIER_HAZARD_RADIATION": "Radiation Hazard",
    "SORTIE_MODIFIER_HAZARD_MAGNETIC": "Magnetic Anomaly",
    "SORTIE_MODIFIER_HAZARD_FIRE": "Fire Hazard",
    "SORTIE_MODIFIER_HAZARD_ICE": "Cryogenic Leakage",
    "SORTIE_MODIFIER_HAZARD_COLD": "Cryogenic Leakage",
    "SORTIE_MODIFIER_HAZARD_ELECTRICITY": "Electromagnetic Anomalies",
    "SORTIE_MODIFIER_HAZARD_FOG": "Dense Fog",
}

# Invasions keep WFHelper's own faction spelling (FACTION_LABEL in the parser).
INVASION_FACTION = {"FC_GRINEER": "Grineer", "FC_CORPUS": "Corpus", "FC_INFESTATION": "Infested",
                    "FC_OROKIN": "Orokin", "FC_SENTIENT": "Sentient"}

RAW_BOUNTY_SYNDICATES = {"CetusSyndicate": "Ostrons", "SolarisSyndicate": "Solaris United",
                         "EntratiSyndicate": "Entrati"}
BOUNTY_SYNDICATES = {"Ostrons", "Solaris United", "Entrati", "The Holdfasts", "Cavia", "The Hex"}
_FIVE_STANDING = [[1000, 1500], [2000, 3000], [3000, 4500], [4000, 6000], [5000, 7500]]
BOUNTY_CYCLE_SYNDICATES = {
    "ZarimanSyndicate": {"displayName": "The Holdfasts", "standingTiers": _FIVE_STANDING,
                         "levelTiers": [[50, 55], [60, 65], [70, 75], [90, 95], [110, 115]]},
    "EntratiLabSyndicate": {"displayName": "Cavia", "standingTiers": _FIVE_STANDING,
                            "levelTiers": [[55, 60], [65, 70], [75, 80], [95, 100], [115, 120]]},
    # In-game levels; DE's drop-table labels run 10 lower (pools match by tier index).
    "HexSyndicate": {"displayName": "The Hex",
                     "standingTiers": _FIVE_STANDING + [[6000, 9000], [7500, 11250]],
                     "levelTiers": [[65, 70], [75, 80], [85, 90], [95, 100], [105, 110], [115, 120],
                                    [125, 130]]},
}

WORLD_STATE_MARKERS = ("ActiveMissions", "VoidStorms", "Invasions", "VoidTraders", "SyndicateMissions",
                       "EndlessXpSchedule")
CIRCUIT_CATEGORIES = (("EXC_NORMAL", "normal"), ("EXC_HARD", "hard"))


# ---- small helpers -----------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: float | int | None) -> str | None:
    """JS Date.toISOString() spelling, so the frontend can keep WFHelper's parsing."""
    if ms is None:
        return None
    ms = int(ms)
    dt = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def parse_iso(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def de_ms(obj: Any) -> int:
    """DE dates are {"$date": {"$numberLong": "1760000000000"}}; 0 when absent or malformed."""
    if not isinstance(obj, dict):
        return 0
    d = obj.get("$date")
    if isinstance(d, dict):
        d = d.get("$numberLong")
    try:
        return int(d) if d is not None else 0
    except (TypeError, ValueError):
        return 0


def de_date(obj: Any) -> str | None:
    ms = de_ms(obj)
    return iso(ms) if ms else None


def _oid(raw: dict) -> str:
    v = raw.get("_id")
    if isinstance(v, dict):
        return str(v.get("$oid") or "")
    return ""


def as_list(value: Any) -> list:
    """DE has shipped a scalar where an array belongs; one bad field must not cost the rest."""
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value: Any, default: float = 0) -> float:
    """JS ``Number(x) || default``: a numeric string counts, NaN/inf/0/garbage give ``default``."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        try:
            v = float(value.strip())
        except ValueError:
            return default
    else:
        return default
    return v if math.isfinite(v) and v != 0 else default


def _int(value: Any, default: int = 0) -> int:
    """``_num`` truncated to int, for counts and levels shown as whole numbers."""
    return int(_num(value, default))


def store_item_path(path: Any) -> str:
    """Store paths mirror inventory identities under an extra StoreItems segment."""
    return re.sub(r"^/Lotus/StoreItems/", "/Lotus/", path or "") if isinstance(path, str) else ""


def strip_tags(value: str) -> str:
    """Dict values embed icon tags (<SHARD_BLUE_SIMPLE>, <DT_RADIATION>) that have no glyph here."""
    return re.sub(r"\s{2,}", " ", re.sub(r"<[A-Za-z0-9_]+>", " ", value)).strip()


def prettify_path_slug(value: str) -> str:
    """".../SeasonDailyAimGlide" -> "Season Daily Aim Glide"; "Booster3DayStoreItem" -> "Booster 3 Day"."""
    slug = re.sub(r"StoreItems?$", "", (value.split("/")[-1] or value))
    slug = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", slug)
    slug = re.sub(r"([A-Za-z])(\d)", r"\1 \2", slug)
    slug = re.sub(r"(\d)([A-Za-z])", r"\1 \2", slug)
    return re.sub(r"\s{2,}", " ", slug).strip()


def _db_item_name(unique_name: str) -> str | None:
    """Item names from the bundled @wfcd item DB (stands in for WFHelper's PEP export tables)."""
    try:
        from .items import items

        item = items().by_unique(unique_name)
    except Exception:  # noqa: BLE001 - no app / no bundle: fall back to the path
        return None
    return item.name if item else None


# Injectable for tests: unique name -> display name or None.
item_name_lookup: Callable[[str], str | None] = _db_item_name


def resolve_item_name(item_path: str) -> str:
    name = item_name_lookup(item_path) if item_path else None
    if name:
        name = strip_tags(name)
        # Relic projections read "Lith G9 Intact" in the item DB; WFHelper says "Lith G9 Relic".
        if "/Projections/" in item_path:
            name = re.sub(r"\s+(Intact|Exceptional|Flawless|Radiant)$", "", name) + " Relic"
        return name
    readable = prettify_path_slug(item_path or "")
    if readable.startswith("Avatar Image"):
        return readable.replace("Avatar Image", "Glyph", 1).strip()
    return readable


def mission_type_label(mission_type: str, node_id: str) -> str:
    return solnodes.mission_type_label(mission_type or "", node_id or "")


def railjack_mission_label(node_id: str) -> str:
    n = solnodes.node(node_id)
    return n.mission if n and n.mission else "Railjack"


def sortie_modifier_label(modifier: str) -> str:
    return SORTIE_MODIFIER.get(modifier) or enum_tail(modifier or "", "SORTIE_MODIFIER_")


def boss_label(boss: str | None) -> str:
    """Empty rather than "Unknown" so a boss-less payload hides the line."""
    return enum_tail(boss, "SORTIE_BOSS_") if boss else ""


def clean_challenge_text(text: str, ally: str | None = None, count: int | None = None) -> str:
    t = text.replace("|COUNT|", str(count) if count is not None else "X")
    t = t.replace("|ALLY|", ally or "Ally")
    t = re.sub(r"\|OPEN_COLOR\|[^|]*\|CLOSE_COLOR\|\s*", "", t)
    t = re.sub(r"<DT_[A-Z_]+>", "", t)
    return t.replace("\n", " ").strip()


def _planet(node_id: str) -> str:
    n = solnodes.node(node_id)
    return n.system if n else ""


def active_entry(value: Any, now: int) -> dict | None:
    """DE ships these singly or as an array: take the live entry, else the first."""
    lst = value if isinstance(value, list) else ([value] if value else [])
    lst = [e for e in lst if isinstance(e, dict)]
    for e in lst:
        if de_ms(e.get("Expiry")) > now:
            return e
    return lst[0] if lst else None


# ---- computed parts -------------------------------------------------------------

def compute_vallis(now: int) -> dict:
    elapsed = (now - VALLIS_EPOCH_MS) % VALLIS_PERIOD_MS
    warm = elapsed < VALLIS_WARM_MS
    left = VALLIS_WARM_MS - elapsed if warm else VALLIS_PERIOD_MS - elapsed
    start = now - elapsed if warm else now - (elapsed - VALLIS_WARM_MS)
    return {"isWarm": warm, "state": "warm" if warm else "cold", "timeLeft": "",
            "activation": iso(start), "expiry": iso(now + left)}


def compute_cetus_cambion(bounty_expiry_ms: int, now: int) -> tuple[dict, dict]:
    night_start = bounty_expiry_ms - POE_NIGHT_MS
    is_day = now < night_start
    expiry = iso(night_start if is_day else bounty_expiry_ms)
    activation = iso(bounty_expiry_ms - POE_CYCLE_MS if is_day else night_start)
    cetus = {"isDay": is_day, "state": "day" if is_day else "night", "timeLeft": "",
             "activation": activation, "expiry": expiry}
    cambion = {"active": "fass" if is_day else "vome", "state": "fass" if is_day else "vome", "timeLeft": "",
               "activation": activation, "expiry": expiry}
    return cetus, cambion


def compute_duviri_mood(now: int) -> dict:
    i = math.trunc(now / DUVIRI_MOOD_PERIOD_MS)
    return {"state": DUVIRI_MOODS[i % 5], "nextState": DUVIRI_MOODS[(i + 1) % 5],
            "activation": iso(i * DUVIRI_MOOD_PERIOD_MS), "expiry": iso((i + 1) * DUVIRI_MOOD_PERIOD_MS)}


def _week_start(now: int) -> datetime:
    d = datetime.fromtimestamp(now / 1000, tz=timezone.utc)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def compute_steel_path(now: int) -> dict:
    s = (now - STEEL_PATH_EPOCH_MS) / 1000
    ind = int((s % STEEL_PATH_EIGHT_WEEKS_S) // STEEL_PATH_SEVEN_DAYS_S)
    act = _week_start(now)
    exp = act + timedelta(days=6, hours=23, minutes=59, seconds=59)
    rot = [dict(r) for r in STEEL_PATH_ROTATION]
    upcoming = []
    for off in (1, 2):
        wa = act + timedelta(days=7 * off)
        upcoming.append({**rot[(ind + off) % len(rot)], "activation": iso(_ms(wa)),
                         "expiry": iso(_ms(wa + timedelta(days=6, hours=23, minutes=59, seconds=59))),
                         "weekOffset": off})
    return {"currentReward": dict(rot[ind]), "activation": iso(_ms(act)), "expiry": iso(_ms(exp)),
            "rotation": rot, "upcoming": upcoming, "evergreens": [dict(e) for e in STEEL_PATH_EVERGREENS]}


def compute_resets(now: int) -> dict:
    """Daily reset at 00:00 UTC, weekly at Monday 00:00 UTC (buildWorldTimes)."""
    d = datetime.fromtimestamp(now / 1000, tz=timezone.utc)
    daily = d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    weekly = _week_start(now) + timedelta(days=7)
    return {"daily": iso(_ms(daily)), "weekly": iso(_ms(weekly)),
            "dailyActivation": iso(_ms(daily - timedelta(days=1))),
            "weeklyActivation": iso(_ms(weekly - timedelta(days=7)))}


def parse_circuit_choices(raw: dict | None, now: int) -> list[dict]:
    raw = raw if isinstance(raw, dict) else {}
    active = None
    for e in as_list(raw.get("EndlessXpSchedule")):
        if not isinstance(e, dict):
            continue
        frm, to = de_ms(e.get("Activation")), de_ms(e.get("Expiry"))
        if frm <= now and (not to or to > now):
            active = e
            break
    # JS: active?.CategoryChoices || raw.EndlessXpChoices (an active week without picks falls back).
    cats = as_list(active.get("CategoryChoices")) if active else []
    if not cats:
        cats = as_list(raw.get("EndlessXpChoices"))
    out = []
    for tag, category in CIRCUIT_CATEGORIES:
        entry = next((c for c in cats if isinstance(c, dict) and c.get("Category") == tag), None)
        choices = [re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", n) for n in as_list((entry or {}).get("Choices"))
                   if isinstance(n, str)]
        if choices:
            out.append({"category": category, "choices": choices})
    return out


def circuit_name_key(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*&\s*", " and ", name.lower())).strip()


def circuit_rotation_index(rotation, choices: list[str]) -> int:
    """Week with most matches, accepted only with >= half of its names; -1 otherwise."""
    have = {circuit_name_key(c) for c in choices}
    best, best_hits = -1, 0
    for i, week in enumerate(rotation):
        hits = sum(1 for n in week if circuit_name_key(n) in have)
        if hits > best_hits:
            best, best_hits = i, hits
    need = math.ceil(len(rotation[best]) / 2) if best >= 0 else 1
    return best if best_hits >= need else -1


def build_circuit(choices: list[dict]) -> dict:
    """Deck addition: current Circuit picks plus the weeks after them (rotation from this week on)."""
    out = {}
    for category, rotation in (("normal", CIRCUIT_NORMAL_ROTATION), ("hard", CIRCUIT_HARD_ROTATION)):
        live = next((c["choices"] for c in choices if c.get("category") == category), [])
        idx = circuit_rotation_index(rotation, live) if live else -1
        weeks = [list(w) for w in rotation[idx:] + rotation[:idx]] if idx >= 0 else []
        out[category] = {"choices": live, "weekIndex": idx, "weeks": weeks}
    return out


# ---- parse ----------------------------------------------------------------------------

def _parse_fissures(raw: dict, now: int) -> list[dict]:
    out = []
    for m in as_list(raw.get("ActiveMissions")):
        if not isinstance(m, dict):
            continue
        mod = m.get("Modifier") or ""
        if not (mod.startswith("VoidT") and mod in VOID_TIER):
            continue
        node_id = m.get("Node") or "Unknown"
        exp = de_ms(m.get("Expiry"))
        if exp < now:
            continue
        out.append({"id": _oid(m), "expiry": iso(exp) if exp else None, "tier": VOID_TIER[mod],
                    "missionType": mission_type_label(m.get("MissionType") or "", node_id),
                    "node": solnodes.node_label_comma(node_id), "nodeId": node_id, "planet": _planet(node_id),
                    "isHard": m.get("Hard") is True or mod.endswith("Hard"), "expired": False,
                    "activation": de_date(m.get("Activation"))})
    # Void Storms (Railjack) carry ActiveMissionTier ("VoidT3", "...Hard") and no MissionType.
    for vs in as_list(raw.get("VoidStorms")):
        if not isinstance(vs, dict):
            continue
        tier_raw = vs.get("ActiveMissionTier") or ""
        hard = tier_raw.endswith("Hard")
        tier = VOID_TIER.get(tier_raw[:-4] if hard else tier_raw)
        node_id = vs.get("Node") or "Unknown"
        exp = de_ms(vs.get("Expiry"))
        if not tier or exp < now:
            continue
        out.append({"id": _oid(vs), "expiry": iso(exp) if exp else None, "tier": tier,
                    "missionType": railjack_mission_label(node_id), "node": solnodes.node_label_comma(node_id),
                    "nodeId": node_id, "planet": _planet(node_id), "isHard": hard, "isStorm": True,
                    "expired": False, "activation": de_date(vs.get("Activation"))})
    return out


def _price(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _parse_void_trader(raw: dict) -> dict | None:
    b = raw.get("VoidTraders")
    b = b[0] if isinstance(b, list) and b else b
    if not isinstance(b, dict):
        return None
    inv = []
    for i in as_list(b.get("Manifest")):
        if not isinstance(i, dict) or "BaroTreasureBox" in (i.get("ItemType") or ""):
            continue
        un = store_item_path(i.get("ItemType"))
        row = {"uniqueName": un, "item": resolve_item_name(un)}
        if _price(i.get("PrimePrice")):
            row["ducats"] = i["PrimePrice"]
        if _price(i.get("RegularPrice")):
            row["credits"] = i["RegularPrice"]
        row["imageOverride"] = None
        inv.append(row)
    node = b.get("Node") or ""
    return {"id": _oid(b), "activation": de_date(b.get("Activation")), "expiry": de_date(b.get("Expiry")),
            "location": HUB_NODE.get(node) or node or "Unknown", "node": node, "inventory": inv}


def _parse_vault_trader(raw: dict) -> dict | None:
    v = raw.get("PrimeVaultTraders")
    v = v[0] if isinstance(v, list) and v else v
    if not isinstance(v, dict):
        return None
    inv = []
    for i in as_list(v.get("Manifest")):
        if not isinstance(i, dict):
            continue
        un = store_item_path(i.get("ItemType"))
        row = {"uniqueName": un, "item": resolve_item_name(un)}
        if _price(i.get("PrimePrice")):
            row["aya"] = i["PrimePrice"]
        if _price(i.get("RegularPrice")):
            row["regalAya"] = i["RegularPrice"]
        inv.append(row)
    node = v.get("Node") or ""
    return {"activation": de_date(v.get("Activation")), "expiry": de_date(v.get("Expiry")),
            "location": HUB_NODE.get(node) or node or "Varzia", "inventory": inv}


def _parse_nightwave_challenge(c: dict) -> dict:
    path = c.get("Challenge") or ""
    info = solnodes.challenge(path)
    count = info[3] if info else 0
    title = info[0] if info else (prettify_path_slug(path) or "Unknown")
    desc_t = info[1] if info else ""
    standing = info[2] if info else None
    return {
        "id": _oid(c),
        "name": path.split("/")[-1] if path else "",
        "title": strip_tags(title),
        "description": clean_challenge_text(desc_t, None, count) if desc_t else (strip_tags(title) if info else ""),
        "standing": standing,
        "requiredCount": count,
        "isDaily": c.get("Daily") is True,
        "isElite": "/WeeklyHard/" in path,
        "activation": de_date(c.get("Activation")),
        "expiry": de_date(c.get("Expiry")),
    }


def _calendar_label(value: str) -> str:
    return title_case(value) if re.search(r"[A-Za-z]", value) and value == value.upper() else value


def _parse_calendar_event(ev: dict) -> dict | None:
    if ev.get("challenge"):
        info = solnodes.challenge(ev["challenge"])
        if not info:
            return {"kind": "challenge", "label": prettify_path_slug(ev["challenge"])}
        title, desc, _standing, count = info
        out = {"kind": "challenge", "label": _calendar_label(clean_challenge_text(title, None, count or None))}
        if desc:
            out["description"] = clean_challenge_text(desc, None, count or None)
        return out
    if ev.get("reward"):
        un = store_item_path(ev["reward"])
        return {"kind": "reward", "label": _calendar_label(resolve_item_name(un)), "uniqueName": un}
    if ev.get("upgrade"):
        return {"kind": "upgrade", "label": prettify_path_slug(ev["upgrade"])}
    return None


def _parse_alert(a: dict) -> dict:
    info = _as_dict(a.get("MissionInfo"))
    node_id = info.get("location") or ""
    reward = _as_dict(info.get("missionReward"))
    items = [{"name": resolve_item_name(store_item_path(c.get("ItemType"))), "count": _int(c.get("ItemCount"), 1)}
             for c in as_list(reward.get("countedItems")) if isinstance(c, dict)]
    items += [{"name": resolve_item_name(store_item_path(p)), "count": 1}
              for p in as_list(reward.get("items")) if isinstance(p, str)]
    return {"id": _oid(a), "activation": de_date(a.get("Activation")), "expiry": de_date(a.get("Expiry")),
            "node": solnodes.node_label(node_id), "nodeId": node_id,
            "mission": mission_type_label(info.get("missionType") or "", node_id),
            "faction": solnodes.faction_label(info.get("faction")),
            "minLevel": _int(info.get("minEnemyLevel")), "maxLevel": _int(info.get("maxEnemyLevel")),
            "credits": _int(reward.get("credits")), "items": items}


def _invasion_reward(reward: Any) -> dict:
    if not isinstance(reward, dict):
        return {"items": [], "countedItems": [], "credits": 0}
    counted = [{"count": _int(c.get("ItemCount"), 1), "type": resolve_item_name(store_item_path(c.get("ItemType")))}
               for c in as_list(reward.get("countedItems")) if isinstance(c, dict)]
    return {"items": [], "countedItems": counted, "credits": _int(reward.get("credits"))}


def _parse_invasions(raw: dict) -> list[dict]:
    out = []
    for inv in as_list(raw.get("Invasions")):
        if not isinstance(inv, dict) or inv.get("Completed"):
            continue
        goal = inv.get("Goal") or 0
        count = inv.get("Count") or 0
        try:
            completion = round(count / goal * 1000) / 10 if goal > 0 else 0
        except TypeError:
            completion = 0
        node_id = inv.get("Node") or ""
        atk, dfn = inv.get("Faction") or "", inv.get("DefenderFaction") or ""
        out.append({
            "id": _oid(inv), "node": solnodes.node_label_comma(node_id), "nodeId": node_id,
            "planet": _planet(node_id), "desc": inv.get("LocTag") and prettify_path_slug(inv["LocTag"]) or "",
            "attacker": {"reward": _invasion_reward(inv.get("AttackerReward")), "faction": INVASION_FACTION.get(atk, atk)},
            "defender": {"reward": _invasion_reward(inv.get("DefenderReward")), "faction": INVASION_FACTION.get(dfn, dfn)},
            "vsInfestation": atk == "FC_INFESTATION",
            "completion": max(0.0, min(100.0, abs(completion))),
            "count": count, "goal": goal, "completed": False,
            "activation": de_date(inv.get("Activation")),
        })
    return out


def _parse_raw_bounties(raw: dict, now: int) -> list[dict]:
    out = []
    for sm in as_list(raw.get("SyndicateMissions")):
        if not isinstance(sm, dict):
            continue
        name = RAW_BOUNTY_SYNDICATES.get(sm.get("Tag") or "")
        jobs = as_list(sm.get("Jobs"))
        if not name or de_ms(sm.get("Expiry")) <= now or not jobs:
            continue
        parsed = []
        for j in jobs:
            if not isinstance(j, dict) or not j.get("jobType"):
                continue
            slug = (j.get("jobType") or "").split("/")[-1] or "Unknown"
            typ = re.sub(r"([a-z])([A-Z])", r"\1 \2", re.sub(r"Bounty.*", " Bounty", slug)).strip() or "Unknown"
            parsed.append({"type": typ, "enemyLevels": [j.get("minEnemyLevel") or 0, j.get("maxEnemyLevel") or 0],
                           "standingStages": as_list(j.get("xpAmounts")), "minMR": j.get("masteryReq") or 0})
        out.append({"syndicate": name, "syndicateKey": sm["Tag"], "expiry": de_date(sm.get("Expiry")),
                    "jobs": parsed})
    return out


def parse_raw(raw: dict | None, now: int | None = None) -> dict | None:
    """DE world state -> WFHelper WorldState shape. Cycles are filled in by fetch_and_parse."""
    if not isinstance(raw, dict):
        return None
    now = now_ms() if now is None else now

    sortie_raw = active_entry(raw.get("Sorties"), now)
    sortie = None
    if sortie_raw:
        sortie = {"id": _oid(sortie_raw), "activation": de_date(sortie_raw.get("Activation")),
                  "expiry": de_date(sortie_raw.get("Expiry")), "boss": boss_label(sortie_raw.get("Boss")),
                  "missions": [{"node": solnodes.node_label(v.get("node") or ""),
                                "mission": mission_type_label(v.get("missionType") or "", v.get("node") or ""),
                                "modifier": sortie_modifier_label(v.get("modifierType") or "")}
                               for v in as_list(sortie_raw.get("Variants")) if isinstance(v, dict)]}

    archon_raw = active_entry(raw.get("LiteSorties"), now)
    archon = None
    if archon_raw:
        archon = {"id": _oid(archon_raw), "activation": de_date(archon_raw.get("Activation")),
                  "expiry": de_date(archon_raw.get("Expiry")), "boss": boss_label(archon_raw.get("Boss")),
                  "missions": [{"node": solnodes.node_label(m.get("node") or ""),
                                "mission": mission_type_label(m.get("missionType") or "", m.get("node") or "")}
                               for m in as_list(archon_raw.get("Missions")) if isinstance(m, dict)]}

    season = raw.get("SeasonInfo")
    nightwave = None
    if isinstance(season, dict):
        acts = [c for c in as_list(season.get("ActiveChallenges")) if isinstance(c, dict)]
        # DE leaves finished acts in ActiveChallenges; a missing expiry keeps the act.
        acts = [c for c in acts if (lambda e: e == 0 or e > now)(de_ms(c.get("Expiry")))]
        nightwave = {"activation": de_date(season.get("Activation")), "expiry": de_date(season.get("Expiry")),
                     "season": _int(season.get("Season")), "phase": _int(season.get("Phase")),
                     "affiliationTag": season.get("AffiliationTag") or "",
                     "challenges": [_parse_nightwave_challenge(c) for c in acts]}

    alerts = [_parse_alert(a) for a in as_list(raw.get("Alerts"))
              if isinstance(a, dict) and de_ms(a.get("Expiry")) > now]

    descent_arr = [d for d in as_list(raw.get("Descents")) if isinstance(d, dict)]
    descent = next((d for d in descent_arr if de_ms(d.get("Activation")) <= now < de_ms(d.get("Expiry"))),
                   descent_arr[0] if descent_arr else None)

    cal_raw = active_entry(raw.get("KnownCalendarSeasons"), now)
    calendar = None
    if cal_raw:
        days = []
        for day in as_list(cal_raw.get("Days")):
            if not isinstance(day, dict):
                continue
            evs = [_parse_calendar_event(e) for e in as_list(day.get("events")) if isinstance(e, dict)]
            evs = [e for e in evs if e and e.get("label")]
            if evs:
                days.append({"day": _int(day.get("day")), "events": evs})
        s = re.sub(r"^CST_", "", cal_raw.get("Season") or "").lower()
        calendar = {"activation": de_date(cal_raw.get("Activation")), "expiry": de_date(cal_raw.get("Expiry")),
                    "days": days, "season": s[:1].upper() + s[1:]}

    deals = []
    for d in as_list(raw.get("DailyDeals")):
        if not isinstance(d, dict) or de_ms(d.get("Expiry")) <= now:
            continue
        un = store_item_path(d.get("StoreItem"))
        deals.append({"uniqueName": un, "item": resolve_item_name(un), "imageOverride": None,
                      "discount": d.get("Discount") or 0, "originalPrice": d.get("OriginalPrice") or 0,
                      "salePrice": d.get("SalePrice") or 0, "total": d.get("AmountTotal") or 0,
                      "sold": d.get("AmountSold") or 0, "expiry": de_date(d.get("Expiry"))})

    return {
        "fissures": _parse_fissures(raw, now),
        "voidTrader": _parse_void_trader(raw),
        "vaultTrader": _parse_vault_trader(raw),
        "sortie": sortie,
        "archonHunt": archon,
        "nightwave": nightwave,
        "descents": {"activation": de_date(descent.get("Activation")), "expiry": de_date(descent.get("Expiry"))}
        if descent else None,
        "calendarSeason": calendar,
        "alerts": alerts,
        "steelPath": compute_steel_path(now),
        "duviriCycle": {"state": None, "expiry": de_date(descent.get("Expiry")) if descent else None,
                        "choices": parse_circuit_choices(raw, now)},
        "earthCycle": None,
        "cetusCycle": None,
        "vallisCycle": None,
        "cambionCycle": None,
        "zarimanCycle": None,
        "invasions": _parse_invasions(raw),
        "bounties": _parse_raw_bounties(raw, now),
        "dailyDeals": deals,
    }


def empty_world_state(now: int | None = None) -> dict:
    now = now_ms() if now is None else now
    return {"fissures": [], "voidTrader": None, "vaultTrader": None, "sortie": None, "archonHunt": None,
            "nightwave": None, "descents": None, "calendarSeason": None, "alerts": [],
            "steelPath": compute_steel_path(now), "duviriCycle": None, "earthCycle": None, "cetusCycle": None,
            "vallisCycle": compute_vallis(now), "cambionCycle": None, "zarimanCycle": None, "invasions": [],
            "bounties": [], "dailyDeals": [], "resets": compute_resets(now), "circuit": build_circuit([])}


# ---- enrichment payloads ------------------------------------------------------------

def parse_bounty_cycle_bounties(data: dict) -> list[dict]:
    """Seed-generated Zariman/Cavia/Hex jobs from the oracle (not in DE's payload)."""
    bounties = data.get("bounties") if isinstance(data, dict) else None
    if not isinstance(bounties, dict):
        return []
    exp = _num(data.get("expiry"))
    expiry = iso(exp) if exp > 0 else None
    out = []
    for key, jobs in bounties.items():
        cfg = BOUNTY_CYCLE_SYNDICATES.get(key)
        if not cfg or not isinstance(jobs, list) or not jobs:
            continue
        parsed = []
        for i, job in enumerate(jobs):
            job = _as_dict(job)
            node_id = job.get("node") or ""
            n = solnodes.node(node_id)
            mtype = mission_type_label(n.mission_type, node_id) if n and n.mission_type else "Unknown"
            levels = cfg["levelTiers"][i] if i < len(cfg["levelTiers"]) else [n.min_level if n else 0,
                                                                               n.max_level if n else 0]
            pair = cfg["standingTiers"][i] if i < len(cfg["standingTiers"]) else []
            parsed.append({"type": mtype, "enemyLevels": list(levels), "tierIndex": i,
                           "standingStages": [pair[0]] if pair else [], "minMR": 0})
        out.append({"syndicate": cfg["displayName"], "syndicateKey": key, "expiry": expiry, "jobs": parsed})
    return out


def _wfstat_earth(data: dict) -> dict | None:
    e = data.get("earthCycle") if isinstance(data.get("earthCycle"), dict) else None
    if not e:
        return None
    exp = parse_iso(e.get("expiry"))
    if exp is None:
        return None
    is_day = e.get("isDay")
    if not isinstance(is_day, bool):
        st = str(e.get("state") or e.get("timeOfDay") or "").lower()
        is_day = True if st == "day" else False if st == "night" else None
    if is_day is None:
        return None
    return {"isDay": is_day, "state": "day" if is_day else "night",
            "timeLeft": e.get("timeLeft") if isinstance(e.get("timeLeft"), str) else "", "expiry": iso(exp),
            "activation": iso(parse_iso(e.get("activation"))) if parse_iso(e.get("activation")) else None}


def _wfstat_zariman(data: dict) -> dict | None:
    z = data.get("zarimanCycle") if isinstance(data.get("zarimanCycle"), dict) else None
    if not z:
        return None
    exp = parse_iso(z.get("expiry"))
    if exp is None:
        return None
    corpus = z.get("isCorpus")
    if not isinstance(corpus, bool):
        corpus = str(z.get("state") or "").lower() == "corpus"
    return {"isCorpus": corpus, "state": "corpus" if corpus else "grineer", "expiry": iso(exp),
            "activation": iso(parse_iso(z.get("activation"))) if parse_iso(z.get("activation")) else None}


def _wfstat_invasions(data: dict) -> list[dict]:
    out = []
    for inv in as_list(data.get("invasions")):
        if not isinstance(inv, dict) or inv.get("completed"):
            continue

        def side(s: Any) -> dict:
            s = _as_dict(s)
            r = _as_dict(s.get("reward"))
            return {"reward": {"items": as_list(r.get("items")), "countedItems": as_list(r.get("countedItems")),
                               "credits": r.get("credits") or 0}, "faction": s.get("faction") or "Unknown"}

        c = inv.get("completion")
        out.append({"id": inv.get("id") or "", "node": inv.get("node") or "Unknown", "desc": inv.get("desc") or "",
                    "attacker": side(inv.get("attacker")), "defender": side(inv.get("defender")),
                    "vsInfestation": bool(inv.get("vsInfestation")),
                    "completion": round(c * 10) / 10 if isinstance(c, (int, float)) else 0, "completed": False})
    return out


def _wfstat_bounties(data: dict) -> list[dict]:
    out = []
    for sm in as_list(data.get("syndicateMissions")):
        if not isinstance(sm, dict) or sm.get("syndicate") not in BOUNTY_SYNDICATES or not as_list(sm.get("jobs")):
            continue
        jobs = []
        for j in as_list(sm.get("jobs")):
            j = _as_dict(j)
            lv = j.get("enemyLevels")
            jobs.append({"type": j.get("type") or "Unknown",
                         "enemyLevels": [lv[0] if lv else 0, lv[1] if len(lv or []) > 1 else 0]
                         if isinstance(lv, list) else [0, 0],
                         "standingStages": as_list(j.get("standingStages")), "minMR": j.get("minMR") or 0})
        out.append({"syndicate": sm.get("syndicate") or "", "syndicateKey": sm.get("syndicateKey") or "",
                    "expiry": sm.get("expiry"), "jobs": jobs})
    return out


def _wfstat_circuit(data: dict) -> list[dict]:
    d = _as_dict(data.get("duviriCycle"))
    out = []
    for e in as_list(d.get("choices")):
        e = _as_dict(e)
        ch = [c for c in as_list(e.get("choices")) if isinstance(c, str)]
        if isinstance(e.get("category"), str) and e["category"] and ch:
            out.append({"category": e["category"], "choices": ch})
    return out


# ---- fetching ------------------------------------------------------------------------

_host_failed: dict[str, float] = {}


def _http_json(url: str, timeout: float) -> Any:
    # One attempt per URL like WFHelper: net.fetch's transport retries would triple every timeout.
    return json.loads(net.fetch(url, headers={"Accept": "application/json"}, timeout=timeout,
                                retry_network=False))


# Injectable for tests: (url, timeout) -> parsed JSON; raise on failure.
fetch_json: Callable[[str, float], Any] = _http_json


def _enrich(url: str) -> Any:
    """Optional source: skipped for HOST_BACKOFF after a failure; None on failure."""
    if time.monotonic() < _host_failed.get(url, 0):
        return None
    try:
        return fetch_json(url, CYCLE_FETCH_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        _host_failed[url] = time.monotonic() + HOST_BACKOFF
        _log.warning("world state enrichment %s failed: %s", url, e)
        return None


def is_world_state_payload(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in WORLD_STATE_MARKERS)


def fetch_raw() -> tuple[dict, str]:
    """DE first (two paths), then the browse.wf oracle; raises when all fail.

    The sources share FETCH_BUDGET so three timeouts in a row still answer inside the RPC timeout.
    """
    deadline = time.monotonic() + FETCH_BUDGET

    def timeout() -> float:
        left = deadline - time.monotonic()
        if left < 1:
            raise TimeoutError("world-state fetch budget exhausted")
        return min(FETCH_TIMEOUT, left)

    for i, url in enumerate(FETCH_URLS):
        try:
            raw = fetch_json(url, timeout())
        except Exception as e:  # noqa: BLE001
            _log.warning("world state %s failed: %s", url, e)
            continue
        if is_world_state_payload(raw):
            return raw, "de" if i == 0 else "de-legacy"
        _log.warning("world state %s returned an invalid payload", url)
    try:
        raw = fetch_json(ORACLE_WORLDSTATE_URL, timeout())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"every world-state source failed: {e}") from e
    if not is_world_state_payload(raw):
        raise RuntimeError("every world-state source failed: oracle returned an invalid payload")
    return raw, "oracle"


def fetch_and_parse(enrich: bool = True, now: Callable[[], int] | None = None) -> dict:
    now = now or (lambda: now_ms())
    raw, source = fetch_raw()
    parsed = parse_raw(raw, now())
    if parsed is None:
        raise RuntimeError("world-state payload could not be parsed")

    oracle, wfstat = None, None
    if enrich:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="wfh-ws") as ex:
            f_oracle = ex.submit(_enrich, ORACLE_BOUNTY_CYCLE_URL)
            f_wfstat = ex.submit(_enrich, WARFRAMESTAT_URL)
            oracle, wfstat = f_oracle.result(), f_wfstat.result()
    oracle = oracle if isinstance(oracle, dict) else {}
    wfstat = wfstat if isinstance(wfstat, dict) else {}
    t = now()
    return merge(parsed, raw, oracle, wfstat, t, source)


def merge(parsed: dict, raw: dict, oracle: dict, wfstat: dict, now: int, source: str = "de") -> dict:
    """Cycles + enrichment over parse_raw output (worldStateParser.fetchAndParse merge rules)."""
    out = dict(parsed)
    out["vallisCycle"] = compute_vallis(now)

    # Cetus/Cambion: oracle bounty-cycle expiry, else DE's own Cetus bounty expiry (same boundary),
    # else warframestat's cetusCycle.
    cetus = cambion = None
    # Each step also replaces an expiry already in the past (a stale oracle answer).
    bounty_expiry = _num(oracle.get("expiry"))
    if not bounty_expiry or bounty_expiry <= now:
        cs = next((sm for sm in as_list(raw.get("SyndicateMissions"))
                   if isinstance(sm, dict) and sm.get("Tag") == "CetusSyndicate" and de_ms(sm.get("Expiry")) > now),
                  None)
        bounty_expiry = de_ms(cs.get("Expiry")) if cs else 0
    if not bounty_expiry or bounty_expiry <= now:
        wc = _as_dict(wfstat.get("cetusCycle"))
        exp = parse_iso(wc.get("expiry"))
        if exp and isinstance(wc.get("isDay"), bool):
            # Day ends POE_NIGHT_MS before the bounty expiry; night ends at it.
            bounty_expiry = exp + POE_NIGHT_MS if wc["isDay"] else exp
    if bounty_expiry and bounty_expiry > now:
        cetus, cambion = compute_cetus_cambion(int(bounty_expiry), now)
    out["cetusCycle"], out["cambionCycle"] = cetus, cambion
    # Kept (and stripped by state()) so a stale cache can roll Cetus/Cambion forward.
    out["_bountyExpiry"] = int(bounty_expiry) if cetus else None

    earth = _wfstat_earth(wfstat)
    out["_earthFromCetus"] = not earth and bool(cetus)   # popped by state(); see _stale_view
    if not earth and cetus:
        earth = {"isDay": cetus["isDay"], "state": cetus["state"], "timeLeft": "", "expiry": cetus["expiry"],
                 "activation": cetus.get("activation")}
    out["earthCycle"] = earth

    zariman = _wfstat_zariman(wfstat)
    if not zariman and oracle.get("zarimanFaction") and _num(oracle.get("expiry")) > now:
        corpus = str(oracle["zarimanFaction"]).upper() == "FC_CORPUS"
        zariman = {"isCorpus": corpus, "state": "corpus" if corpus else "grineer", "expiry": iso(bounty_expiry),
                   "activation": None}
    out["zarimanCycle"] = zariman

    choices = (parsed.get("duviriCycle") or {}).get("choices") or _wfstat_circuit(wfstat)
    out["duviriCycle"] = {**(parsed.get("duviriCycle") or {}), **compute_duviri_mood(now), "choices": choices}
    out["circuit"] = build_circuit(choices)
    out["steelPath"] = compute_steel_path(now)
    out["resets"] = compute_resets(now)

    if not out.get("invasions"):
        out["invasions"] = _wfstat_invasions(wfstat)

    # Bounties: raw first, warframestat fills gaps by display name, oracle seeds the rest.
    raw_b = parsed.get("bounties") or []
    raw_names = {RAW_BOUNTY_SYNDICATES.get(b.get("syndicateKey") or "") for b in raw_b}
    merged: dict[str, dict] = {}
    for b in raw_b:
        if b.get("syndicateKey"):
            merged[b["syndicateKey"]] = b
    for b in _wfstat_bounties(wfstat):
        k = b.get("syndicateKey")
        if k and k not in raw_names and k not in merged:
            merged[k] = b
    for b in parse_bounty_cycle_bounties(oracle):
        if b["syndicateKey"] not in merged:
            merged[b["syndicateKey"]] = b
    out["bounties"] = list(merged.values())
    out["bountyRotation"] = oracle.get("rot") or None
    out["source"] = source
    return out


# ---- cache ---------------------------------------------------------------------------

_cache: dict | None = None
_cache_at = 0.0            # monotonic seconds of the last good fetch
_bounty_expiry: int | None = None   # Cetus bounty boundary behind the cached cetusCycle
_earth_from_cetus = False  # cached earthCycle was derived from Cetus (no warframestat)
_fail_until = 0.0          # monotonic; refreshes are skipped until then after a failure
_last_error: str | None = None
_refresh_lock = threading.Lock()


def _enrich_enabled() -> bool:
    try:
        from ..app import get_app

        return bool(get_app().settings.get("world.enrich", True))
    except Exception:  # noqa: BLE001
        return True


def _decorate(ws: dict, stale: bool, refreshing: bool = False) -> dict:
    out = dict(ws)
    out["stale"] = stale
    out["error"] = _last_error
    if refreshing:
        out["refreshing"] = True
    return out


def _expired(cycle: Any, t: int) -> bool:
    exp = parse_iso(cycle.get("expiry")) if isinstance(cycle, dict) else None
    return exp is not None and exp <= t


def _stale_view(cached: dict, t: int) -> dict:
    """Last good state with every clock-derived part moved to ``t``.

    Cetus/Cambion (and an Earth cycle derived from Cetus) roll the cached bounty boundary forward
    in 150-min steps; other cycles whose expiry has passed are dropped rather than shown wrong.
    """
    ws = dict(cached)
    ws.update({"vallisCycle": compute_vallis(t), "steelPath": compute_steel_path(t), "resets": compute_resets(t)})
    if ws.get("duviriCycle"):
        ws["duviriCycle"] = {**ws["duviriCycle"], **compute_duviri_mood(t)}
    if _bounty_expiry:
        b = _bounty_expiry
        if b <= t:
            b += math.ceil((t - b + 1) / POE_CYCLE_MS) * POE_CYCLE_MS
        cetus, cambion = compute_cetus_cambion(b, t)
        ws["cetusCycle"], ws["cambionCycle"] = cetus, cambion
        if _earth_from_cetus:
            ws["earthCycle"] = {"isDay": cetus["isDay"], "state": cetus["state"], "timeLeft": "",
                                "expiry": cetus["expiry"], "activation": cetus.get("activation")}
    for key in ("cetusCycle", "cambionCycle", "earthCycle", "zarimanCycle"):
        if _expired(ws.get(key), t):
            ws[key] = None
    return ws


def state(max_age: float = 60) -> dict:
    """The current world state, refetched when older than ``max_age`` seconds.

    Single flight and non-blocking: while another caller refreshes, the cached state (or the
    empty one) comes back at once with ``refreshing: True`` instead of parking a pool thread.
    After a failed refresh nothing is refetched for FAIL_BACKOFF seconds unless ``max_age`` is 0
    (an explicit refresh). On failure the last good state is returned with ``stale: True`` and
    clocks moved forward (see ``_stale_view``); with nothing cached ``empty_world_state()`` with
    ``error`` set. Blocking: call through ``app.run_blocking`` from async code.
    """
    global _cache, _cache_at, _bounty_expiry, _earth_from_cetus, _fail_until, _last_error
    if _cache is not None and time.monotonic() - _cache_at < max_age:
        return _decorate(_cache, False)
    if max_age > 0 and time.monotonic() < _fail_until:
        return _fallback()
    if not _refresh_lock.acquire(blocking=False):
        return _fallback(refreshing=True)
    try:
        if _cache is not None and time.monotonic() - _cache_at < max_age:
            return _decorate(_cache, False)  # another caller refreshed just before us
        try:
            ws = fetch_and_parse(_enrich_enabled())
        except Exception as e:  # noqa: BLE001
            _last_error = f"{type(e).__name__}: {e}"
            _fail_until = time.monotonic() + FAIL_BACKOFF
            _log.warning("world state refresh failed: %s", _last_error)
            return _fallback()
        ws["fetchedAt"] = now_ms()
        _bounty_expiry = ws.pop("_bountyExpiry", None)
        _earth_from_cetus = bool(ws.pop("_earthFromCetus", False))
        _cache, _cache_at, _last_error, _fail_until = ws, time.monotonic(), None, 0.0
        return _decorate(ws, False)
    finally:
        _refresh_lock.release()


def _fallback(refreshing: bool = False) -> dict:
    """What state() answers without a fresh fetch: the stale view of the cache, else empty."""
    cached = _cache
    if cached is not None:
        if refreshing and time.monotonic() - _cache_at < RPC_MAX_AGE:
            return _decorate(cached, False, refreshing)
        return _decorate(_stale_view(cached, now_ms()), True, refreshing)
    ws = empty_world_state()
    ws["fetchedAt"] = None
    return _decorate(ws, True, refreshing)


def reset_cache() -> None:
    global _cache, _cache_at, _bounty_expiry, _earth_from_cetus, _fail_until, _last_error
    with _refresh_lock:
        _cache, _cache_at, _bounty_expiry, _fail_until, _last_error = None, 0.0, None, 0.0, None
        _earth_from_cetus = False
    _host_failed.clear()


def arbi_schedule(days: float | None = None) -> list[dict]:
    """Upcoming arbitrations ``[{epochMs, nodeId, node, mission, faction, key}]`` (see arbi_schedule.py)."""
    return _arbi.schedule(days)


# ---- module wiring -----------------------------------------------------------------

def setup(app) -> None:
    app.services["worldstate"] = state
    _arbi.setup(app)


@rpc.method("world.state")
async def _rpc_state(force: bool = False) -> dict:
    from ..app import get_app

    return await get_app().run_blocking(state, 0 if force else RPC_MAX_AGE)
