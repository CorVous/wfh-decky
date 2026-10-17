"""Star chart node, mission type and challenge labels (English) from DE's own data.

WFHelper resolves node ids ("SolNode149") through the bundled
``warframe-public-export-plus`` package (``ExportRegions.json`` +
``dict.en.json``, ~4 MB). The Deck only needs the few hundred labels the world
state and arbitration schedule print, so this module carries a precomputed
table instead (docs/port/arbitration.md §5.2 suggests exactly this).

Regenerate after a game update that adds nodes:

    curl -sSfLO https://registry.npmjs.org/warframe-public-export-plus/-/warframe-public-export-plus-<v>.tgz
    tar -xzf warframe-public-export-plus-<v>.tgz
    PYTHONPATH=py_modules bin/python/bin/python3 -m wfh.data.solnodes build package

which rewrites the block between the GENERATED markers below.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Only Infestation and Orokin differ from the FC_ tail fallback; in game they read
# as Infested and Corrupted (services/regionNames.ts FACTION_LABELS).
FACTION_LABELS: dict[str, str] = {
    "FC_GRINEER": "Grineer",
    "FC_CORPUS": "Corpus",
    "FC_INFESTATION": "Infested",
    "FC_OROKIN": "Corrupted",
    "FC_SENTIENT": "Sentient",
    "FC_NARMER": "Narmer",
}

# Fallback MT_ labels for enums DE's ExportMissionTypes leaves unnamed
# (config/shared/missionTypes.ts MISSION_TYPE_LABELS, verbatim).
MISSION_TYPE_FALLBACK: dict[str, str] = {
    "MT_ALCHEMY": "Alchemy",
    "MT_ARENA": "Arena",
    "MT_ARMAGEDDON": "Void Armageddon",
    "MT_ARTIFACT": "Disruption",
    "MT_ASSASSINATION": "Assassination",
    "MT_ASSAULT": "Assault",
    "MT_CAPTURE": "Capture",
    "MT_CORRUPTION": "Void Flood",
    "MT_COUNTER_INTEL": "Deception",
    "MT_DEFENSE": "Defense",
    "MT_ENDLESS_CAPTURE": "Legacyte Harvest",
    "MT_ENDLESS_EXTERMINATION": "Sanctuary Onslaught",
    "MT_EVACUATION": "Defection",
    "MT_EXCAVATE": "Excavation",
    "MT_EXTERMINATION": "Exterminate",
    "MT_HIVE": "Hive",
    "MT_INTEL": "Spy",
    "MT_JUNCTION": "Solar Rail Junction",
    "MT_LANDSCAPE": "Free Roam",
    "MT_MOBILE_DEFENSE": "Mobile Defense",
    "MT_PURIFY": "Infested Salvage",
    "MT_PURSUIT": "Pursuit",
    "MT_PVP": "Conclave",
    "MT_RACE": "Rush",
    "MT_RAILJACK": "Railjack",
    "MT_RESCUE": "Rescue",
    "MT_RETRIEVAL": "Hijack",
    "MT_SABOTAGE": "Sabotage",
    "MT_SURVIVAL": "Survival",
    "MT_TERRITORY": "Interception",
    "MT_VAULTS": "Netracell",
    "MT_VOID_CASCADE": "Void Cascade",
}


def title_case(value: str) -> str:
    """config/shared/textNormalize.ts titleCase: lower, then upper after start/space/-."""
    return re.sub(r"(^|[\s-])(\S)", lambda m: m.group(1) + m.group(2).upper(), value.lower())


def enum_tail(value: str, prefix: str) -> str:
    """SORTIE_BOSS_VAY_HEK -> "Vay Hek"."""
    tail = value[len(prefix):] if value.startswith(prefix) else value
    return title_case(tail.replace("_", " ")) if tail else "Unknown"


class Node:
    __slots__ = ("node_id", "name", "system", "mission_type", "mission", "faction", "min_level", "max_level")

    def __init__(self, node_id: str, row: tuple):
        self.node_id = node_id
        (self.name, self.system, self.mission_type, self.mission, self.faction,
         self.min_level, self.max_level) = row

    @property
    def label(self) -> str:
        return f"{self.name} ({self.system})" if self.system else self.name


def node(node_id: str) -> Node | None:
    row = NODES.get(node_id or "")
    return Node(node_id, row) if row else None


def node_label(node_id: str) -> str:
    """"Casta (Ceres)"; the raw id when DE has no entry; "Unknown" for an empty id."""
    if not node_id:
        return "Unknown"
    n = node(node_id)
    return n.label if n else node_id


def node_label_comma(node_id: str) -> str:
    """WFHelper's fissure/invasion spelling "Casta, Ceres" (formatNodeLabel)."""
    if not node_id:
        return "Unknown"
    n = node(node_id)
    if not n:
        return node_id
    return f"{n.name}, {n.system}" if n.system else n.name


def node_mission(node_id: str) -> str:
    """missionLabel(): the node's own mission name, else its MT_ tail, else Unknown."""
    n = node(node_id)
    if n and n.mission:
        return n.mission
    mt = n.mission_type if n else ""
    if mt.startswith("MT_"):
        return title_case(mt[3:].replace("_", " "))
    return "Unknown"


def node_faction(node_id: str) -> str:
    n = node(node_id)
    return faction_label(n.faction if n else "")


def faction_label(faction: str | None) -> str:
    fc = faction if isinstance(faction, str) else ""
    if fc in FACTION_LABELS:
        return FACTION_LABELS[fc]
    if fc.startswith("FC_"):
        return title_case(fc[3:].replace("_", " "))
    return "Unknown"


def mission_type_label(mission_type: str, node_id: str = "") -> str:
    """resolveMissionTypeLabel: DE's MT_ table, the node's mission, the fallback map, the tail."""
    if mission_type in MISSION_TYPES:
        return MISSION_TYPES[mission_type]
    n = node(node_id)
    if n and n.mission:
        return n.mission
    return MISSION_TYPE_FALLBACK.get(mission_type) or enum_tail(mission_type or "", "MT_")


def challenge(path: str) -> tuple[str, str, int | None, int] | None:
    """(title, description template, standing, required count) for a challenge path."""
    return CHALLENGES.get(path or "")


# ---- table build ----------------------------------------------------------

