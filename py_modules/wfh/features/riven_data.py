"""Riven weapon and stat database (port of WFHelper services/rivenData.ts + shared tables).

Source data is `warframe-public-export-plus` (ExportWeapons.json,
ExportUpgrades.json, dict.en.json). scripts/fetch-data.sh trims it at build
time into data/riven/riven.json.gz (~60 KB):

    {"version", "source", "builtAt",
     "weapons": [[uniqueName, name, disposition, productCategory, holsterCategory, flags], ...],
     "types":   {riven mod key: {"compat", "entries": [[tag, canBeBuff, canBeCurse, baseValue,
                                                         displayName, prefix, suffix], ...]}}}

flags: 1 = "SHOTGUN" compat tag, 2 = "SENTINEL_WEAPON" compat tag, 4 = shotgun ancestor.
Weapons keep the export's order (display-name ties keep the later entry, as
WFHelper does). A newer copy in <runtime_dir>/data/riven (refresh()) wins.

Build: python -m wfh.features.riven_data build <unpacked package dir> <out dir>
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import tarfile
import threading
import time
from dataclasses import dataclass, field

from .. import log

_log = log.get("riven.data")

DATA_FILE = "riven.json.gz"
PEP_PACKAGE = "warframe-public-export-plus"
REGISTRY = "https://registry.npmjs.org/" + PEP_PACKAGE

FLAG_SHOTGUN_TAG = 1
FLAG_SENTINEL = 2
FLAG_SHOTGUN_FAMILY = 4

# ---- shared tables (docs/port/riven-scanner.md Appendix C) -------------------------

STAT_NAME_TO_TAG: dict[str, str] = {
    "critical chance": "WeaponCritChanceMod",
    "critical damage": "WeaponCritDamageMod",
    "multishot": "WeaponFireIterationsMod",
    "fire rate": "WeaponFireRateMod",
    "damage": "WeaponDamageAmountMod",
    "reload speed": "WeaponReloadSpeedMod",
    "status chance": "WeaponStunChanceMod",
    "status duration": "WeaponProcTimeMod",
    "punch through": "WeaponPunctureDepthMod",
    "magazine capacity": "WeaponClipMaxMod",
    "ammo maximum": "WeaponAmmoMaxMod",
    "weapon recoil": "WeaponRecoilReductionMod",
    "recoil": "WeaponRecoilReductionMod",
    "zoom": "WeaponZoomFovMod",
    "projectile speed": "WeaponProjectileSpeedMod",
    "impact": "WeaponImpactDamageMod",
    "puncture": "WeaponArmorPiercingDamageMod",
    "slash": "WeaponSlashDamageMod",
    "cold": "WeaponFreezeDamageMod",
    "heat": "WeaponFireDamageMod",
    "electricity": "WeaponElectricityDamageMod",
    "toxin": "WeaponToxinDamageMod",
    "damage to grineer": "WeaponFactionDamageGrineer",
    "damage to corpus": "WeaponFactionDamageCorpus",
    "damage to infested": "WeaponFactionDamageInfested",
    "melee damage": "WeaponMeleeDamageMod",
    "attack speed": "WeaponFireRateMod",
    "range": "WeaponMeleeRangeIncMod",
    "combo duration": "ComboDurationMod",
    "critical chance for slide attack": "SlideAttackCritChanceMod",
    "slide attack": "SlideAttackCritChanceMod",
    "finisher damage": "WeaponMeleeFinisherDamageMod",
    "heavy attack efficiency": "WeaponMeleeComboEfficiencyMod",
    "initial combo": "WeaponMeleeComboInitialBonusMod",
    "chance to gain combo count": "WeaponMeleeComboPointsOnHitMod",
    "additional combo count chance": "WeaponMeleeComboBonusOnHitMod",
}

# Reverse: tag -> title-cased first name (rivenData.ts TAG_TO_DISPLAY).
TAG_TO_DISPLAY: dict[str, str] = {}
for _name, _tag in STAT_NAME_TO_TAG.items():
    TAG_TO_DISPLAY.setdefault(_tag, " ".join(w[:1].upper() + w[1:] for w in _name.split(" ")))

STAT_TAG_TO_DISPLAY_NAME: dict[str, str] = {
    "WeaponCritChanceMod": "Critical Chance",
    "WeaponCritDamageMod": "Critical Damage",
    "WeaponFireIterationsMod": "Multishot",
    "WeaponFireRateMod": "Fire Rate",
    "WeaponDamageAmountMod": "Damage",
    "WeaponMeleeDamageMod": "Melee Damage",
    "WeaponReloadSpeedMod": "Reload Speed",
    "WeaponStunChanceMod": "Status Chance",
    "WeaponProcTimeMod": "Status Duration",
    "WeaponPunctureDepthMod": "Punch Through",
    "WeaponClipMaxMod": "Magazine Capacity",
    "WeaponAmmoMaxMod": "Ammo Maximum",
    "WeaponRecoilReductionMod": "Weapon Recoil",
    "WeaponZoomFovMod": "Zoom",
    "WeaponProjectileSpeedMod": "Projectile Speed",
    "WeaponImpactDamageMod": "Impact",
    "WeaponArmorPiercingDamageMod": "Puncture",
    "WeaponSlashDamageMod": "Slash",
    "WeaponFreezeDamageMod": "Cold",
    "WeaponFireDamageMod": "Heat",
    "WeaponElectricityDamageMod": "Electricity",
    "WeaponToxinDamageMod": "Toxin",
    "WeaponFactionDamageGrineer": "Damage to Grineer",
    "WeaponFactionDamageCorpus": "Damage to Corpus",
    "WeaponFactionDamageInfested": "Damage to Infested",
    "WeaponMeleeRangeIncMod": "Range",
    "ComboDurationMod": "Combo Duration",
    "SlideAttackCritChanceMod": "Slide Attack",
    "WeaponMeleeFinisherDamageMod": "Finisher Damage",
    "WeaponMeleeComboEfficiencyMod": "Heavy Attack Efficiency",
    "WeaponMeleeComboInitialBonusMod": "Initial Combo",
    "WeaponMeleeComboPointsOnHitMod": "Chance to Gain Combo Count",
    "WeaponMeleeComboBonusOnHitMod": "Additional Combo Count Chance",
}

TAG_TO_WFM_URL_NAME: dict[str, str] = {
    "WeaponCritChanceMod": "critical_chance",
    "WeaponCritDamageMod": "critical_damage",
    "WeaponFireIterationsMod": "multishot",
    "WeaponFireRateMod": "fire_rate_/_attack_speed",
    "WeaponDamageAmountMod": "base_damage_/_melee_damage",
    "WeaponReloadSpeedMod": "reload_speed",
    "WeaponStunChanceMod": "status_chance",
    "WeaponProcTimeMod": "status_duration",
    "WeaponPunctureDepthMod": "punch_through",
    "WeaponClipMaxMod": "magazine_capacity",
    "WeaponAmmoMaxMod": "ammo_maximum",
    "WeaponRecoilReductionMod": "recoil",
    "WeaponZoomFovMod": "zoom",
    "WeaponProjectileSpeedMod": "projectile_speed",
    "WeaponImpactDamageMod": "impact_damage",
    "WeaponArmorPiercingDamageMod": "puncture_damage",
    "WeaponSlashDamageMod": "slash_damage",
    "WeaponFreezeDamageMod": "cold_damage",
    "WeaponFireDamageMod": "heat_damage",
    "WeaponElectricityDamageMod": "electric_damage",
    "WeaponToxinDamageMod": "toxin_damage",
    "WeaponFactionDamageGrineer": "damage_vs_grineer",
    "WeaponFactionDamageCorpus": "damage_vs_corpus",
    "WeaponFactionDamageInfested": "damage_vs_infested",
    "WeaponMeleeDamageMod": "base_damage_/_melee_damage",
    "WeaponMeleeRangeIncMod": "range",
    "ComboDurationMod": "combo_duration",
    "SlideAttackCritChanceMod": "critical_chance_on_slide_attack",
    "WeaponMeleeFinisherDamageMod": "finisher_damage",
    "WeaponMeleeComboEfficiencyMod": "channeling_efficiency",
    "WeaponMeleeComboInitialBonusMod": "channeling_damage",
    "WeaponMeleeComboPointsOnHitMod": "chance_to_gain_combo_count",
    "WeaponMeleeComboBonusOnHitMod": "chance_to_gain_extra_combo_count",
    "WeaponMeleeFactionDamageGrineer": "damage_vs_grineer",
    "WeaponMeleeFactionDamageCorpus": "damage_vs_corpus",
    "WeaponMeleeFactionDamageInfested": "damage_vs_infested",
}

# weaponVariants.ts
VARIANT_SUFFIXES = [" Prime", " Wraith", " Vandal"]
VARIANT_PREFIXES = ["MK1-", "Kuva ", "Tenet ", "Prisma ", "Vaykor ", "Rakta ", "Secura ", "Sancti ", "Synoid ", "Telos "]

RIVEN_KEY_PREFIX = "/Lotus/Upgrades/Mods/Randomized/"
RIFLE_RIVEN_KEY = RIVEN_KEY_PREFIX + "LotusRifleRandomModRare"
PISTOL_RIVEN_KEY = RIVEN_KEY_PREFIX + "LotusPistolRandomModRare"
MELEE_RIVEN_KEY = RIVEN_KEY_PREFIX + "PlayerMeleeWeaponRandomModRare"
SHOTGUN_RIVEN_KEY = RIVEN_KEY_PREFIX + "LotusShotgunRandomModRare"
ARCHGUN_RIVEN_KEY = RIVEN_KEY_PREFIX + "LotusArchgunRandomModRare"
KITGUN_RIVEN_KEY = RIVEN_KEY_PREFIX + "LotusModularPistolRandomModRare"
ZAW_RIVEN_KEY = RIVEN_KEY_PREFIX + "LotusModularMeleeRandomModRare"

MODULAR_MELEE_PATH = re.compile(r"ModularMelee", re.I)
MODULAR_PISTOL_PATH = re.compile(r"SUModularSecondary|InfKitGun", re.I)

RIVEN_MODS_BY_CATEGORY = {
    "LongGuns": RIFLE_RIVEN_KEY,
    "Pistols": PISTOL_RIVEN_KEY,
    "Melee": MELEE_RIVEN_KEY,
    "SpaceGuns": ARCHGUN_RIVEN_KEY,
    "DrifterMelee": MELEE_RIVEN_KEY,
}
RIVEN_MODS_BY_HOLSTER = {
    "RIFLE": RIFLE_RIVEN_KEY, "SNIPER": RIFLE_RIVEN_KEY, "BOW": RIFLE_RIVEN_KEY,
    "SHOTGUN": SHOTGUN_RIVEN_KEY, "PISTOL": PISTOL_RIVEN_KEY, "MELEE": MELEE_RIVEN_KEY,
}
MELEE_CATEGORIES = {"Melee", "SpaceMelee", "DrifterMelee"}

WEAPON_OCR_ALIASES = {"euphona": "Euphona Prime", "gotva": "Gotva Prime", "reaper": "Reaper Prime"}
MIN_FUZZY_LABEL_CHARS = 5


def levenshtein(a: str, b: str) -> int:
    """Standard DP edit distance (rewardScannerUtils.levenshteinDistance)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def normalize_weapon_ocr_text(text: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def clean_loc_tag(raw: str) -> str:
    """"|val|% <DT_FIRE_COLOR>Heat" -> "Heat"; drops "(x2 for Bows)" qualifiers."""
    cleaned = re.sub(r"\|[^|]*\|[%s]?\s*", "", raw).strip()
    cleaned = re.sub(r"<[^>]+>", "", cleaned).strip()
    cleaned = re.sub(r"\s*\(.*?\)\s*", "", cleaned).strip()
    return cleaned


def stat_name_to_tag(stat_name: str) -> str | None:
    return STAT_NAME_TO_TAG.get(str(stat_name or "").lower().strip())


def stat_tag_to_display_name(tag: str, melee: bool = False) -> str:
    """config/shared/rivenStatDisplayNames.ts statTagToDisplayName."""
    if melee and tag == "WeaponFireRateMod":
        return "Attack Speed"
    return STAT_TAG_TO_DISPLAY_NAME.get(tag, tag)


def tag_to_wfm_url_name(tag: str) -> str | None:
    return TAG_TO_WFM_URL_NAME.get(tag)


# ---- database ----------------------------------------------------------------------

@dataclass(frozen=True)
class UpgradeEntry:
    tag: str
    can_be_buff: bool
    can_be_curse: bool
    base_value: float
    display_name: str
    prefix: str
    suffix: str


@dataclass(frozen=True)
class WeaponInfo:
    unique_name: str
    name: str
    disposition: float
    product_category: str
    holster_category: str
    flags: int

    @property
    def shotgun_tag(self) -> bool:
        return bool(self.flags & FLAG_SHOTGUN_TAG)

    @property
    def sentinel(self) -> bool:
        return bool(self.flags & FLAG_SENTINEL)

    @property
    def shotgun_family(self) -> bool:
        return bool(self.flags & FLAG_SHOTGUN_FAMILY)


def _name_rank(category: str) -> int:
    # Duviri's Drifter twins of six Tenno melee weapons must never win the name key.
    return 0 if category == "DrifterMelee" else 1


@dataclass
class RivenDb:
    version: str = ""
    weapons: list[WeaponInfo] = field(default_factory=list)
    types: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_name_lc: dict[str, WeaponInfo] = {}
        self.display_names: dict[str, str] = {}
        self.display_norm: dict[str, str] = {}
        self.by_unique: dict[str, str] = {}
        self.entries: dict[str, list[UpgradeEntry]] = {}
        self.tag_display: dict[str, str] = {}
        for w in self.weapons:
            lc = w.name.lower()
            cur = self.by_name_lc.get(lc)
            if cur is None or _name_rank(w.product_category) >= _name_rank(cur.product_category):
                self.by_name_lc[lc] = w
            self.display_names[lc] = w.name
            self.display_norm[normalize_weapon_ocr_text(w.name)] = w.name
            self.by_unique[w.unique_name] = w.name
        for key, t in self.types.items():
            ents = []
            for e in t.get("entries", []):
                ue = UpgradeEntry(str(e[0]), bool(e[1]), bool(e[2]), float(e[3]), str(e[4] or ""), str(e[5] or ""),
                                  str(e[6] or ""))
                ents.append(ue)
                if ue.display_name and ue.tag not in self.tag_display:
                    self.tag_display[ue.tag] = ue.display_name
            self.entries[key] = ents
        self._slug_cache: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self.by_name_lc)

    # lookups
    def weapon(self, name: str) -> WeaponInfo | None:
        return self.by_name_lc.get(str(name or "").lower())

    def name_by_unique(self, unique_name: str) -> str | None:
        return self.by_unique.get(unique_name)

    def disposition(self, name: str) -> float | None:
        w = self.weapon(name)
        return w.disposition if w else None

    def is_melee(self, name: str) -> bool:
        w = self.weapon(name)
        if not w:
            return False
        if MODULAR_MELEE_PATH.search(w.unique_name):
            return True
        return w.product_category in MELEE_CATEGORIES or w.holster_category == "MELEE"

    def resolve_riven_type(self, name: str) -> str | None:
        w = self.weapon(name)
        if not w:
            return None
        cat = w.product_category
        if cat == "LongGuns" and (w.holster_category == "SHOTGUN" or w.shotgun_tag or w.shotgun_family):
            return SHOTGUN_RIVEN_KEY
        if MODULAR_MELEE_PATH.search(w.unique_name):
            return ZAW_RIVEN_KEY
        if MODULAR_PISTOL_PATH.search(w.unique_name):
            return KITGUN_RIVEN_KEY
        by_cat = RIVEN_MODS_BY_CATEGORY.get(cat)
        if by_cat:
            return by_cat
        if not w.sentinel:
            return None
        holster = w.holster_category or ("PISTOL" if re.search(r"Pistol$", w.unique_name, re.I) else "")
        return RIVEN_MODS_BY_HOLSTER.get(holster)

    def type_entries(self, key: str) -> list[UpgradeEntry]:
        return self.entries.get(key, [])

    def find_upgrade_entry(self, key: str, tag: str) -> UpgradeEntry | None:
        ents = self.type_entries(key)
        found = next((e for e in ents if e.tag == tag), None)
        if not found:
            alt = tag.replace("WeaponFaction", "WeaponMeleeFaction", 1)
            found = next((e for e in ents if e.tag == alt), None)
        if not found and tag == "WeaponDamageAmountMod":
            found = next((e for e in ents if e.tag == "WeaponMeleeDamageMod"), None)
        return found

    def stat_display_name(self, tag: str, melee: bool = False) -> str:
        if melee and tag == "WeaponFireRateMod":
            return "Attack Speed"
        return self.tag_display.get(tag) or TAG_TO_DISPLAY.get(tag) or tag

    def riven_suffix(self, key: str, buffs: list[tuple[str, float]]) -> str:
        """generateRivenSuffix: buffs (tag, fingerprint Value) sorted by Value desc."""
        if not self.type_entries(key) or not buffs:
            return ""

        def entry(tag):
            return self.find_upgrade_entry(key, tag)

        def title(s: str) -> str:
            return s[:1].upper() + s[1:].lower()

        def base(tag):
            e = entry(tag)
            return e.base_value if e else 0

        # Value descending; ties by baseValue ascending (stable, like Array.sort).
        ordered = sorted(buffs, key=lambda b: (-b[1], base(b[0])))
        if len(ordered) == 1:
            e = entry(ordered[0][0])
            if not e or not e.prefix:
                return ""
            return title(e.prefix) + (e.suffix or "").lower()
        name = ""
        for i, (tag, _v) in enumerate(ordered):
            e = entry(tag)
            if not e:
                continue
            if i == len(ordered) - 1:
                name += (e.suffix or "").lower()
            elif name == "":
                if e.prefix:
                    name += title(e.prefix)
            elif e.prefix:
                name += "-" + e.prefix.lower()
        return name

    # names
    def family_slug(self, weapon_name: str) -> str:
        """getRivenFamilySlug: "Boar Prime" -> "boar"; an affix strips only when the base exists."""
        cached = self._slug_cache.get(weapon_name)
        if cached is not None:
            return cached
        name = weapon_name.strip()
        for suffix in VARIANT_SUFFIXES:
            if not name.lower().endswith(suffix.lower()):
                continue
            base = name[: -len(suffix)]
            name = base if base.lower() in self.by_name_lc else name
            break
        for prefix in VARIANT_PREFIXES:
            if not name.lower().startswith(prefix.lower()):
                continue
            base = name[len(prefix):]
            name = base if base.lower() in self.by_name_lc else name
            break
        slug = re.sub(r"[^a-z0-9]+", "_", name.replace("&", " and ").lower().strip()).strip("_")
        if len(self._slug_cache) < 4096:
            self._slug_cache[weapon_name] = slug
        return slug

    def family_variants(self, weapon_name: str) -> list[tuple[str, float]]:
        slug = self.family_slug(weapon_name)
        if not slug:
            return []
        out = []
        for name in self.display_names.values():
            if self.family_slug(name) != slug:
                continue
            info = self.by_name_lc.get(name.lower())
            if info:
                out.append((name, info.disposition))
        return out

    def all_weapon_names(self) -> list[str]:
        return sorted(self.display_names.values(), key=lambda s: s.lower())

    def find_weapon_in_text(self, text: str) -> str | None:
        """Longest weapon name that starts a line (word boundary), else a fuzzy leading phrase."""
        line_starts = [ln.strip().lower() for ln in re.split(r"\r?\n", str(text or ""))]
        line_starts = [ln for ln in line_starts if ln]
        best_exact, best_len = None, 0
        for name_lc in self.by_name_lc:
            if len(name_lc) <= best_len or len(name_lc) < 3:
                continue
            for ln in line_starts:
                if ln.startswith(name_lc) and not re.match(r"[a-z0-9]", ln[len(name_lc):len(name_lc) + 1]):
                    best_exact = self.display_names.get(name_lc, name_lc)
                    best_len = len(name_lc)
                    break
        lines = [normalize_weapon_ocr_text(ln) for ln in re.split(r"\r?\n", str(text or ""))]
        lines = [ln for ln in lines if len(ln) >= 4][:4]
        for ln in lines:
            if ln in WEAPON_OCR_ALIASES:
                return WEAPON_OCR_ALIASES[ln]
        best = None  # (name, distance, tokens)
        for ln in lines:
            words = [w for w in ln.split(" ") if len(w) >= 2]
            if not words:
                continue
            for n in range(1, min(4, len(words)) + 1):
                phrase = " ".join(words[:n]).strip()
                if len(phrase) < 4:
                    continue
                if phrase in WEAPON_OCR_ALIASES:
                    return WEAPON_OCR_ALIASES[phrase]
                pw = len(phrase.split(" "))
                for norm, display in self.display_norm.items():
                    if not norm:
                        continue
                    ww = len(norm.split(" "))
                    if abs(pw - ww) > 1:
                        continue
                    max_d = 3 if len(norm) >= 14 else 2 if len(norm) >= 8 else 1
                    if abs(len(norm) - len(phrase)) > max_d:
                        continue  # cheap bound: distance >= length gap
                    d = levenshtein(phrase, norm)
                    if d > max_d:
                        continue
                    if best is None or d < best[1] or (d <= best[1] + 1 and ww > best[2]):
                        best = (display, d, ww)
        if best:
            if not best_exact:
                return best[0]
            if len(normalize_weapon_ocr_text(best[0]).split(" ")) > len(normalize_weapon_ocr_text(best_exact).split(" ")):
                return best[0]
        return best_exact or (best[0] if best else None)

    def find_weapon_by_label_line(self, lines: list[str]) -> tuple[str, bool] | None:
        """FITS IN caption: a whole line must name a weapon; (name, exact) or None."""
        best = None  # [name, exact, len, distance]
        for raw in lines:
            norm = normalize_weapon_ocr_text(raw)
            if len(norm) < 3:
                continue
            exact = self.display_norm.get(norm)
            if exact:
                if best is None or not best[1] or len(norm) > best[2]:
                    best = [exact, True, len(norm), 0]
                continue
            if len(norm) < MIN_FUZZY_LABEL_CHARS or (best and best[1]):
                continue
            max_d = 2 if len(norm) >= 14 else 1
            line_best, ambiguous = None, False
            for nw, display in self.display_norm.items():
                if abs(len(nw) - len(norm)) > max_d:
                    continue
                d = levenshtein(norm, nw)
                if d > max_d:
                    continue
                if line_best is None or d < line_best[1]:
                    line_best, ambiguous = (display, d), False
                elif d == line_best[1] and display != line_best[0]:
                    ambiguous = True
            if not line_best or ambiguous:
                continue
            if best is None or line_best[1] < best[3] or (line_best[1] == best[3] and len(norm) > best[2]):
                best = [line_best[0], False, len(norm), line_best[1]]
        return (best[0], best[1]) if best else None


# ---- loading -----------------------------------------------------------------------

_db: RivenDb | None = None
_db_lock = threading.Lock()
_dirs: tuple[str | None, str | None] | None = None  # (bundled data dir, runtime data dir) override


def set_data_dirs(bundled: str | None, runtime: str | None) -> None:
    """Tests / setup: where data/riven lives (bundled) and the refresh target (runtime)."""
    global _dirs, _db
    with _db_lock:
        _dirs = (bundled, runtime)
        _db = None


def _candidate_dirs() -> list[str]:
    if _dirs is not None:
        return [os.path.join(d, "riven") for d in _dirs if d]
    try:
        from ..app import get_app

        a = get_app()
        return [os.path.join(a.runtime_dir, "data", "riven"), os.path.join(a.data_dir, "riven")]
    except Exception:  # noqa: BLE001 - no app (CLI)
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        return [os.path.join(root, "data", "riven")]


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", str(v or "0")))


def _read(path: str) -> dict | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and data.get("weapons") else None
    except (OSError, ValueError) as e:
        if not isinstance(e, FileNotFoundError):
            _log.warning("riven data unreadable at %s: %s", path, e)
        return None


def from_payload(data: dict) -> RivenDb:
    weapons = [WeaponInfo(str(w[0]), str(w[1]), float(w[2]), str(w[3] or ""), str(w[4] or ""), int(w[5] or 0))
               for w in data.get("weapons", []) if isinstance(w, list) and len(w) >= 6]
    return RivenDb(str(data.get("version", "")), weapons, dict(data.get("types") or {}))