_BEGIN = "# --- GENERATED BEGIN from warframe-public-export-plus 0.6.12 (python -m wfh.data.solnodes build)
# fmt: off
# node id: (name, system, MT_ type, mission label, faction, min level, max level)
NODES: dict[str, tuple[str, str, str, str, str, int, int]] = {
    '1999Hub': ('Höllvania Central Mall', 'Höllvania', 'MT_PVP', 'Conclave', '', 0, 0),
    'CeresToJupiterJunction': ('JUPITER JUNCTION', 'Deimos', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'CetusHub4': ('Cetus', 'Earth', 'MT_PVP', 'Conclave', '', 0, 0),
    'ClanNode0': ('Romula', 'Venus', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 8, 18),
    'ClanNode1': ('Malva', 'Venus', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 8, 18),
    'ClanNode10': ('Memphis', 'Phobos', 'MT_EVACUATION', 'Defection', 'FC_INFESTATION', 15, 25),
    'ClanNode11': ('Zeugma', 'Phobos', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 15, 25),
    'ClanNode12': ('Caracol', 'Saturn', 'MT_EVACUATION', 'Defection', 'FC_INFESTATION', 26, 36),
    'ClanNode13': ('Piscinas', 'Saturn', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 26, 36),
    'ClanNode14': ('Amarna', 'Sedna', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 35, 45),
    'ClanNode15': ('Sangeru', 'Sedna', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 35, 45),
    'ClanNode16': ('Ur', 'Uranus', 'MT_ARTIFACT', 'Disruption', 'FC_INFESTATION', 30, 35),
    'ClanNode17': ('Assur', 'Uranus', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 25, 35),
    'ClanNode18': ('Akkad', 'Eris', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 35, 45),
    'ClanNode19': ('Zabala', 'Eris', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 35, 45),
    'ClanNode2': ('Coba', 'Earth', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 6, 16),
    'ClanNode20': ('Yursa', 'Neptune', 'MT_EVACUATION', 'Defection', 'FC_INFESTATION', 30, 40),
    'ClanNode21': ('Kelashin', 'Neptune', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 30, 40),
    'ClanNode22': ('Seimeni', 'Ceres', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 15, 25),
    'ClanNode23': ('Gabii', 'Ceres', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 15, 25),
    'ClanNode24': ('Sechura', 'Pluto', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 35, 45),
    'ClanNode25': ('Hieracon', 'Pluto', 'MT_EXCAVATE', 'Excavation', 'FC_INFESTATION', 35, 45),
    'ClanNode3': ('Tikal', 'Earth', 'MT_EXCAVATE', 'Excavation', 'FC_INFESTATION', 6, 16),
    'ClanNode4': ('Sinai', 'Jupiter', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 20, 30),
    'ClanNode5': ('Cameria', 'Jupiter', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 20, 30),
    'ClanNode6': ('Larzac', 'Europa', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 23, 33),
    'ClanNode7': ('Cholistan', 'Europa', 'MT_EXCAVATE', 'Excavation', 'FC_INFESTATION', 23, 33),
    'ClanNode8': ('Kadesh', 'Mars', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 10, 20),
    'ClanNode9': ('Wahiba', 'Mars', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 10, 20),
    'CrewBattleNode501': ('Mordo Cluster', 'Saturn Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 55, 60),
    'CrewBattleNode502': ('Sover Strait', 'Earth Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 15, 20),
    'CrewBattleNode503': ('Bifrost Echo', 'Venus Proxima', 'MT_RAILJACK', 'Exterminate', 'FC_CORPUS', 15, 18),
    'CrewBattleNode504': ('Arva Vector', 'Neptune Proxima', 'MT_RAILJACK', 'Defense', 'FC_CORPUS', 25, 28),
    'CrewBattleNode509': ('Iota Temple', 'Earth Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 20, 28),
    'CrewBattleNode511': ('Beacon Shield Ring', 'Venus Proxima', 'MT_RAILJACK', 'Volatile', 'FC_CORPUS', 17, 20),
    'CrewBattleNode512': ('Orvin-Haarc', 'Venus Proxima', 'MT_RAILJACK', 'Spy', 'FC_CORPUS', 19, 22),
    'CrewBattleNode513': ('Vesper Strait', 'Venus Proxima', 'MT_RAILJACK', 'Orphix', 'FC_CORPUS', 21, 24),
    'CrewBattleNode514': ('Falling Glory', 'Venus Proxima', 'MT_RAILJACK', 'Defense', 'FC_CORPUS', 23, 26),
    'CrewBattleNode515': ('Luckless Expanse', 'Venus Proxima', 'MT_RAILJACK', 'Survival', 'FC_CORPUS', 22, 25),
    'CrewBattleNode516': ('Nu-gua Mines', 'Neptune Proxima', 'MT_RAILJACK', 'Exterminate', 'FC_CORPUS', 27, 30),
    'CrewBattleNode518': ('Ogal Cluster', 'Earth Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 21, 26),
    'CrewBattleNode519': ("Korm's Belt", 'Earth Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 24, 30),
    'CrewBattleNode521': ('Enkidu Ice Drifts', 'Neptune Proxima', 'MT_RAILJACK', 'Survival', 'FC_CORPUS', 29, 32),
    'CrewBattleNode522': ('Bendar Cluster', 'Earth Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 29, 36),
    'CrewBattleNode523': ("Mammon's Prospect", 'Neptune Proxima', 'MT_RAILJACK', 'Orphix', 'FC_CORPUS', 31, 34),
    'CrewBattleNode524': ('Sovereign Grasp', 'Neptune Proxima', 'MT_RAILJACK', 'Volatile', 'FC_CORPUS', 33, 36),
    'CrewBattleNode525': ('Brom Cluster', 'Neptune Proxima', 'MT_RAILJACK', 'Spy', 'FC_CORPUS', 32, 35),
    'CrewBattleNode526': ('Khufu Envoy', 'Pluto Proxima', 'MT_RAILJACK', 'Orphix', 'FC_CORPUS', 35, 38),
    'CrewBattleNode527': ('Seven Sirens', 'Pluto Proxima', 'MT_RAILJACK', 'Exterminate', 'FC_CORPUS', 37, 40),
    'CrewBattleNode528': ('Obol Crossing', 'Pluto Proxima', 'MT_RAILJACK', 'Defense', 'FC_CORPUS', 39, 42),
    'CrewBattleNode529': ('Profit Margin', 'Pluto Proxima', 'MT_RAILJACK', 'Volatile', 'FC_CORPUS', 41, 44),
    'CrewBattleNode530': ("Kasio's Rest", 'Saturn Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 70, 75),
    'CrewBattleNode531': ("Fenton's Field", 'Pluto Proxima', 'MT_RAILJACK', 'Survival', 'FC_CORPUS', 40, 43),
    'CrewBattleNode533': ('Nodo Gap', 'Saturn Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 54, 60),
    'CrewBattleNode534': ('Lupal Pass', 'Saturn Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 48, 56),
    'CrewBattleNode535': ('Vand Cluster', 'Saturn Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 65, 70),
    'CrewBattleNode536': ('Peregrine Axis', 'Pluto Proxima', 'MT_RAILJACK', 'Spy', 'FC_CORPUS', 43, 46),
    'CrewBattleNode538': ('Calabash', 'Veil Proxima', 'MT_RAILJACK', 'Exterminate', 'FC_CORPUS', 45, 49),
    'CrewBattleNode539': ('Numina', 'Veil Proxima', 'MT_RAILJACK', 'Volatile', 'FC_CORPUS', 48, 52),
    'CrewBattleNode540': ('Arc Silver', 'Veil Proxima', 'MT_RAILJACK', 'Defense', 'FC_CORPUS', 51, 55),
    'CrewBattleNode541': ('Erato', 'Veil Proxima', 'MT_RAILJACK', 'Orphix', 'FC_CORPUS', 54, 58),
    'CrewBattleNode542': ('Lu-yan', 'Veil Proxima', 'MT_RAILJACK', 'Survival', 'FC_CORPUS', 55, 59),
    'CrewBattleNode543': ('Sabmir Cloud', 'Veil Proxima', 'MT_RAILJACK', 'Spy', 'FC_CORPUS', 57, 60),
    'CrewBattleNode550': ('Nsu Grid', 'Veil Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 80, 90),
    'CrewBattleNode553': ('Flexa', 'Veil Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 80, 90),
    'CrewBattleNode554': ('H-2 Cloud', 'Veil Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 80, 90),
    'CrewBattleNode555': ('R-9 Cloud', 'Veil Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 80, 90),
    'CrewBattleNode556': ('Free Flight', 'Earth Proxima', 'MT_RAILJACK', 'Skirmish', 'FC_GRINEER', 1, 1),
    'CrewBattleNode557': ('Kuva Lich Confrontation', 'Saturn Proxima', 'MT_RAILJACK', 'Assassination', 'FC_GRINEER', 63, 70),
    'CrewBattleNode558': ('Sister of Parvos Confrontation', 'Neptune Proxima', 'MT_RAILJACK', 'Assassination', 'FC_CORPUS', 63, 70),
    'CrewBattleNode559': ('TECHNOCYTE CODA CONCERT', 'Earth Proxima', 'MT_RAILJACK', 'Assassination', '', 100, 100),
    'CrewBattleNode560': ('The Kuva Wytch', 'Uranus Proxima', 'MT_RAILJACK', 'Skirmish + Assassinate', 'FC_CORPUS', 65, 70),
    'CrewBattleNode561': ("Scoria's Angel", 'Uranus Proxima', 'MT_RAILJACK', 'Skirmish + Assassinate', 'FC_CORPUS', 65, 70),
    'DeimosHub': ('Necralisk', 'Deimos', 'MT_PVP', 'Conclave', '', 0, 0),
    'EarthHUB': ('Strata Relay', 'Earth', 'MT_PVP', 'Conclave', '', 0, 0),
    'EarthToMarsJunction': ('MARS JUNCTION', 'Earth', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 1),
    'EarthToVenusJunction': ('VENUS JUNCTION', 'Earth', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'EntratiLabHub': ('Sanctum Anatomica', 'Deimos', 'MT_PVP', 'Conclave', '', 0, 0),
    'ErisToSednaJunction': ('SEDNA JUNCTION', 'Eris', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 1),
    'EventNode763': ('The Index: Endurance', 'Neptune', 'MT_ARENA', 'Arena', 'FC_CORPUS', 30, 30),
    'IronwakeHUB3': ('Iron Wake', 'Earth', 'MT_PVP', 'Conclave', '', 0, 0),
    'JadeShadows2HUB': ('Pontis Tower', 'Uranus Proxima', 'MT_PVP', 'Conclave', '', 0, 0),
    'JupiterToEuropaJunction': ('EUROPA JUNCTION', 'Jupiter', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'JupiterToSaturnJunction': ('SATURN JUNCTION', 'Jupiter', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'MarsToCeresJunction': ('CERES JUNCTION', 'Mars', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 1),
    'MarsToPhobosJunction': ('PHOBOS JUNCTION', 'Mars', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'MercuryHUB': ('Larunda Relay', 'Mercury', 'MT_PVP', 'Conclave', '', 0, 0),
    'NeptuneToPlutoJunction': ('PLUTO JUNCTION', 'Neptune', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'NokkoColony': ('Deepmines', 'Venus', 'MT_LANDSCAPE', 'Free Roam', 'FC_CORPUS', 30, 40),
    'PlutoHUB': ('Orcus Relay', 'Pluto', 'MT_PVP', 'Conclave', '', 0, 0),
    'PlutoToErisJunction': ('ERIS JUNCTION', 'Pluto', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 1),
    'PvpNode0': ('Cephalon Capture', 'Saturn', 'MT_PVP', 'Conclave', '', 0, 0),
    'PvpNode10': ('Annihilation', 'Saturn', 'MT_PVP', 'Conclave', '', 0, 0),
    'PvpNode9': ('Team Annihilation', 'Saturn', 'MT_PVP', 'Conclave', '', 0, 0),
    'SaturnHUB': ('Kronia Relay', 'Saturn', 'MT_PVP', 'Conclave', '', 0, 0),
    'SaturnToUranusJunction': ('URANUS JUNCTION', 'Saturn', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'SettlementNode1': ('Roche', 'Phobos', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 10, 12),
    'SettlementNode10': ('Kepler', 'Phobos', 'MT_RACE', 'Rush', 'FC_CORPUS', 12, 14),
    'SettlementNode11': ('Gulliver', 'Phobos', 'MT_DEFENSE', 'Defense', 'FC_CORPUS', 10, 15),
    'SettlementNode12': ('Monolith', 'Phobos', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 13, 15),
    'SettlementNode14': ('Shklovsky', 'Phobos', 'MT_INTEL', 'Spy', 'FC_CORPUS', 11, 13),
    'SettlementNode15': ('Sharpless', 'Phobos', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 11, 13),
    'SettlementNode2': ('Skyresh', 'Phobos', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 12, 14),
    'SettlementNode20': ('Iliad', 'Phobos', 'MT_ASSASSINATION', 'Assassination', 'FC_CORPUS', 13, 15),
    'SettlementNode3': ('Stickney', 'Phobos', 'MT_SURVIVAL', 'Survival', 'FC_CORPUS', 10, 15),
    'SolNode1': ('Galatea', 'Neptune', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 27, 29),
    'SolNode10': ('Thebe', 'Jupiter', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 18, 20),
    'SolNode100': ('Elara', 'Jupiter', 'MT_SURVIVAL', 'Survival', 'FC_CORPUS', 15, 20),
    'SolNode101': ('Kiliken', 'Venus', 'MT_EXCAVATE', 'Excavation', 'FC_CORPUS', 3, 7),
    'SolNode102': ('Oceanum', 'Pluto', 'MT_INTEL', 'Spy', 'FC_CORPUS', 32, 36),
    'SolNode103': ('M Prime', 'Mercury', 'MT_EXTERMINATION', 'Exterminate', 'FC_INFESTATION', 7, 9),
    'SolNode104': ('Fossa', 'Venus', 'MT_ASSASSINATION', 'Assassination', 'FC_CORPUS', 6, 8),
    'SolNode105': ('Titania', 'Uranus', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 27, 29),
    'SolNode106': ('Alator', 'Mars', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 8, 13),
    'SolNode107': ('Venera', 'Venus', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 5, 7),
    'SolNode108': ('Tolstoj', 'Mercury', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 8, 10),
    'SolNode109': ('Linea', 'Venus', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 5, 7),
    'SolNode11': ('Tharsis', 'Mars', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 9, 11),
    'SolNode113': ('Ares', 'Mars', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 8, 10),
    'SolNode114': ('Puck', 'Uranus', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 27, 29),
    'SolNode118': ('Laomedeia', 'Neptune', 'MT_ARTIFACT', 'Disruption', 'FC_CORPUS', 25, 30),
    'SolNode119': ('Caloris', 'Mercury', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 6, 8),
    'SolNode12': ('Elion', 'Mercury', 'MT_CAPTURE', 'Capture', 'FC_GRINEER', 7, 9),
    'SolNode121': ('Carpo', 'Jupiter', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 17, 19),
    'SolNode122': ('Stephano', 'Uranus', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 24, 29),
    'SolNode123': ('V Prime', 'Venus', 'MT_SURVIVAL', 'Survival', 'FC_CORPUS', 6, 8),
    'SolNode125': ('Io', 'Jupiter', 'MT_DEFENSE', 'Defense', 'FC_CORPUS', 15, 20),
    'SolNode126': ('Metis', 'Jupiter', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 15, 17),
    'SolNode127': ('Psamathe', 'Neptune', 'MT_ASSASSINATION', 'Assassination', 'FC_CORPUS', 30, 32),
    'SolNode128': ('E Gate', 'Venus', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 3, 5),
    'SolNode129': ('Orb Vallis', 'Venus', 'MT_LANDSCAPE', 'Free Roam', 'FC_CORPUS', 10, 30),
    'SolNode130': ('Lares', 'Mercury', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 6, 11),
    'SolNode131': ('Pallas', 'Ceres', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 12, 14),
    'SolNode132': ('Bode', 'Ceres', 'MT_INTEL', 'Spy', 'FC_GRINEER', 12, 14),
    'SolNode135': ('Thon', 'Ceres', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 15, 17),
    'SolNode137': ('Nuovo', 'Ceres', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 13, 15),
    'SolNode138': ('Ludi', 'Ceres', 'MT_RETRIEVAL', 'Hijack', 'FC_GRINEER', 15, 17),
    'SolNode139': ('Lex', 'Ceres', 'MT_CAPTURE', 'Capture', 'FC_GRINEER', 14, 16),
    'SolNode14': ('Ultor', 'Mars', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 11, 13),
    'SolNode140': ('Kiste', 'Ceres', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 13, 15),
    'SolNode141': ('Ker', 'Ceres', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 14, 16),
    'SolNode144': ('Exta', 'Ceres', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 14, 16),
    'SolNode146': ('Draco', 'Ceres', 'MT_SURVIVAL', 'Survival', 'FC_GRINEER', 12, 17),
    'SolNode147': ('Cinxia', 'Ceres', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 12, 17),
    'SolNode149': ('Casta', 'Ceres', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 12, 17),
    'SolNode15': ('Pacific', 'Earth', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 3, 6),
    'SolNode153': ('Brugia', 'Eris', 'MT_RESCUE', 'Rescue', 'FC_INFESTATION', 32, 36),
    'SolNode16': ('Augustus', 'Mars', 'MT_EXCAVATE', 'Excavation', 'FC_GRINEER', 9, 11),
    'SolNode162': ('Isos', 'Eris', 'MT_CAPTURE', 'Capture', 'FC_INFESTATION', 32, 36),
    'SolNode164': ('Kala-azar', 'Eris', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 30, 40),
    'SolNode166': ('Nimus', 'Eris', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 30, 40),
    'SolNode167': ('Oestrus', 'Eris', 'MT_PURIFY', 'Infested Salvage', 'FC_INFESTATION', 34, 38),
    'SolNode17': ('Proteus', 'Neptune', 'MT_DEFENSE', 'Defense', 'FC_CORPUS', 27, 32),
    'SolNode171': ('Saxis', 'Eris', 'MT_EXTERMINATION', 'Exterminate', 'FC_INFESTATION', 34, 38),
    'SolNode172': ('Xini', 'Eris', 'MT_TERRITORY', 'Interception', 'FC_INFESTATION', 30, 40),
    'SolNode173': ('Solium', 'Eris', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_INFESTATION', 34, 38),
    'SolNode175': ('Naeglar', 'Eris', 'MT_HIVE', 'Hive', 'FC_INFESTATION', 30, 34),
    'SolNode177': ('Kappa', 'Sedna', 'MT_ARTIFACT', 'Disruption', 'FC_GRINEER', 34, 38),
    'SolNode18': ('Rhea', 'Saturn', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 21, 26),
    'SolNode181': ('Adaro', 'Sedna', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 32, 36),
    'SolNode183': ('Vodyanoi', 'Sedna', 'MT_ARENA', 'Arena', 'FC_GRINEER', 85, 85),
    'SolNode184': ('Rusalka', 'Sedna', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 32, 36),
    'SolNode185': ('Berehynia', 'Sedna', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 30, 40),
    'SolNode187': ('Selkie', 'Sedna', 'MT_SURVIVAL', 'Survival', 'FC_GRINEER', 30, 40),
    'SolNode188': ('Kelpie', 'Sedna', 'MT_INTEL', 'Spy', 'FC_GRINEER', 35, 40),
    'SolNode189': ('Naga', 'Sedna', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 30, 34),
    'SolNode19': ('Enceladus', 'Saturn', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 23, 25),
    'SolNode190': ('Nakki', 'Sedna', 'MT_ARENA', 'Arena', 'FC_GRINEER', 40, 40),
    'SolNode191': ('Marid', 'Sedna', 'MT_RETRIEVAL', 'Hijack', 'FC_GRINEER', 34, 38),
    'SolNode193': ('Merrow', 'Sedna', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 35, 40),
    'SolNode195': ('Hydron', 'Sedna', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 30, 40),
    'SolNode196': ('Charybdis', 'Sedna', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 34, 38),
    'SolNode199': ('Yam', 'Sedna', 'MT_ARENA', 'Arena', 'FC_GRINEER', 60, 60),
    'SolNode2': ('Aphrodite', 'Venus', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 5, 8),
    'SolNode20': ('Telesto', 'Saturn', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 22, 24),
    'SolNode203': ('Abaddon', 'Europa', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 21, 23),
    'SolNode204': ('Armaros', 'Europa', 'MT_EXTERMINATION', 'Exterminate', 'FC_INFESTATION', 18, 20),
    'SolNode205': ('Baal', 'Europa', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 21, 23),
    'SolNode209': ('Morax', 'Europa', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 18, 20),
    'SolNode21': ('Narcissus', 'Pluto', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 32, 36),
    'SolNode210': ('Naamah', 'Europa', 'MT_ASSASSINATION', 'Assassination', 'FC_CORPUS', 21, 23),
    'SolNode211': ('Ose', 'Europa', 'MT_TERRITORY', 'Interception', 'FC_CORPUS', 18, 23),
    'SolNode212': ('Paimon', 'Europa', 'MT_DEFENSE', 'Defense', 'FC_CORPUS', 18, 23),
    'SolNode214': ('Sorath', 'Europa', 'MT_RETRIEVAL', 'Hijack', 'FC_CORPUS', 19, 21),
    'SolNode215': ('Valac', 'Europa', 'MT_INTEL', 'Spy', 'FC_INFESTATION', 18, 20),
    'SolNode216': ('Valefor', 'Europa', 'MT_EXCAVATE', 'Excavation', 'FC_CORPUS', 18, 23),
    'SolNode217': ('Orias', 'Europa', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 20, 22),
    'SolNode22': ('Tessera', 'Venus', 'MT_DEFENSE', 'Defense', 'FC_CORPUS', 3, 7),
    'SolNode220': ('Kokabiel', 'Europa', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 20, 22),
    'SolNode223': ('Boethius', 'Mercury', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_INFESTATION', 8, 10),
    'SolNode224': ('Odin', 'Mercury', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 6, 11),
    'SolNode225': ('Suisei', 'Mercury', 'MT_INTEL', 'Spy', 'FC_GRINEER', 8, 10),
    'SolNode226': ('Pantheon', 'Mercury', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 6, 8),
    'SolNode228': ('Plains of Eidolon', 'Earth', 'MT_LANDSCAPE', 'Free Roam', 'FC_GRINEER', 10, 30),
    'SolNode229': ('Cambion Drift', 'Deimos', 'MT_LANDSCAPE', 'Free Roam', 'FC_INFESTATION', 15, 30),
    'SolNode23': ('Cytherean', 'Venus', 'MT_TERRITORY', 'Interception', 'FC_CORPUS', 6, 8),
    'SolNode230': ('Everview Arc', 'Zariman', 'MT_CORRUPTION', 'Void Flood', 'FC_GRINEER', 50, 55),
    'SolNode231': ('Halako Perimeter', 'Zariman', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 50, 55),
    'SolNode232': ('Tuvul Commons', 'Zariman', 'MT_VOID_CASCADE', 'Void Cascade', 'FC_GRINEER', 50, 55),
    'SolNode233': ('Oro Works', 'Zariman', 'MT_ARMAGEDDON', 'Void Armageddon', 'FC_GRINEER', 50, 55),
    'SolNode234': ('Dormizone', 'Zariman', 'MT_PVP', 'Conclave', '', 0, 0),
    'SolNode235': ('The Greenway', 'Zariman', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 50, 55),
    'SolNode236': ('The Duviri Experience', 'Duviri', 'MT_LANDSCAPE', 'Free Roam', 'FC_DUVIRI', 20, 20),
    'SolNode237': ('The Lone Story', 'Duviri', 'MT_LANDSCAPE', 'Free Roam', 'FC_DUVIRI', 20, 20),
    'SolNode238': ('The Circuit', 'Duviri', 'MT_ENDLESS_DUVIRI', 'The Circuit', 'FC_OROKIN', 35, 55),
    'SolNode239': ('Vesper Relay', 'Venus', 'MT_PAINT_FLOOD', "Follie's Hunt", 'FC_TENNO', 35, 45),
    'SolNode24': ('Oro', 'Earth', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 20, 25),
    'SolNode25': ('Callisto', 'Jupiter', 'MT_TERRITORY', 'Interception', 'FC_CORPUS', 15, 20),
    'SolNode250': ('Recall: Hunhullus', 'Dark Refractory, Deimos', 'MT_TAU_WAR', 'The Perita Rebellion', 'FC_OROKIN', 65, 70),
    'SolNode251': ('Recall: Dactolyst', 'Dark Refractory, Deimos', 'MT_TAU_WAR', 'The Perita Rebellion', 'FC_OROKIN', 65, 70),
    'SolNode252': ('Recall: Vanguard', 'Dark Refractory, Deimos', 'MT_TAU_WAR', 'The Perita Rebellion', 'FC_OROKIN', 65, 70),
    'SolNode253': ('Descend', 'Dark Refractory, Deimos', 'MT_DESCENT', 'The Descendia', 'FC_OROKIN', 65, 65),
    'SolNode254': ("Descend from Marie's Sanctuary", 'Dark Refractory, Deimos', 'MT_DESCENT', 'The Descendia', 'FC_OROKIN', 72, 72),
    'SolNode255': ("Descend from Lyon's Sanctuary", 'Dark Refractory, Deimos', 'MT_DESCENT', 'The Descendia', 'FC_OROKIN', 79, 79),
    'SolNode256': ("Roathe's Oblivion", 'Dark Refractory, Deimos', 'MT_DESCENT', 'The Descendia', 'FC_OROKIN', 86, 86),
    'SolNode257': ('The Guilty', 'Dark Refractory, Deimos', 'MT_TAU_WAR', 'The Perita Rebellion', 'FC_OROKIN', 165, 170),
    'SolNode26': ('Lith', 'Earth', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 2, 5),
    'SolNode27': ('E Prime', 'Earth', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 1, 3),
    'SolNode28': ('Terminus', 'Mercury', 'MT_SABOTAGE', 'Sabotage', 'FC_INFESTATION', 8, 10),
    'SolNode30': ('Olympus', 'Mars', 'MT_ARTIFACT', 'Disruption', 'FC_GRINEER', 15, 20),
    'SolNode300': ('Plato', 'Lua', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 25, 30),
    'SolNode301': ('Grimaldi', 'Lua', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 25, 30),
    'SolNode302': ('Tycho', 'Lua', 'MT_SURVIVAL', 'Survival', 'FC_CORPUS', 25, 30),
    'SolNode304': ('Copernicus', 'Lua', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 25, 30),
    'SolNode305': ('Stöfler', 'Lua', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 25, 30),
    'SolNode306': ('Pavlov', 'Lua', 'MT_INTEL', 'Spy', 'FC_CORPUS', 25, 30),
    'SolNode307': ('Zeipel', 'Lua', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 25, 30),
    'SolNode308': ('Apollo', 'Lua', 'MT_ARTIFACT', 'Disruption', 'FC_CORPUS', 35, 40),
    'SolNode309': ('Yuvarium', 'Lua', 'MT_SURVIVAL', 'Conjunction Survival', 'FC_OROKIN', 25, 30),
    'SolNode31': ('Anthe', 'Saturn', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 22, 24),
    'SolNode310': ('Circulus', 'Lua', 'MT_SURVIVAL', 'Conjunction Survival', 'FC_OROKIN', 80, 100),
    'SolNode32': ('Tethys', 'Saturn', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 24, 26),
    'SolNode33': ('Ariel', 'Uranus', 'MT_CAPTURE', 'Capture', 'FC_GRINEER', 25, 27),
    'SolNode34': ('Sycorax', 'Uranus', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 24, 26),
    'SolNode36': ('Martialis', 'Mars', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 10, 12),
    'SolNode38': ('Minthe', 'Pluto', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 30, 34),
    'SolNode39': ('Everest', 'Earth', 'MT_EXCAVATE', 'Excavation', 'FC_GRINEER', 1, 6),
    'SolNode4': ('Acheron', 'Pluto', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 34, 38),
    'SolNode400': ('Teshub', 'Void', 'MT_EXTERMINATION', 'Exterminate', 'FC_OROKIN', 10, 15),
    'SolNode401': ('Hepit', 'Void', 'MT_CAPTURE', 'Capture', 'FC_OROKIN', 10, 15),
    'SolNode402': ('Taranis', 'Void', 'MT_DEFENSE', 'Defense', 'FC_OROKIN', 10, 15),
    'SolNode403': ('Tiwaz', 'Void', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_OROKIN', 20, 25),
    'SolNode404': ('Stribog', 'Void', 'MT_SABOTAGE', 'Sabotage', 'FC_OROKIN', 20, 25),
    'SolNode405': ('Ani', 'Void', 'MT_SURVIVAL', 'Survival', 'FC_OROKIN', 20, 25),
    'SolNode406': ('Ukko', 'Void', 'MT_CAPTURE', 'Capture', 'FC_OROKIN', 30, 35),
    'SolNode407': ('Oxomoco', 'Void', 'MT_EXTERMINATION', 'Exterminate', 'FC_OROKIN', 30, 35),
    'SolNode408': ('Belenus', 'Void', 'MT_DEFENSE', 'Defense', 'FC_OROKIN', 30, 35),
    'SolNode409': ('Mot', 'Void', 'MT_SURVIVAL', 'Survival', 'FC_OROKIN', 40, 45),
    'SolNode41': ('Arval', 'Mars', 'MT_INTEL', 'Spy', 'FC_GRINEER', 9, 11),
    'SolNode410': ('Aten', 'Void', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_OROKIN', 40, 45),
    'SolNode411': ('Marduk', 'Void', 'MT_SABOTAGE', 'Sabotage', 'FC_OROKIN', 40, 45),
    'SolNode412': ('Mithra', 'Void', 'MT_TERRITORY', 'Interception', 'FC_OROKIN', 40, 45),
    'SolNode42': ('Helene', 'Saturn', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 21, 26),
    'SolNode43': ('Cerberus', 'Pluto', 'MT_TERRITORY', 'Interception', 'FC_CORPUS', 30, 40),
    'SolNode45': ('Ara', 'Mars', 'MT_CAPTURE', 'Capture', 'FC_GRINEER', 10, 12),
    'SolNode450': ('Tyana Pass', 'Mars', 'MT_DEFENSE', 'Mirror Defense', 'FC_CORPUS', 25, 30),
    'SolNode451': ("Saya's Visions", 'Earth', 'MT_OFFERING', 'Shrine Defense', 'FC_INFESTATION', 5, 15),
    'SolNode46': ('Spear', 'Mars', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 8, 12),
    'SolNode48': ('Regna', 'Pluto', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 34, 38),
    'SolNode49': ('Larissa', 'Neptune', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 29, 31),
    'SolNode50': ('Numa', 'Saturn', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 22, 24),
    'SolNode51': ('Hades', 'Pluto', 'MT_ASSASSINATION', 'Assassination', 'FC_CORPUS', 35, 45),
    'SolNode53': ('Themisto', 'Jupiter', 'MT_ASSASSINATION', 'Assassination', 'FC_CORPUS', 18, 20),
    'SolNode56': ('Cypress', 'Pluto', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 34, 38),
    'SolNode57': ('Sao', 'Neptune', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 30, 32),
    'SolNode58': ('Hellas', 'Mars', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 8, 12),
    'SolNode59': ('Eurasia', 'Earth', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 3, 6),
    'SolNode6': ('Despina', 'Neptune', 'MT_EXCAVATE', 'Excavation', 'FC_CORPUS', 27, 32),
    'SolNode60': ('Caliban', 'Uranus', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 25, 27),
    'SolNode61': ('Ishtar', 'Venus', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 6, 8),
    'SolNode62': ('Neso', 'Neptune', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 29, 31),
    'SolNode63': ('Mantle', 'Earth', 'MT_CAPTURE', 'Capture', 'FC_GRINEER', 2, 4),
    'SolNode64': ('Umbriel', 'Uranus', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 24, 29),
    'SolNode65': ('Gradivus', 'Mars', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 9, 12),
    'SolNode66': ('Unda', 'Venus', 'MT_INTEL', 'Spy', 'FC_CORPUS', 6, 8),
    'SolNode67': ('Dione', 'Saturn', 'MT_INTEL', 'Spy', 'FC_GRINEER', 21, 23),
    'SolNode68': ('Vallis', 'Mars', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 11, 13),
    'SolNode69': ('Ophelia', 'Uranus', 'MT_SURVIVAL', 'Survival', 'FC_GRINEER', 24, 29),
    'SolNode70': ('Cassini', 'Saturn', 'MT_CAPTURE', 'Capture', 'FC_GRINEER', 21, 23),
    'SolNode701': ('Jordas Golem Assassinate', 'Eris', 'MT_ASSASSINATION', 'Assassination', 'FC_INFESTATION', 32, 34),
    'SolNode705': ('Mutalist Alad V Assassinate', 'Eris', 'MT_ASSASSINATION', 'Assassination', 'FC_INFESTATION', 30, 35),
    'SolNode706': ('Horend', 'Deimos', 'MT_CAPTURE', 'Capture', 'FC_INFESTATION', 12, 14),
    'SolNode707': ('Hyf', 'Deimos', 'MT_DEFENSE', 'Defense', 'FC_INFESTATION', 15, 20),
    'SolNode708': ('Phlegyas', 'Deimos', 'MT_EXTERMINATION', 'Exterminate', 'FC_INFESTATION', 13, 15),
    'SolNode709': ('Dirus', 'Deimos', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_INFESTATION', 15, 17),
    'SolNode710': ('Formido', 'Deimos', 'MT_SABOTAGE', 'Sabotage', 'FC_INFESTATION', 14, 16),
    'SolNode711': ('Terrorem', 'Deimos', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 25, 35),
    'SolNode712': ('Magnacidium', 'Deimos', 'MT_ASSASSINATION', 'Assassination', 'FC_INFESTATION', 20, 25),
    'SolNode713': ('Exequias', 'Deimos', 'MT_ASSASSINATION', 'Assassination', 'FC_INFESTATION', 30, 35),
    'SolNode715': ('Effervo', 'Deimos', 'MT_ASSASSINATION', 'Assassination', 'FC_MITW', 55, 60),
    'SolNode716': ('Nex', 'Deimos', 'MT_EXTERMINATION', 'Exterminate', 'FC_MITW', 55, 60),
    'SolNode717': ('Persto', 'Deimos', 'MT_SURVIVAL', 'Survival', 'FC_MITW', 55, 60),
    'SolNode718': ('Cambire', 'Deimos', 'MT_ALCHEMY', 'Alchemy', 'FC_MITW', 55, 60),
    'SolNode719': ('Munio', 'Deimos', 'MT_DEFENSE', 'Mirror Defense', 'FC_MITW', 55, 60),
    'SolNode72': ('Outer Terminus', 'Pluto', 'MT_DEFENSE', 'Defense', 'FC_CORPUS', 30, 40),
    'SolNode720': ('Testudo', 'Deimos', 'MT_VAULTS', 'Netracell', '', 220, 240),
    'SolNode721': ('Armatus', 'Deimos', 'MT_ARTIFACT', 'Disruption', 'FC_MITW', 55, 60),
    'SolNode723': ('Brutus', 'Uranus', 'MT_ASCENSION', 'Ascension', 'FC_CORPUS', 45, 50),
    'SolNode73': ('Ananke', 'Jupiter', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 16, 18),
    'SolNode74': ('Carme', 'Jupiter', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 16, 18),
    'SolNode740': ('The Ropalolyst', 'Jupiter', 'MT_ASSASSINATION', 'Assassination', 'FC_SENTIENT', 40, 40),
    'SolNode741': ('Koro', 'Kuva Fortress', 'MT_ASSAULT', 'Assault', 'FC_GRINEER', 29, 31),
    'SolNode742': ('Nabuk', 'Kuva Fortress', 'MT_DEFENSE', 'Defense', 'FC_GRINEER', 30, 32),
    'SolNode743': ('Rotuma', 'Kuva Fortress', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 30, 32),
    'SolNode744': ('Taveuni', 'Kuva Fortress', 'MT_SURVIVAL', 'Survival', 'FC_GRINEER', 32, 37),
    'SolNode745': ('Tamu', 'Kuva Fortress', 'MT_ARTIFACT', 'Disruption', 'FC_GRINEER', 35, 40),
    'SolNode746': ('Dakata', 'Kuva Fortress', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 28, 30),
    'SolNode747': ('Pago', 'Kuva Fortress', 'MT_INTEL', 'Spy', 'FC_GRINEER', 31, 33),
    'SolNode748': ('Garus', 'Kuva Fortress', 'MT_RESCUE', 'Rescue', 'FC_GRINEER', 31, 33),
    'SolNode75': ('Cervantes', 'Earth', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 4, 6),
    'SolNode76': ('Hydra', 'Pluto', 'MT_CAPTURE', 'Capture', 'FC_CORPUS', 30, 34),
    'SolNode761': ('THE INDEX: ENDURANCE (LOW RISK)', 'Neptune', 'MT_ARENA', 'Arena', 'FC_CORPUS', 30, 30),
    'SolNode762': ('THE INDEX: ENDURANCE (MEDIUM RISK)', 'Neptune', 'MT_ARENA', 'Arena', 'FC_CORPUS', 30, 30),
    'SolNode763': ('THE INDEX: ENDURANCE (HIGH RISK)', 'Neptune', 'MT_ARENA', 'Arena', 'FC_CORPUS', 30, 30),
    'SolNode78': ('Triton', 'Neptune', 'MT_RESCUE', 'Rescue', 'FC_CORPUS', 28, 30),
    'SolNode79': ('Cambria', 'Earth', 'MT_INTEL', 'Spy', 'FC_GRINEER', 3, 6),
    'SolNode801': ('Sanctuary Onslaught', 'Sanctuary', 'MT_ENDLESS_EXTERMINATION', 'Sanctuary Onslaught', '', 20, 30),
    'SolNode802': ('Elite Sanctuary Onslaught', 'Sanctuary', 'MT_ENDLESS_EXTERMINATION', 'Sanctuary Onslaught', '', 60, 70),
    'SolNode81': ('Palus', 'Pluto', 'MT_SURVIVAL', 'Survival', 'FC_CORPUS', 30, 40),
    'SolNode82': ('Calypso', 'Saturn', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 24, 26),
    'SolNode83': ('Cressida', 'Uranus', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 27, 29),
    'SolNode84': ('Nereid', 'Neptune', 'MT_INTEL', 'Spy', 'FC_CORPUS', 30, 32),
    'SolNode85': ('Gaia', 'Earth', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 1, 6),
    'SolNode850': ('Legacyte Harvest', 'Höllvania', 'MT_ENDLESS_CAPTURE', 'Legacyte Harvest', 'FC_TECHROT', 65, 70),
    'SolNode851': ('Hell-Scrub: Scaldra', 'Höllvania', 'MT_SURVIVAL', 'Survival', 'FC_SCALDRA', 65, 70),
    'SolNode852': ('Hell-Scrub: Techrot', 'Höllvania', 'MT_SURVIVAL', 'Survival', 'FC_TECHROT', 65, 70),
    'SolNode853': ('Exterminate: Scaldra', 'Höllvania', 'MT_EXTERMINATION', 'Exterminate', 'FC_SCALDRA', 65, 70),
    'SolNode854': ('Exterminate: Techrot', 'Höllvania', 'MT_EXTERMINATION', 'Exterminate', 'FC_TECHROT', 65, 70),
    'SolNode855': ('Faceoff: Squad VS Squad', 'Höllvania', 'MT_PVPVE', 'Faceoff', 'FC_SCALDRA', 65, 70),
    'SolNode856': ('Assassinate: H-09 Tank', 'Höllvania', 'MT_ASSASSINATION', 'Assassination', 'FC_SCALDRA', 65, 70),
    'SolNode857': ('Faceoff: Single Squad', 'Höllvania', 'MT_PVPVE', 'Faceoff', 'FC_SCALDRA', 65, 70),
    'SolNode858': ('Solstice Square', 'Höllvania', 'MT_DEFENSE', 'Defense', 'FC_SCALDRA', 65, 70),
    'SolNode87': ('Ganymede', 'Jupiter', 'MT_ARTIFACT', 'Disruption', 'FC_CORPUS', 30, 35),
    'SolNode88': ('Adrastea', 'Jupiter', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 18, 20),
    'SolNode89': ('Mariana', 'Earth', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 1, 3),
    'SolNode9': ('Rosalind', 'Uranus', 'MT_INTEL', 'Spy', 'FC_GRINEER', 27, 29),
    'SolNode902': ('Montes', 'Venus', 'MT_EXTERMINATION', 'Exterminate', 'FC_CORPUS', 3, 8),
    'SolNode903': ('Erpo', 'Earth', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 1, 6),
    'SolNode904': ('Syrtis', 'Mars', 'MT_EXTERMINATION', 'Exterminate', 'FC_GRINEER', 8, 13),
    'SolNode905': ('Galilea', 'Jupiter', 'MT_SABOTAGE', 'Sabotage', 'FC_CORPUS', 15, 20),
    'SolNode906': ('Pandora', 'Saturn', 'MT_PURSUIT', 'Pursuit', 'FC_GRINEER', 21, 23),
    'SolNode907': ('Caelus', 'Uranus', 'MT_TERRITORY', 'Interception', 'FC_GRINEER', 24, 29),
    'SolNode908': ('Salacia', 'Neptune', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_CORPUS', 27, 32),
    'SolNode93': ('Keeler', 'Saturn', 'MT_MOBILE_DEFENSE', 'Mobile Defense', 'FC_GRINEER', 23, 25),
    'SolNode94': ('Apollodorus', 'Mercury', 'MT_SURVIVAL', 'Survival', 'FC_INFESTATION', 6, 11),
    'SolNode96': ('Titan', 'Saturn', 'MT_SURVIVAL', 'Survival', 'FC_GRINEER', 21, 26),
    'SolNode97': ('Amalthea', 'Jupiter', 'MT_INTEL', 'Spy', 'FC_CORPUS', 17, 19),
    'SolNode98': ('Desdemona', 'Uranus', 'MT_SABOTAGE', 'Sabotage', 'FC_GRINEER', 26, 28),
    'SolNode99': ('War', 'Mars', 'MT_ASSASSINATION', 'Assassination', 'FC_GRINEER', 11, 13),
    'SolarisUnitedHub1': ('Fortuna', 'Venus', 'MT_PVP', 'Conclave', '', 0, 0),
    'TradeHUB1': ("Maroo's Bazaar", 'Mars', 'MT_PVP', 'Conclave', '', 0, 0),
    'UranusToNeptuneJunction': ('NEPTUNE JUNCTION', 'Uranus', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'VenusToMercuryJunction': ('MERCURY JUNCTION', 'Venus', 'MT_JUNCTION', 'Solar Rail Junction', '', 1, 3),
    'ZarimanHub': ('Chrysalith', 'Zariman', 'MT_PVP', 'Conclave', '', 0, 0),
}

MISSION_TYPES: dict[str, str] = {
    'MT_ALCHEMY': 'Alchemy',
    'MT_ARENA': 'Arena',
    'MT_ARMAGEDDON': 'Void Armageddon',
    'MT_ARTIFACT': 'Disruption',
    'MT_ASCENSION': 'Ascension',
    'MT_ASSASSINATION': 'Assassination',
    'MT_ASSAULT': 'Assault',
    'MT_CAPTURE': 'Capture',
    'MT_CORRUPTION': 'Void Flood',
    'MT_DEFENSE': 'Defense',
    'MT_DESCENT': 'The Descendia',
    'MT_ENDLESS_CAPTURE': 'Legacyte Harvest',
    'MT_ENDLESS_DUVIRI': 'The Circuit',
    'MT_ENDLESS_EXTERMINATION': 'Sanctuary Onslaught',
    'MT_EVACUATION': 'Defection',
    'MT_EXCAVATE': 'Excavation',
    'MT_EXTERMINATION': 'Exterminate',
    'MT_HIVE': 'Hive',
    'MT_INTEL': 'Spy',
    'MT_JUNCTION': 'Solar Rail Junction',
    'MT_LANDSCAPE': 'Free Roam',
    'MT_MOBILE_DEFENSE': 'Mobile Defense',
    'MT_OFFERING': 'Shrine Defense',
    'MT_PAINT_FLOOD': "Follie's Hunt",
    'MT_PURIFY': 'Infested Salvage',
    'MT_PURSUIT': 'Pursuit',
    'MT_PVP': 'Conclave',
    'MT_PVPVE': 'Faceoff',
    'MT_RACE': 'Rush',
    'MT_RESCUE': 'Rescue',
    'MT_RETRIEVAL': 'Hijack',
    'MT_SABOTAGE': 'Sabotage',
    'MT_SALVAGE': 'Sector Wars',
    'MT_SURVIVAL': 'Survival',
    'MT_TAU_WAR': 'The Perita Rebellion',
    'MT_TERRITORY': 'Interception',
    'MT_VAULTS': 'Netracell',
    'MT_VOID_CASCADE': 'Void Cascade',
}

# challenge path: (title, description template, standing, required count)
CHALLENGES: dict[str, tuple[str, str, int | None, int]] = {
    '/Lotus/Types/Challenges/Calendar1999/CalendarDestroyPropsBase': ('Starve The Beast', 'Destroy |COUNT| Containers', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarDestroyPropsEasy': ('Starve The Beast', 'Destroy |COUNT| Containers', None, 75),
    '/Lotus/Types/Challenges/Calendar1999/CalendarDestroyPropsHard': ('Starve The Beast', 'Destroy |COUNT| Containers', None, 300),
    '/Lotus/Types/Challenges/Calendar1999/CalendarDestroyPropsMedium': ('Starve The Beast', 'Destroy |COUNT| Containers', None, 150),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesBase': ('Even the Odds', 'Kill |COUNT| Enemies', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesEasy': ('Even the Odds', 'Kill |COUNT| Enemies', None, 250),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesHard': ('Even the Odds', 'Kill |COUNT| Enemies', None, 1000),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesMedium': ('Even the Odds', 'Kill |COUNT| Enemies', None, 500),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithAbilitiesBase': ('Demonstration of Power', 'Kill |COUNT| Enemies with Abilities', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithAbilitiesEasy': ('Demonstration of Power', 'Kill |COUNT| Enemies with Abilities', None, 150),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithAbilitiesHard': ('Demonstration of Power', 'Kill |COUNT| Enemies with Abilities', None, 500),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithAbilitiesMedium': ('Demonstration of Power', 'Kill |COUNT| Enemies with Abilities', None, 300),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithMeleeBase': ('By the Blade', 'Kill |COUNT| Enemies with Melee Weapons', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithMeleeEasy': ('By the Blade', 'Kill |COUNT| Enemies with Melee Weapons', None, 150),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithMeleeHard': ('By the Blade', 'Kill |COUNT| Enemies with Melee Weapons', None, 500),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEnemiesWithMeleeMedium': ('By the Blade', 'Kill |COUNT| Enemies with Melee Weapons', None, 300),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEximusBase': ('Ex-Eximus', 'Kill |COUNT| Eximus', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEximusEasy': ('Ex-Eximus', 'Kill |COUNT| Eximus', None, 10),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEximusHard': ('Ex-Eximus', 'Kill |COUNT| Eximus', None, 30),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillEximusMedium': ('Ex-Eximus', 'Kill |COUNT| Eximus', None, 20),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesBase': ('Punish Scaldra', 'Kill |COUNT| Scaldra Troops', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesEasy': ('Punish Scaldra', 'Kill |COUNT| Scaldra Troops', None, 150),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesHard': ('Punish Scaldra', 'Kill |COUNT| Scaldra Troops', None, 500),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesMedium': ('Punish Scaldra', 'Kill |COUNT| Scaldra Troops', None, 350),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithAbilitiesBase': ('Shock and Awe', 'Kill |COUNT| Scaldra Troops with Abilities', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithAbilitiesEasy': ('Shock and Awe', 'Kill |COUNT| Scaldra Troops with Abilities', None, 75),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithAbilitiesHard': ('Shock and Awe', 'Kill |COUNT| Scaldra Troops with Abilities', None, 250),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithAbilitiesMedium': ('Shock and Awe', 'Kill |COUNT| Scaldra Troops with Abilities', None, 150),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithMeleeBase': ('Make It Personal', 'Kill |COUNT| Scaldra Troops with Melee Weapons', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithMeleeEasy': ('Make It Personal', 'Kill |COUNT| Scaldra Troops with Melee Weapons', None, 75),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithMeleeHard': ('Make It Personal', 'Kill |COUNT| Scaldra Troops with Melee Weapons', None, 250),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillScaldraEnemiesWithMeleeMedium': ('Make It Personal', 'Kill |COUNT| Scaldra Troops with Melee Weapons', None, 150),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTankBase': ('Tankless Work', 'Destroy |COUNT| Tanks', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTankHard': ('Tankless Work', 'Destroy |COUNT| Tanks', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesBase': ('Purge the Infection', 'Kill |COUNT| Techrot', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesEasy': ('Purge the Infection', 'Kill |COUNT| Techrot', None, 250),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesHard': ('Purge the Infection', 'Kill |COUNT| Techrot', None, 750),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesMedium': ('Purge the Infection', 'Kill |COUNT| Techrot', None, 500),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithAbilitiesBase': ('Null and Void', 'Kill |COUNT| Techrot with Abilities', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithAbilitiesEasy': ('Null and Void', 'Kill |COUNT| Techrot with Abilities', None, 100),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithAbilitiesHard': ('Null and Void', 'Kill |COUNT| Techrot with Abilities', None, 300),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithAbilitiesMedium': ('Null and Void', 'Kill |COUNT| Techrot with Abilities', None, 200),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithMeleeBase': ('Electronic Waste Disposal', 'Kill |COUNT| Techrot with Melee Weapons', None, 1),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithMeleeEasy': ('Electronic Waste Disposal', 'Kill |COUNT| Techrot with Melee Weapons', None, 100),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithMeleeHard': ('Electronic Waste Disposal', 'Kill |COUNT| Techrot with Melee Weapons', None, 300),
    '/Lotus/Types/Challenges/Calendar1999/CalendarKillTechrotEnemiesWithMeleeMedium': ('Electronic Waste Disposal', 'Kill |COUNT| Techrot with Melee Weapons', None, 200),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyAFirmShake': ('A Firm Shake', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyAimGlide': ('Glider', 'Kill |COUNT| Enemies while Aim Gliding', 1000, 15),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyArtist': ('Artist', "Customize a piece of Equipment's colors.", 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyAugmentation': ('Augmentation', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyBuildersTouch': ('Builder’s Touch', 'Claim an item from your Foundry.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyBulletJump': ('Trampoline', 'Bullet Jump |COUNT| times.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCaptionThat': ('Caption That', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCodexScan': ('Researcher', 'Scan |COUNT| Objects or Enemies.', 1000, 15),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCollectCredits': ('Saver', 'Pick up |COUNT| Credits.', 1000, 15000),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCollectHundredResources': ('Gatherer', '', 1000, 1500),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCompleteMission': ('Agent', 'Complete a Mission.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCompleteMissionMelee': ('Swordsman', 'Complete a Mission with only a Melee Weapon equipped', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCompleteMissionPrimary': ('Hands Full', 'Complete a Mission with only a Primary Weapon equipped', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyCompleteMissionSecondary': ('Sidearm', 'Complete a Mission with only a Secondary Weapon equipped', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyDeathFromAbove': ('Death from Above', '', 1000, 10),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyDeployAirSupport': ('Air It Out', 'Deploy an Air Support Charge in a Mission', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyDeployGlyph': ('Graffiti', 'Deploy a Glyph while on a mission.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyDeploySpecter': ('Doppelganger', 'Deploy a Specter', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyDonateLeverian': ('Patron', 'Donate to the Leverian', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyFeedMeMore': ('Feed Me More', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyHelpingHand': ('Helping Hand', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyHijackCrewship': ('Confiscated', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyInteractWithPet': ('Loyalty', 'Interact with your Kubrow or Kavat', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemies': ('Warning Shot', 'Kill |COUNT| Enemies.', 1000, 200),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWhileOnKDrive': ('Thrill Rider', 'Kill |COUNT| enemies while riding a K-Drive, Kaithe, Velocipod, or Atomicycle.', 1000, 20),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithAbilities': ('Power Trip', 'Kill |COUNT| Enemies with Abilities.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithBlast': ('Detonator', 'Kill |COUNT| Enemies with <DT_EXPLOSION>Blast Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithCorrosive': ('Meltdown', 'Kill |COUNT| Enemies with <DT_CORROSIVE>Corrosive Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithElectricity': ('Short Circuit', 'Kill |COUNT| Enemies with <DT_ELECTRICITY>Electricity Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithFinishers': ('Executioner', 'Kill |COUNT| Enemies with Finishers.', 1000, 10),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithFire': ('Arsonist', 'Kill |COUNT| Enemies with <DT_FIRE>Heat Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithFreeze': ('Deep Freeze', 'Kill |COUNT| Enemies with <DT_FREEZE>Cold Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithGas': ('Biohazard', 'Kill |COUNT| Enemies with <DT_GAS>Gas Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithHeadshots': ('Marksman', 'Kill |COUNT| Enemies with Headshots.', 1000, 40),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithMagnetic': ('Attractive', 'Kill |COUNT| Enemies with <DT_MAGNETIC>Magnetic Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithMelee': ('Sword Dance', 'Kill |COUNT| Enemies with a Melee Weapon.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithPoison': ('Poisoner', 'Kill |COUNT| Enemies with <DT_POISON>Toxin Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithPrimary': ('Mow Them Down', 'Kill |COUNT| Enemies with a Primary Weapon.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithRadiation': ('Reactor', 'Kill |COUNT| Enemies with <DT_RADIATION>Radiation Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithSecondary': ('Smaller is Bigger', 'Kill |COUNT| Enemies with a Secondary Weapon.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillEnemiesWithViral': ('Go Viral', 'Kill |COUNT| Enemies with <DT_VIRAL>Viral Damage.', 1000, 150),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyKillThrall': ('Hush', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyLichNode': ('Reclaimed', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyLiquidation': ('Liquidation', 'Sell any item in your Inventory for Credits.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyMedic': ('Medic', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyMercyKill': ('No Mercy', 'Mercy Kill an Enemy', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyOpenLockers': ('Kleptomaniac', '', 1000, 20),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPickUpEnergy': ('Energizing', 'Pick up |COUNT| Energy Orbs.', 1000, 20),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPickUpMedallion': ('The Hunt is On', '', 1000, 5),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPickUpMods': ('Shiny', 'Pick up |COUNT| Mods.', 1000, 8),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPilotFallenMech': ('Reanimator', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPlaceMarker': ('Communicator', 'Mark |COUNT| Mods or Resources.', 1000, 5),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPlayEmote': ('Expressive', 'Play |COUNT| Emote.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyPlayMinigame': ('Child at Heart', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyRequiemTotem': ('Ancient Obelisk', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailySalutations': ('Salutations', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailySlideKills': ('Accelerator', 'Kill |COUNT| Enemies while Sliding.', 1000, 10),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailySolveCiphers': ('Hacker', '', 1000, 8),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyStayOnTop': ('Stay on top', '', 1000, 20),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailySuspendFiveEnemies': ('Deep Impact', 'Suspend 5 or more enemies in the air at once with a Heavy Slam Melee Attack', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailySwatter': ('Swatter', 'Kill |COUNT| Drones or Ospreys with your Melee Weapon.', 1000, 3),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyThePersonalTouch': ('The Personal Touch', 'Place 1 decoration in your Orbiter.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyToppingOffTheTank': ('Topping Off the Tank', 'Successfully defend an Excavator without allowing it to run out of power.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyTransmuteMods': ('Everything Old is New Again', 'Complete |COUNT| Mod Transmutations', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyTwoForOne': ('Two for One', 'Pierce and kill 2 or more enemies in a single Bow shot.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyVisitFeaturedDojo': ('Just Visiting', 'Visit a Featured Dojo', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyWholeArsenal': ('Whole Arsenal', 'Get a kill with your Primary, Secondary, and Melee weapons within 10 seconds.', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Daily/SeasonDailyYourMove': ('Your Move', '', 1000, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyBeastSlayer': ('Beast Slayer', 'Defeat the Orowyrm', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyBloodthirsty': ('Bloodthirsty', 'Kill 20 enemies in 5 seconds.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyBoardingPartyNoDamage': ('Flawless', 'Clear a Railjack Boarding Party without your Warframe taking damage', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyBowOffense': ('Bow Offense', 'Kill |COUNT| enemies with a Primary Bow.', 4500, 150),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCatchRarePlainsFish': ('Earth Fisher', 'Catch |COUNT| Rare Fish in the Plains of Eidolon.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCatchRareVenusFish': ('Venus Fisher', 'Catch |COUNT| Rare Servofish in the Orb Vallis.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCollectHundredResources': ('Gatherer', 'Collect |COUNT| Resources', 4500, 4000),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCollector': ('Collector', 'Collect |COUNT| resources from Duviri', 4500, 100),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteAssassination': ('Assassin', 'Complete |COUNT| Assassination missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteCapture': ('Jailer', 'Complete |COUNT| Capture missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteClemMission': ('Good Friend', 'Help Clem with his weekly mission.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteDisruptionConduits': ("Don't Blow It", 'Complete |COUNT| Conduits in Disruption', 4500, 12),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteExterminate': ('Eliminator', 'Complete |COUNT| Exterminate missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteInvasionMissions': ('Invader', 'Complete |COUNT| Invasion missions of any type.', 4500, 6),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteKuva': ("Don't Fear The Reaper", 'Complete |COUNT| Kuva Siphon Missions', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteMobileDefense': ('Protector', 'Complete |COUNT| Mobile Defense missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteNightmareMissions': ('Sound Sleeper', 'Complete |COUNT| Nightmare missions of any type.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteRace': ('Ollie Oop!', "Play Ollie's Crash Course and complete a race", 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteRescue': ('Rescuer', 'Complete |COUNT| Rescue missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteSabotage': ('Saboteur', 'Complete |COUNT| Sabotage missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteSortie': ('Sortie Specialist', 'Complete |COUNT| Sortie.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteSpy': ('Operative', 'Complete |COUNT| Spy missions.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteSyndicateMissions': ('Supporter', 'Complete |COUNT| Syndicate missions.', 4500, 5),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteTreasures': ('Animator', "Look for Ayatan Treasures for Maroo in Maroo's Bazaar.", 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyCompleteVenusRace': ('Now Boarding', 'Complete a K-Drive race in Orb Vallis on Venus or in Cambion Drift on Deimos.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyDestroyCrewshipArtillery': ('Forward Thinking', 'Destroy a Crewship with Forward Artillery', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyEternalGuardian': ('Eternal Guardian', '', 4500, 2),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyFeedHelminth': ('Feed the Beast', 'Feed the Helminth any resource.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyFinelyTuned': ('Finely Tuned', 'Play a Shawzin song activity in Duviri', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyGildModular': ('Gilded', '', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyHighGround': ('High Ground', '', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyHorsingAround': ('Horsing Around', 'Fly your Kaithe for |COUNT| meters', 4500, 1500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyIDecree': ('I Decree', 'Collect |COUNT| Decrees in a single Duviri session, or run of The Circuit.', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyIsolationBounties': ('Vault Raider', 'Complete an Isolation Vault Bounty Mission in the Cambion Drift on Deimos.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyKillArchgunEnemies': ('Heavy Ordnance', 'Kill |COUNT| enemies with an Archgun', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyKillEnemies': ('Not a Warning Shot', '', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyKillEnemiesInMech': ('Necralizer', 'Kill |COUNT| enemies with a Necramech.', 4500, 100),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyKillEnemiesWithHeadshots': ('Not a Warning Shot', 'Kill |COUNT| Enemies with head shots.', 4500, 100),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyKillEximus': ('Eximus Eliminator', '', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyKillThumper': ("Tusk Thumpin'", 'Kill a Tusk Thumper in the Plains of Eidolon', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyLoyalty': ('Loyalty', 'Gain a total of |COUNT| Standing across all Syndicate factions.', 4500, 5000),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyMadLab': ('Mad Lab', '', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyMineRarePlainsResources': ('Earth Miner', 'Mine |COUNT| Rare Gems or Ore in the Plains of Eidolon.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyMineRareVenusResources': ('Venus Miner', 'Mine |COUNT| Rare Gems or Ore in the Orb Vallis.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyNightAndDay': ('Night and Day', 'Collect |COUNT| Vome or Fass Residue in the Cambion Drift.', 4500, 10),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyOpenLockers': ('Kleptomaniac', 'Open |COUNT| Lockers.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPerfectAnimalCapture': ('Conservationist', 'Complete |COUNT| different Perfect Animal Captures in Orb Vallis.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions': ('Mission Complete', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions10': ('Mission Complete X', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions11': ('Mission Complete XI', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions12': ('Mission Complete XII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions13': ('Mission Complete XIII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions14': ('Mission Complete XIV', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions15': ('Mission Complete XV', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions16': ('Mission Complete XVI', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions17': ('Mission Complete XVII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions18': ('Mission Complete XVIII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions19': ('Mission Complete XIX', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions2': ('Mission Complete II', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions20': ('Mission Complete XX', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions21': ('Mission Complete XXI', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions22': ('Mission Complete XXII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions23': ('Mission Complete XXIII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions24': ('Mission Complete XXIV', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions25': ('Mission Complete XXV', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions3': ('Mission Complete III', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions4': ('Mission Complete IV', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions5': ('Mission Complete V', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions6': ('Mission Complete VI', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions7': ('Mission Complete VII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions8': ('Mission Complete VIII', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentCompleteMissions9': ('Mission Complete IX', 'Complete |COUNT| missions', 4500, 15),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies': ('Not A Warning Shot', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies10': ('Not A Warning Shot X', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies11': ('Not A Warning Shot XI', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies12': ('Not A Warning Shot XII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies13': ('Not A Warning Shot XIII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies14': ('Not A Warning Shot XIV', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies15': ('Not A Warning Shot XV', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies16': ('Not A Warning Shot XVI', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies17': ('Not A Warning Shot XVII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies18': ('Not A Warning Shot XVIII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies19': ('Not A Warning Shot XIX', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies2': ('Not A Warning Shot II', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies20': ('Not A Warning Shot XX', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies21': ('Not A Warning Shot XXI', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies22': ('Not A Warning Shot XXII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies23': ('Not A Warning Shot XXIII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies24': ('Not A Warning Shot XXIV', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies25': ('Not A Warning Shot XXV', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies3': ('Not A Warning Shot III', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies4': ('Not A Warning Shot IV', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies5': ('Not A Warning Shot V', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies6': ('Not A Warning Shot VI', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies7': ('Not A Warning Shot VII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies8': ('Not A Warning Shot VIII', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEnemies9': ('Not A Warning Shot IX', 'Kill |COUNT| enemies.', 4500, 500),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus': ('Eximus Eliminator', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus10': ('Eximus Eliminator X', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus11': ('Eximus Eliminator XI', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus12': ('Eximus Eliminator XII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus13': ('Eximus Eliminator XIII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus14': ('Eximus Eliminator XIV', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus15': ('Eximus Eliminator XV', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus16': ('Eximus Eliminator XVI', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus17': ('Eximus Eliminator XVII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus18': ('Eximus Eliminator XVIII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus19': ('Eximus Eliminator XIX', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus2': ('Eximus Eliminator II', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus20': ('Eximus Eliminator XX', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus21': ('Eximus Eliminator XXI', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus22': ('Eximus Eliminator XXII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus23': ('Eximus Eliminator XXIII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus24': ('Eximus Eliminator XXIV', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus25': ('Eximus Eliminator XXV', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus3': ('Eximus Eliminator III', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus4': ('Eximus Eliminator IV', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus5': ('Eximus Eliminator V', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus6': ('Eximus Eliminator VI', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus7': ('Eximus Eliminator VII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus8': ('Eximus Eliminator VIII', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPermanentKillEximus9': ('Eximus Eliminator IX', 'Kill |COUNT| Eximus.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPerplexed': ('Perplexed', 'Complete a puzzle in Duviri', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPickUpMedallion': ('The Hunt is On', 'Find |COUNT| Syndicate Medallions. You can search by yourself or with a squad.', 4500, 10),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPickUpRareMods': ('Enhance!', '', 4500, 8),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyPlainsBounties': ('Earth Bounty Hunter', 'Complete |COUNT| Bounties in the Plains of Eidolon.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyRailjackHijackDestroyThree': ('Friendly Fire', '', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyRailjackMissions': ('Explorer', 'Complete |COUNT| Railjack Missions', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyRequiemTotem': ('Ancient Obelisks', 'Activate 3 Requiem Obelisks on Deimos.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyRifleOffense': ('Rifle Offense', 'Kill |COUNT| enemies with a Primary Rifle.', 4500, 150),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklySabotageCaches': ('Cache Hunter', 'Find |COUNT| caches across any mission.', 4500, 6),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklySanctuaryOnslaught': ('Test Subject', 'Complete |COUNT| Zones of Sanctuary Onslaught.', 4500, 8),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyShotgunOffense': ('Shotgun Offense', 'Kill |COUNT| enemies with a Primary Shotgun.', 4500, 150),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklySimarisScan': ('Sanctuary Researcher', 'Complete |COUNT| Scans for Cephalon Simaris.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklySkeletonsInTheCloset': ('Skeletons in the Closet', 'Kill |COUNT| Dax enemies in Duviri', 4500, 50),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklySniperOffense': ('Sniper Offense', 'Kill |COUNT| enemies with a Primary Sniper Rifle.', 4500, 150),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklySolveCiphers': ('Hacker', 'Hack |COUNT| Consoles.', 4500, 10),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyTheOldWays': ('The Old Ways', 'Complete 1 mission with only a single pistol and a glaive equipped.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyToAshes': ('To Ashes', 'Dissolve an Arcane for Vosfor.', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyUnlockDragonVaults': ('Vault Looter', 'Unlock |COUNT| Dragon Key vaults on Deimos.', 4500, 4),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyUnlockRelics': ('Unlock Relics', 'Unlock |COUNT| Relics', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyUseForma': ('Polarized', 'Polarize a Weapon, Companion, or Warframe (not in Simulacrum).', 4500, 1),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyVenusBounties': ('Venus Bounty Hunter', 'Complete |COUNT| Bounties in the Orb Vallis.', 4500, 3),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyVoidNet': ('Void Net', 'Collect |COUNT| Void Traces.', 4500, 30),
    '/Lotus/Types/Challenges/Seasons/Weekly/SeasonWeeklyZarimanBountyHunter': ('Zariman Bounty Hunter', '', 4500, 4),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardAntiquarian': ('Antiquarian', 'Open one of each era/tier of Relic (Lith, Meso, Neo, Axi) |COUNT|.', 7000, 4),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardBattleHardened': ('Battle Hardened', 'Complete The Perita Rebellion with |COUNT| or more orders completed.', 7000, 6),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCeremonialEvolution': ('Ceremonial Evolution', 'Activate the Incarnon Form of any Incarnon weapon in-mission |COUNT| times', 7000, 5),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCollectUniqueResources': ('Resource Scavenger', 'Collect |COUNT| different types of resources.', 7000, 20),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCompleteArchonHunt': ('Archon Hunter', 'Complete an Archon Hunt.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCompleteConquest': ('Voluntary Specimen', 'Complete a run of Deep Archimedea or Temporal Archimedea', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCompleteNightmareMissions': ('Night Terror', 'Complete |COUNT| Nightmare missions of any type.', 7000, 5),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCompleteSortie': ('Sortie Expert', 'Complete |COUNT| Sorties.', 7000, 3),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardCompleteSteelPathMissions': ('The Path Less Travelled', 'Complete |COUNT| different Steel Path Missions.', 7000, 5),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardElementaryMyDear': ('Elementary, My Dear', 'Complete |COUNT| successful Conversions in the Alchemy game mode.', 7000, 3),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardEliteBeastSlayer': ('Elite Beast Slayer', 'Defeat the Orowyrm in Steel Path', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardEliteSanctuaryOnslaught': ('Elite Test Subject', 'Complete |COUNT| Zones of Elite Sanctuary Onslaught.', 7000, 8),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardEternalGuardian': ('Eternal Guardian', 'Complete |COUNT| Void Armageddon rounds.', 7000, 2),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardExterminateNoAlarm': ('Silent Eliminator', '', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFallenAngel': ('Fallen Angel', 'Defeat |COUNT| Void Angels in the Zariman. ', 7000, 3),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFastCapture': ('Speedster', 'Finish a Capture mission in less than 90 seconds', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFinishLich': ('Choose Wisely', '', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFriendsDefense': ('Defense', 'Complete a Defense mission reaching at least Wave 12.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFriendsMirrorDefense': ('Mirror, Mirror', 'Defend both Mirror Defense objectives 3 times each in a single mission.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFriendsProfitTaker': ('Profit-Taker', 'Kill the Profit-Taker.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardFriendsSurvival': ('Survival', 'Complete a Survival mission reaching at least 20 minutes.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardGoingDown': ('Going Down', 'Defeat Roathe in Descendia.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardHighGround': ('High Ground', 'Close |COUNT| Ruptures in Void Flood', 7000, 12),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardHoldTheMushrooms': ('Hold the Mushrooms', 'Collect |COUNT| mushrooms from the Deepmines.', 7000, 6),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardIndexWinStreak': ('Day Trader', 'Win 3 wagers in a row in one match of The Index.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillEnemies': ('Kill Shot', 'Kill |COUNT| Enemies.', 7000, 1500),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillEnemiesSteelPath': ('Cold Steel', 'Kill |COUNT| Enemies on The Steel Path.', 7000, 1000),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillEximus': ('Eximus Executioner', 'Kill |COUNT| Eximus.', 7000, 100),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillExploiterOrb': ('Nothing but Profit', 'Kill the Exploiter Orb', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillOrCaptureRainalyst': ('Hydrolyst Hunter', 'Kill or Capture an Eidolon Hydrolyst.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillRopalolyst': ('Flying High', '', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillSilverGroveSpecters': ('Grove Guardian', '', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillTank': ('Tanked', 'Defeat an H-09 Efervon Tank', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKillThumper': ('Walk Without Rhythm', 'Kill a Tusk Thumper Doma in the Plains of Eidolon', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardKuvaSurvivalNoCapsules': ('Hold Your Breath', 'Survive for over 20 minutes in Kuva Survival.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardLuaPuzzles': ('Ascendant', 'Complete |COUNT| Halls of Ascension on Lua.', 7000, 4),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardPerplexed': ('Perplexed', '', 7000, 3),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardRailjackMissions': ('Elite Explorer', 'Complete |COUNT| Railjack Missions', 7000, 6),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardRiseOfTheMachine': ('Rise of the Machine', 'Kill |COUNT| enemies using a Necramech without getting destroyed.', 7000, 300),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardSpiderHunt': ('Spider Hunt', 'Complete an Isleweaver Duviri Mission.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardSpyManualNoAlarms': ('Machine Interference', '', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardTerminated': ('Terminated', 'Destroy |COUNT| Necramech vault guardians.', 7000, 3),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardTheManyMadeWhole': ('The Many Made Whole', 'Exchange 10 Riven Slivers for a Riven Mod', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardThePriceOfFreedom': ('The Price of Freedom', 'Free one Captured Solaris using a Granum Crown.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardTreasureFromThePast': ('Treasure From The Past', 'Complete a Netracell.', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardUnlockRelics': ('Unlock Relics', 'Unlock |COUNT| Relics', 7000, 10),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardVitalArbiter': ('Vital Arbiter', 'Complete an Arbitration Mission', 7000, 1),
    '/Lotus/Types/Challenges/Seasons/WeeklyHard/SeasonWeeklyHardZarimanBountyHunter': ('Zariman Bounty Hunter', 'Complete |COUNT| Bounties in the Zariman.', 7000, 4),
}
# fmt: on
# --- GENERATED END"


def _load(pep_dir: str, name: str) -> dict:
    with open(os.path.join(pep_dir, name), encoding="utf-8") as f:
        return json.load(f)


def build_tables(pep_dir: str) -> tuple[dict, dict, dict]:
    regions = _load(pep_dir, "ExportRegions.json")
    mts = _load(pep_dir, "ExportMissionTypes.json")
    chal = _load(pep_dir, "ExportChallenges.json")
    d = _load(pep_dir, "dict.en.json")

    def res(v) -> str:
        if not isinstance(v, str) or not v:
            return ""
        return v if not v.startswith("/") else (d.get(v) or "")

    nodes = {}
    for nid, r in regions.items():
        name = res(r.get("name"))
        if not name:
            continue
        mission = res(r.get("missionName"))
        nodes[nid] = (name, res(r.get("systemName")), r.get("missionType") or "",
                      title_case(mission) if mission else "", r.get("faction") or "",
                      int(r.get("minEnemyLevel") or 0), int(r.get("maxEnemyLevel") or 0))
    mission_types = {k: title_case(res(v.get("name"))) for k, v in mts.items() if res(v.get("name"))}
    # Nightwave acts and 1999 calendar challenges: the only ones the world state names.
    challenges = {}
    for path, e in chal.items():
        if "/Challenges/Seasons/" not in path and "/Challenges/Calendar1999/" not in path:
            continue
        title = res(e.get("name"))
        if not title:
            continue
        desc_key = e.get("description") or (e.get("name") or "").replace("_Name", "_Description")
        standing = e.get("standing")
        challenges[path] = (title, res(desc_key), standing if isinstance(standing, int) and standing >= 0 else None,
                            int(e.get("requiredCount") or 0))
    return nodes, mission_types, challenges


def _render(nodes: dict, mission_types: dict, challenges: dict, source: str) -> str:
    out = [_BEGIN + f" from {source} (python -m wfh.data.solnodes build)", "# fmt: off",
           "# node id: (name, system, MT_ type, mission label, faction, min level, max level)",
           "NODES: dict[str, tuple[str, str, str, str, str, int, int]] = {"]
    out += [f"    {k!r}: {tuple(v)!r}," for k, v in sorted(nodes.items())]
    out += ["}", "", "MISSION_TYPES: dict[str, str] = {"]
    out += [f"    {k!r}: {v!r}," for k, v in sorted(mission_types.items())]
    out += ["}", "", "# challenge path: (title, description template, standing, required count)",
            "CHALLENGES: dict[str, tuple[str, str, int | None, int]] = {"]
    out += [f"    {k!r}: {tuple(v)!r}," for k, v in sorted(challenges.items())]
    out += ["}", "# fmt: on", _END]
    return "\n".join(out)


def build(pep_dir: str) -> None:
    try:
        with open(os.path.join(pep_dir, "package.json"), encoding="utf-8") as f:
            source = "warframe-public-export-plus " + json.load(f).get("version", "?")
    except (OSError, ValueError):
        source = "warframe-public-export-plus"
    block = _render(*build_tables(pep_dir), source)
    path = os.path.abspath(__file__)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    start = text.index(_BEGIN)
    end = text.index(_END) + len(_END)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text[:start] + block + text[end:])
    os.replace(tmp, path)
    print(f"wrote {path}")


# --- GENERATED BEGIN
NODES: dict[str, tuple[str, str, str, str, str, int, int]] = {}
MISSION_TYPES: dict[str, str] = {}
CHALLENGES: dict[str, tuple[str, str, int | None, int]] = {}
# --- GENERATED END


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "build":
        build(sys.argv[2])
    else:
        print("usage: python -m wfh.data.solnodes build <unpacked warframe-public-export-plus dir>")