def db() -> RivenDb:
    """The riven database (empty when no data was built; every lookup then answers None)."""
    global _db
    if _db is not None:
        return _db
    with _db_lock:
        if _db is not None:
            return _db
        best = None
        for d in _candidate_dirs():
            data = _read(os.path.join(d, DATA_FILE))
            if data and (best is None or _version_key(data.get("version")) > _version_key(best.get("version"))):
                best = data
        if best is None:
            _log.warning("no riven data (run scripts/fetch-data.sh); grading is unavailable")
            _db = RivenDb()
        else:
            _db = from_payload(best)
            _log.info("riven data %s: %d weapons, %d riven types", _db.version, len(_db), len(_db.types))
        return _db


def reset() -> None:
    global _db
    with _db_lock:
        _db = None


def status() -> dict:
    d = db()
    return {"version": d.version or None, "weapons": len(d), "types": len(d.types)}


# ---- build -------------------------------------------------------------------------

def _has_shotgun_ancestor(unique: str, weapons: dict) -> bool:
    seen = set()
    cur = unique
    while cur and cur not in seen:
        seen.add(cur)
        if re.search(r"/Shotgun/", cur, re.I):
            return True
        parent = (weapons.get(cur) or {}).get("parentName")
        cur = parent if isinstance(parent, str) else None
    return False


def build_payload(weapons: dict, upgrades: dict, dictionary: dict, version: str) -> dict:
    out_w = []
    for unique, w in weapons.items():
        if not unique or unique == "default" or not isinstance(w, dict):
            continue
        name = w.get("name")
        if isinstance(name, str) and name.startswith("/"):
            name = dictionary.get(name)
        if not name or not isinstance(name, str):
            continue
        dispo = w.get("omegaAttenuation")
        if not isinstance(dispo, (int, float)) or isinstance(dispo, bool):
            continue
        tags = w.get("compatibilityTags") if isinstance(w.get("compatibilityTags"), list) else []
        flags = (FLAG_SHOTGUN_TAG if "SHOTGUN" in tags else 0) | (FLAG_SENTINEL if "SENTINEL_WEAPON" in tags else 0)
        if _has_shotgun_ancestor(unique, weapons):
            flags |= FLAG_SHOTGUN_FAMILY
        out_w.append([unique, name, dispo, w.get("productCategory") or "", w.get("holsterCategory") or "", flags])
    types = {}
    for key, mod in upgrades.items():
        ents = mod.get("upgradeEntries") if isinstance(mod, dict) else None
        if not isinstance(ents, list) or not ents or "Randomized" not in key:
            continue
        rows = []
        for ue in ents:
            vals = ue.get("upgradeValues") or [{}]
            base = (vals[0] or {}).get("value", 0) or 0
            loc = (vals[0] or {}).get("locTag")
            disp = clean_loc_tag(dictionary.get(loc, "") if loc else "")
            if not disp:
                disp = TAG_TO_DISPLAY.get(ue.get("tag"), ue.get("tag"))
            rows.append([ue.get("tag"), bool(ue.get("canBeBuff")), bool(ue.get("canBeCurse")), base, disp,
                         dictionary.get(ue.get("prefixTag"), "") if ue.get("prefixTag") else "",
                         dictionary.get(ue.get("suffixTag"), "") if ue.get("suffixTag") else ""])
        types[key] = {"compat": mod.get("compat") or "", "entries": rows}
    return {"version": version, "source": PEP_PACKAGE, "builtAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "weapons": out_w, "types": types}


def write_payload(payload: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, DATA_FILE)
    tmp = path + ".tmp"
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    with open(tmp, "wb") as f:
        f.write(gzip.compress(raw, 9, mtime=0))
    os.replace(tmp, path)
    return path


def build(pep_dir: str, out_dir: str) -> str:
    def load(name):
        with open(os.path.join(pep_dir, name), encoding="utf-8") as f:
            return json.load(f)
    version = "0"
    try:
        version = str(load("package.json").get("version", "0"))
    except OSError:
        pass
    payload = build_payload(load("ExportWeapons.json"), load("ExportUpgrades.json"), load("dict.en.json"), version)
    path = write_payload(payload, out_dir)
    _log.info("riven data %s: %d weapons, %d types -> %s", version, len(payload["weapons"]), len(payload["types"]), path)
    return path


WANTED_MEMBERS = {"package/ExportWeapons.json", "package/ExportUpgrades.json", "package/dict.en.json",
                  "package/package.json"}


def build_from_tarball(tgz: bytes) -> dict:
    """The three export files straight out of the npm tarball (no temp files)."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        for m in tar:
            if m.name in WANTED_MEMBERS and m.isfile():
                f = tar.extractfile(m)
                if f:
                    files[m.name] = f.read()
    missing = WANTED_MEMBERS - set(files)
    if missing - {"package/package.json"}:
        raise ValueError(f"tarball lacks {sorted(missing)}")
    version = json.loads(files.get("package/package.json", b"{}")).get("version", "0")
    return build_payload(json.loads(files["package/ExportWeapons.json"]), json.loads(files["package/ExportUpgrades.json"]),
                         json.loads(files["package/dict.en.json"]), str(version))


def refresh(runtime_data_dir: str, force: bool = False) -> dict:
    """Fetch the latest export from npm when it is newer than the loaded data (blocking)."""
    from .. import net

    meta = json.loads(net.fetch(REGISTRY + "/latest", timeout=20))
    latest = str(meta.get("version", ""))
    cur = db().version
    if not force and cur and _version_key(latest) <= _version_key(cur):
        return {"updated": False, "version": cur, "latest": latest}
    tarball = (meta.get("dist") or {}).get("tarball") or f"{REGISTRY}/-/{PEP_PACKAGE}-{latest}.tgz"
    payload = build_from_tarball(net.fetch(tarball, timeout=120))
    write_payload(payload, os.path.join(runtime_data_dir, "riven"))
    reset()
    return {"updated": True, "version": payload["version"], "latest": latest}


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 4 and sys.argv[1] == "build":
        print(build(sys.argv[2], sys.argv[3]))
    else:
        print("usage: python -m wfh.features.riven_data build <unpacked warframe-public-export-plus dir> <out dir>")
        sys.exit(2)
