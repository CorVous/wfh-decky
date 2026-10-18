"""Riven grading: roll floats, letter grades, the 44bananas good-roll sheet and inventory
fingerprint decoding (ports of WFHelper services/rivenGrading.ts, rivenConstants.ts,
rivenBestAttributes.ts, config/shared/rivenGoodRolls.ts and services/rivenFingerprint.ts).

Graded stat = scanned stat + {"grade": "S".."F"|"?", "rollFloat": 0..1}.
Grade result = {"stats": [...], "overallGrade", "attributeGrade": "Great|Good|OK|Bad|?"}.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from typing import Any, Callable

from .. import log
from . import riven_data as rd

_log = log.get("riven.grading")

# ---- constants (rivenConstants.ts) ---------------------------------------------------

NUM_BUFFS_ATTEN = [0, 1, 0.66000003, 0.5, 0.40000001, 0.34999999]
NUM_BUFFS_CURSE_ATTEN = [0, 1, 0.33000001, 0.5, 1.25, 1.5]  # indexed by number of BUFFS
SPECIFIC_FIT_ATTEN = 1.5
BASE_DRAIN = 10
NON_PERCENTAGE_TAGS = {
    "WeaponFactionDamageGrineer", "WeaponFactionDamageCorpus", "WeaponFactionDamageInfested",
    "WeaponMeleeFactionDamageGrineer", "WeaponMeleeFactionDamageCorpus", "WeaponMeleeFactionDamageInfested",
    "WeaponMeleeComboInitialBonusMod", "ComboDurationMod", "WeaponMeleeRangeIncMod", "WeaponPunctureDepthMod",
}
MAX_RIVEN_MOD_RANK = 8
GRADE_THRESHOLDS = [(9.5, "S"), (7.5, "A+"), (5.5, "A"), (3.5, "A-"), (1.5, "B+"), (-1.5, "B"), (-3.5, "B-"),
                    (-5.5, "C+"), (-7.5, "C"), (-9.5, "C-")]
GRADE_ORDER = ["F", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+", "S"]
FIT_TOLERANCE = 0.02
CORRECTION_MISFIT_THRESHOLD = 0.1
MAX_RIVEN_BUFFS = 3
MAX_RIVEN_CURSES = 1

STAT_CONFUSION_SIBLINGS = {
    "WeaponDamageAmountMod": ["WeaponMeleeDamageMod", "WeaponCritDamageMod"],
    "WeaponMeleeDamageMod": ["WeaponDamageAmountMod", "WeaponCritDamageMod"],
    "WeaponCritDamageMod": ["WeaponMeleeDamageMod", "WeaponDamageAmountMod"],
    "WeaponCritChanceMod": ["SlideAttackCritChanceMod", "WeaponStunChanceMod"],
    "SlideAttackCritChanceMod": ["WeaponCritChanceMod"],
    "WeaponStunChanceMod": ["WeaponCritChanceMod"],
}


def is_multiplier_tag(tag: str) -> bool:
    return tag in NON_PERCENTAGE_TAGS and ("FactionDamage" in tag or tag == "WeaponMeleeComboInitialBonusMod")


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def inverse_lerp(a: float, b: float, v: float) -> float:
    return 0 if b == a else (v - a) / (b - a)


def clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def float_to_grade(roll_float: float, is_curse: bool) -> str:
    f = 1 - roll_float if is_curse else roll_float
    score = lerp(-10, 10, f)
    for lo, grade in GRADE_THRESHOLDS:
        if score >= lo:
            return grade
    return "F"


def grade_rank(grade: str | None) -> int:
    """Comparable rank of a letter grade (-1 for "?" / unknown)."""
    try:
        return GRADE_ORDER.index(grade or "")
    except ValueError:
        return -1


def unparse_buff(displayed: float, base: float, disposition: float, num_buffs: int, num_curses: int,
                 tag: str | None = None, lvl: int = MAX_RIVEN_MOD_RANK) -> float:
    buffs_atten = NUM_BUFFS_ATTEN[min(num_buffs, len(NUM_BUFFS_ATTEN) - 1)]
    curse_atten = 1.25 ** num_curses
    att = SPECIFIC_FIT_ATTEN * disposition * BASE_DRAIN
    v = displayed if tag and tag in NON_PERCENTAGE_TAGS else displayed / 100
    if base == 0 or att == 0 or buffs_atten == 0 or curse_atten == 0:
        return 0.5
    v /= lvl + 1
    v /= buffs_atten
    v /= curse_atten
    v /= att
    v /= abs(base)
    return (v - 0.9) / 0.2


def unparse_curse(displayed: float, base: float, disposition: float, num_buffs: int, num_curses: int,
                  tag: str | None = None, lvl: int = MAX_RIVEN_MOD_RANK) -> float:
    att = SPECIFIC_FIT_ATTEN * disposition * BASE_DRAIN
    curses_in_buff = NUM_BUFFS_ATTEN[min(num_curses, len(NUM_BUFFS_ATTEN) - 1)]
    buffs_in_curse = NUM_BUFFS_CURSE_ATTEN[min(num_buffs, len(NUM_BUFFS_CURSE_ATTEN) - 1)]
    v = abs(displayed) if tag and tag in NON_PERCENTAGE_TAGS else abs(displayed) / 100
    if base == 0 or att == 0 or curses_in_buff == 0 or buffs_in_curse == 0:
        return 0.5
    v /= lvl + 1
    v /= curses_in_buff
    v /= buffs_in_curse
    v /= att
    v /= abs(base)
    return (v - 0.9) / 0.2


def plausible_stat_counts(nb: int, nc: int) -> list[tuple[int, int]]:
    """Scanned counts are a lower bound: a dropped line reads the survivors high."""
    return [(b, c) for b in range(nb, max(nb, MAX_RIVEN_BUFFS) + 1) for c in range(nc, max(nc, MAX_RIVEN_CURSES) + 1)]


def weapon_damage_tag(tag: str, melee: bool) -> str:
    if melee and tag == "WeaponDamageAmountMod":
        return "WeaponMeleeDamageMod"
    if not melee and tag == "WeaponMeleeDamageMod":
        return "WeaponDamageAmountMod"
    return tag


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def correct_scanned_stats(weapon: str, stats: list[dict], db: rd.RivenDb | None = None) -> list[dict]:
    """Fix OCR stat-name confusion against what can roll on the weapon (correctScannedStats)."""
    db = db or rd.db()
    base_disp = db.disposition(weapon)
    key = db.resolve_riven_type(weapon)
    if base_disp is None or not key or not stats:
        return stats
    melee = db.is_melee(weapon)
    counts = plausible_stat_counts(sum(1 for s in stats if s["positive"]), sum(1 for s in stats if not s["positive"]))
    dispositions = [base_disp] + [d for _n, d in db.family_variants(weapon)]

    def violation_for(tag: str, stat: dict, value: float) -> float | None:
        entry = db.find_upgrade_entry(key, tag)
        if not entry:
            return None
        if (not entry.can_be_buff) if stat["positive"] else (not entry.can_be_curse):
            return None
        best = math.inf
        for disp in dispositions:
            for nb, nc in counts:
                for lvl in range(MAX_RIVEN_MOD_RANK + 1):
                    f = (unparse_buff(value, entry.base_value, disp, nb, nc, tag, lvl) if stat["positive"]
                         else unparse_curse(value, entry.base_value, disp, nb, nc, tag, lvl))
                    best = min(best, max(0, f - 1) + max(0, -f))
        return best

    measured = []
    for original in stats:
        stat = original
        tag = rd.stat_name_to_tag(original["name"])
        renamed = False
        if tag:
            norm = weapon_damage_tag(tag, melee)
            if norm != tag:
                stat = {**stat, "name": db.stat_display_name(norm, melee)}
                tag, renamed = norm, True
        value = stat.get("value")
        if tag is None or not _finite(value) or stat.get("multiplier"):
            measured.append((original, stat, renamed, tag, None, None, False))
            continue
        v = violation_for(tag, stat, value)
        measured.append((original, stat, renamed, tag, value, v, v is None or v > CORRECTION_MISFIT_THRESHOLD))

    misfitting = [m for m in measured if m[6]]
    if len(misfitting) > 1:
        _log.info("%d of %d stats out of range for %r - weapon or disposition is wrong, keeping scanned names",
                  len(misfitting), len(measured), weapon)
        return [m[0] for m in measured]

    out = []
    for _orig, stat, _renamed, tag, value, violation, misfits in measured:
        if not misfits or tag is None or value is None:
            out.append(stat)
            continue
        siblings = list(dict.fromkeys(weapon_damage_tag(s, melee) for s in STAT_CONFUSION_SIBLINGS.get(tag, [])))
        fit = []
        for sib in siblings:
            if sib == tag:
                continue
            v = violation_for(sib, stat, value)
            if v is not None and v <= FIT_TOLERANCE:
                fit.append(sib)
        if len(fit) == 1:
            new_name = db.stat_display_name(fit[0], melee)
            _log.info("%r %s misfits %r - corrected to %r", stat["name"], value, weapon, new_name)
            out.append({**stat, "name": new_name})
        else:
            out.append(stat)
    return out


def grade_riven(weapon: str, stats: list[dict], mod_rank: int | None = None,
                db: rd.RivenDb | None = None, good_rolls: "GoodRolls | None" = None) -> dict | None:
    """gradeRiven: per-stat roll float + grade, overall grade, attribute grade. None when ungradeable."""
    if not stats:
        return None
    db = db or rd.db()
    base_disp = db.disposition(weapon)
    if base_disp is None:
        return None
    key = db.resolve_riven_type(weapon)
    if not key:
        return None
    nb = sum(1 for s in stats if s["positive"])
    nc = len(stats) - nb
    stated = mod_rank if isinstance(mod_rank, int) and 0 <= mod_rank <= MAX_RIVEN_MOD_RANK else None
    level = stated if stated is not None else MAX_RIVEN_MOD_RANK
    a_buffs, a_curses = nb, nc
    if nb > MAX_RIVEN_BUFFS or nc > MAX_RIVEN_CURSES:
        return None

    prepared = []
    for s in stats:
        tag = rd.stat_name_to_tag(s["name"])
        entry = db.find_upgrade_entry(key, tag) if tag else None
        is_fraction = bool(tag) and tag in NON_PERCENTAGE_TAGS
        displayed = None
        half = 0.05
        v = s.get("value")
        if _finite(v):
            if s.get("multiplier"):
                frac = v - 1 if s["positive"] else 1 - v
                displayed = frac if is_fraction else frac * 100
                half = 0.005 if is_fraction else 0.5
            else:
                displayed = v
        prepared.append({"stat": s, "tag": tag, "entry": entry, "displayed": displayed, "half": half})

    def raw_at(p, disp, lvl, b, c, value=None):
        value = p["displayed"] if value is None else value
        e = p["entry"]
        if p["stat"]["positive"]:
            return unparse_buff(value, e.base_value, disp, b, c, p["tag"], lvl)
        return unparse_curse(value, e.base_value, disp, b, c, p["tag"], lvl)

    def fits_at(p, disp, lvl, b, c) -> bool:
        x = raw_at(p, disp, lvl, b, c, p["displayed"] - p["half"])
        y = raw_at(p, disp, lvl, b, c, p["displayed"] + p["half"])
        return min(x, y) <= 1 + FIT_TOLERANCE and max(x, y) >= -FIT_TOLERANCE

    disposition = base_disp
    gradeable = [p for p in prepared if p["tag"] and p["entry"] and p["displayed"] is not None]
    if gradeable:
        def violation_at(disp, lvl, b, c):
            total = 0.0
            for p in gradeable:
                f = raw_at(p, disp, lvl, b, c)
                total += max(0, f - 1) + max(0, -f)
            return total

        def all_fit(disp, lvl, b, c):
            return all(fits_at(p, disp, lvl, b, c) for p in gradeable)

        if not all_fit(disposition, level, a_buffs, a_curses):
            candidates = [(weapon, base_disp)] + db.family_variants(weapon)
            search = list(range(MAX_RIVEN_MOD_RANK, -1, -1)) if len(gradeable) >= 2 else []
            refit_lvls = [stated] + [l for l in search if l != stated] if stated is not None else search

            def refit_at(b, c):
                for lvl in refit_lvls:
                    best = None
                    for name, disp in candidates:
                        if not all_fit(disp, lvl, b, c):
                            continue
                        if best is None or abs(disp - base_disp) < abs(best[1] - base_disp):
                            best = (name, disp, lvl, b, c)
                    if best:
                        return best
                return None

            refit = None
            for b, c in plausible_stat_counts(nb, nc):
                refit = refit_at(b, c)
                if refit:
                    break
            if refit:
                disposition, level, a_buffs, a_curses = refit[1], refit[2], refit[3], refit[4]
            else:
                best = [weapon, disposition, a_buffs, a_curses, violation_at(disposition, level, a_buffs, a_curses)]
                for b, c in plausible_stat_counts(nb, nc):
                    for name, disp in candidates:
                        viol = violation_at(disp, level, b, c)
                        closer = abs(disp - base_disp) < abs(best[1] - base_disp)
                        if viol < best[4] - 1e-9 or (viol < best[4] + 1e-9 and closer):
                            best = [name, disp, b, c, viol]
                disposition, a_buffs, a_curses = best[1], best[2], best[3]

    graded = []
    score_sum = 0.0
    scored = 0
    scored_buffs = clamped_buffs = 0
    for p in prepared:
        s = p["stat"]
        if not p["tag"] or not p["entry"]:
            graded.append({**s, "grade": "B", "rollFloat": 0.5})
            scored += 1
            continue
        if p["displayed"] is not None:
            f = clamp01(raw_at(p, disposition, level, a_buffs, a_curses))
            grade = float_to_grade(f, not s["positive"])
            score = lerp(-10, 10, 1 - f if not s["positive"] else f)
            graded.append({**s, "grade": grade, "rollFloat": f})
            if s["positive"]:
                scored_buffs += 1
                if f in (0, 1):
                    clamped_buffs += 1
            score_sum += score
            scored += 1
        else:
            graded.append({**s, "grade": "?", "rollFloat": 0.5})

    if stated is not None and scored_buffs > 0 and clamped_buffs == scored_buffs:
        searched = grade_riven(weapon, stats, None, db, good_rolls)
        if searched:
            return searched

    overall = "?"
    if scored > 0:
        overall = float_to_grade(inverse_lerp(-10, 10, score_sum / scored), False)
    attr = (good_rolls or goodrolls()).attribute_grade(stats, weapon, db)
    return {"stats": graded, "overallGrade": overall, "attributeGrade": attr,
            "disposition": disposition, "rank": level}


def grade_scanned(weapon: str, stats: list[dict], db: rd.RivenDb | None = None) -> dict | None:
    """tryGradeStats: correct confused names, then grade (None for an unknown weapon)."""
    if not weapon or weapon == "Riven" or not stats:
        return None
    db = db or rd.db()
    return grade_riven(weapon, correct_scanned_stats(weapon, stats, db), db=db)


def overall_score(result: dict | None) -> float | None:
    """Mean lerp score of a grade result (for comparing old vs new)."""
    if not result:
        return None
    vals = []
    for s in result.get("stats", []):
        if s.get("grade") in (None, "?"):
            continue
        f = s.get("rollFloat", 0.5)
        vals.append(lerp(-10, 10, 1 - f if not s.get("positive") else f))
    return sum(vals) / len(vals) if vals else None


# ---- good-roll sheet (rivenGoodRolls.ts, rivenBestAttributes.ts) ----------------------

SHEET_ID = "1LJ83e4x_xIVgjZg049PKrDgTex2MIuaiX0FFZi9mH-Q"
SHEET_TABS = [(0, "primary"), (1505239276, "secondary"), (1413904270, "melee"), (289737427, "archgun"),
              (965095749, "robotic")]
CACHE_MAX_AGE_S = 7 * 24 * 3600
REFETCH_COOLDOWN_S = 10 * 60
SHEET_FETCH_TIMEOUT_S = 20
SHEET_ONLY_PREFIXES = ["Coda ", "Dex ", "Mara ", "Carmine ", "Ceti ", "Prime "]
CACHE_FILE = "riven-good-rolls-cache.json"

SHEET_TAG = {
    "CC": "WeaponCritChanceMod", "CD": "WeaponCritDamageMod", "MS": "WeaponFireIterationsMod",
    "FR": "WeaponFireRateMod", "AS": "WeaponFireRateMod", "RLS": "WeaponReloadSpeedMod",
    "SC": "WeaponStunChanceMod", "SD": "WeaponProcTimeMod", "PT": "WeaponPunctureDepthMod",
    "MAG": "WeaponClipMaxMod", "AMMO": "WeaponAmmoMaxMod", "REC": "WeaponRecoilReductionMod",
    "ZOOM": "WeaponZoomFovMod", "PFS": "WeaponProjectileSpeedMod", "IMP": "WeaponImpactDamageMod",
    "PUNC": "WeaponArmorPiercingDamageMod", "SLASH": "WeaponSlashDamageMod", "COLD": "WeaponFreezeDamageMod",
    "HEAT": "WeaponFireDamageMod", "ELEC": "WeaponElectricityDamageMod", "TOX": "WeaponToxinDamageMod",
    "DTG": "WeaponFactionDamageGrineer", "DTC": "WeaponFactionDamageCorpus", "DTI": "WeaponFactionDamageInfested",
    "RANGE": "WeaponMeleeRangeIncMod", "IC": "WeaponMeleeComboInitialBonusMod",
    "EFF": "WeaponMeleeComboEfficiencyMod", "SLIDE": "SlideAttackCritChanceMod",
    "FIN": "WeaponMeleeFinisherDamageMod",
}
ELEMENT_TAGS = [SHEET_TAG["HEAT"], SHEET_TAG["COLD"], SHEET_TAG["ELEC"], SHEET_TAG["TOX"]]


def _tags_for(abbrev: str, klass: str) -> list[str]:
    key = abbrev.upper().strip()
    if not key:
        return []
    if key == "DMG":
        return ["WeaponMeleeDamageMod" if klass == "melee" else "WeaponDamageAmountMod"]
    if key == "ELEMENT":
        return list(ELEMENT_TAGS)
    return [SHEET_TAG[key]] if key in SHEET_TAG else []


def parse_csv(text: str) -> list[list[str]]:
    """RFC-ish CSV: "" escapes a quote inside quotes, \\r ignored, empty rows dropped."""
    rows: list[list[str]] = [[]]
    fld = ""
    quoted = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quoted:
            if ch == '"' and i + 1 < n and text[i + 1] == '"':
                fld += '"'
                i += 1
            elif ch == '"':
                quoted = False
            else:
                fld += ch
        elif ch == '"':
            quoted = True
        elif ch == ",":
            rows[-1].append(fld)
            fld = ""
        elif ch == "\n":
            rows[-1].append(fld)
            rows.append([])
            fld = ""
        elif ch != "\r":
            fld += ch
        i += 1
    if fld or rows[-1]:
        rows[-1].append(fld)
    return [r for r in rows if len(r) > 1 or (len(r) == 1 and r[0])]


def _parse_positives(cell: str, klass: str) -> list[dict]:
    rolls = []
    for alt in re.split(r"\s+or\s+", re.sub(r"\s+", " ", cell or "").strip(), flags=re.I):
        mandatory: dict[str, None] = {}
        optional: dict[str, None] = {}
        for token in [t for t in alt.split() if t]:
            target = optional if "/" in token else mandatory
            for part in token.split("/"):
                for tag in _tags_for(part, klass):
                    target[tag] = None
        for tag in mandatory:
            optional.pop(tag, None)
        if mandatory or optional:
            rolls.append({"mandatory": list(mandatory), "optional": list(optional)})
    return rolls


def _parse_negatives(cell: str, klass: str) -> list[str]:
    out: dict[str, None] = {}
    for token in re.split(r"[/\s]+", cell or ""):
        for tag in _tags_for(token, klass):
            out[tag] = None
    return list(out)


def parse_good_roll_csv(csv: str, klass: str) -> list[tuple[str, dict]]:
    rows = parse_csv(csv.lstrip("﻿"))
    header = [h.lower().strip() for h in (rows[0] if rows else [])]

    def col(prefix):
        return next((i for i, h in enumerate(header) if h.startswith(prefix)), -1)
    wc, pc, nc = col("weapon"), col("positive"), col("negative")
    if wc < 0 or pc < 0 or nc < 0:
        raise ValueError(f"bad riven good-rolls header for {klass}: {', '.join(rows[0]) if rows else '<empty>'}")
    out = []
    for row in rows[1:]:
        get = lambda i: row[i] if i < len(row) else ""  # noqa: E731
        name = get(wc).strip().lower()
        if not name:
            continue
        good = _parse_positives(get(pc), klass)
        bad = _parse_negatives(get(nc), klass)
        if good or bad:
            out.append((name, {"goodAttrs": good, "acceptedBadAttrs": bad}))
    return out


def _normalize_for_slug(value: str) -> str:
    s = re.sub(r"['’‘]", "", value.strip().lower())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


class GoodRolls:
    """The sheet, cached on disk for 7 days; a failed refresh keeps any cache, even stale."""

    def __init__(self, cache_dir: str | None = None, fetch: Callable[[str], bytes] | None = None,
                 clock: Callable[[], float] = time.time):
        self.cache_dir = cache_dir
        self._fetch = fetch
        self.clock = clock
        self.data: dict[str, dict] | None = None
        self.updated_at: float | None = None
        self._family: dict[str, str] | None = None
        self._lock = threading.Lock()
        self._loading = threading.Event()
        self._loading.set()
        self._last_fetch = -1e18
        self.last_error: str | None = None

    # cache
    @property
    def cache_path(self) -> str | None:
        return os.path.join(self.cache_dir, CACHE_FILE) if self.cache_dir else None

    def set_data(self, data: dict[str, dict], updated_at: float | None) -> None:
        self.data = data
        self.updated_at = updated_at
        self._family = None

    def _read_cache(self) -> tuple[dict, float] | None:
        p = self.cache_path
        if not p:
            return None
        try:
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            ts = raw.get("updatedAt")
            data = {k: v for k, v in (raw.get("data") or {}).items()
                    if isinstance(v, dict) and isinstance(v.get("goodAttrs"), list)
                    and isinstance(v.get("acceptedBadAttrs"), list)}
            if not data or not ts:
                return None
            if isinstance(ts, str):
                ts = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            return data, float(ts)
        except (OSError, ValueError, TypeError):
            return None

    def _write_cache(self) -> None:
        p = self.cache_path
        if not p or self.data is None:
            return
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.updated_at or self.clock()))
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"updatedAt": iso, "data": self.data}, f)
            os.replace(tmp, p)
        except OSError as e:
            _log.warning("good-roll cache write failed: %s", e)

    def load_cache_if_needed(self) -> None:
        if self.data is not None:
            return
        cached = self._read_cache()
        if cached:
            self.set_data(*cached)

    def has_sheet(self) -> bool:
        return bool(self.data)

    def stale(self) -> bool:
        return self.updated_at is not None and self.clock() - self.updated_at > CACHE_MAX_AGE_S

    # network
    def _get(self, url: str) -> bytes:
        if self._fetch:
            return self._fetch(url)
        from .. import net
        return net.fetch(url, timeout=SHEET_FETCH_TIMEOUT_S, headers={"Accept": "text/csv"})

    def fetch_sheet(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for gid, klass in SHEET_TABS:
            url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}"
            csv = self._get(url).decode("utf-8", errors="replace")
            for name, entry in parse_good_roll_csv(csv, klass):
                out.setdefault(name, entry)
        return out

    def refresh(self) -> bool:
        try:
            fresh = self.fetch_sheet()
            self.set_data(fresh, self.clock())
            self._write_cache()
            self.last_error = None
            _log.info("loaded %d riven good-roll rows", len(fresh))
            return True
        except Exception as e:  # noqa: BLE001 - offline / blocked: keep what we have
            self.last_error = str(e)
            cached = self._read_cache()
            if cached:
                self.set_data(*cached)
                _log.warning("using cached riven good-rolls%s: %s", " (stale)" if self.stale() else "", e)
            elif not self.has_sheet():
                self.set_data({}, None)
                _log.warning("no riven good-rolls data available: %s", e)
            return False

    def ensure_loaded(self, force: bool = False, wait: bool | None = None) -> None:
        """Refresh when missing or stale; blocks only when nothing is loaded (or force)."""
        self.load_cache_if_needed()
        loaded = self.has_sheet()
        if loaded and not force and not self.stale():
            return
        waits = force or not loaded if wait is None else wait
        with self._lock:
            busy = not self._loading.is_set()
            if not busy:
                if not force and self.clock() - self._last_fetch < REFETCH_COOLDOWN_S:
                    return
                self._last_fetch = self.clock()
                self._loading.clear()
        if busy:
            if waits:
                self._loading.wait(SHEET_FETCH_TIMEOUT_S * len(SHEET_TABS))
            return
        if waits:
            try:
                self.refresh()
            finally:
                self._loading.set()
        else:
            def run():
                try:
                    self.refresh()
                finally:
                    self._loading.set()
            threading.Thread(target=run, name="riven-goodrolls", daemon=True).start()

    def status(self) -> dict:
        self.load_cache_if_needed()
        return {"rows": len(self.data or {}), "updatedAt": int(self.updated_at * 1000) if self.updated_at else None,
                "stale": self.stale(), "error": self.last_error}

    # lookup
    def _family_index(self, db: rd.RivenDb) -> dict[str, str]:
        if self._family is None:
            idx: dict[str, str] = {}
            for k in (self.data or {}):
                slug = _normalize_for_slug(k.replace("&", " and "))
                if slug and slug not in idx:
                    idx[slug] = k
            self._family = idx
        return self._family

    def lookup_name(self, weapon: str, db: rd.RivenDb | None = None) -> str | None:
        if not weapon:
            return None
        self.load_cache_if_needed()
        if not self.data:
            return None
        lc = re.sub(r"\s+", " ", weapon.lower()).strip()
        if lc in self.data:
            return lc
        folded = lc
        for p in SHEET_ONLY_PREFIXES:
            if lc.startswith(p.lower()):
                folded = lc[len(p):].strip()
                break
        if not folded:
            return None
        if folded != lc and folded in self.data:
            return folded
        db = db or rd.db()
        slug = db.family_slug(folded)
        if not slug:
            return None
        return self._family_index(db).get(slug)

    def get(self, weapon: str, db: rd.RivenDb | None = None) -> dict | None:
        k = self.lookup_name(weapon, db)
        return self.data.get(k) if k and self.data else None

    def best_attributes(self, weapon: str, melee: bool = False, db: rd.RivenDb | None = None) -> dict | None:
        data = self.get(weapon, db)
        if not data:
            return None
        seen: set[str] = set()
        pos: list[str] = []
        for part in ("mandatory", "optional"):
            for roll in data["goodAttrs"]:
                for tag in roll[part]:
                    if tag not in seen:
                        seen.add(tag)
                        pos.append(rd.stat_tag_to_display_name(tag, melee))
        neg = [rd.stat_tag_to_display_name(t, melee) for t in data["acceptedBadAttrs"]]
        return {"positives": pos, "negatives": neg}

    def attribute_grade(self, stats: list[dict], weapon: str, db: rd.RivenDb | None = None) -> str:
        data = self.get(weapon, db)
        if not data:
            return "?"
        good = [rd.stat_name_to_tag(s["name"]) or s["name"] for s in stats if s["positive"]]
        bad = [rd.stat_name_to_tag(s["name"]) or s["name"] for s in stats if not s["positive"]]
        return grade_from_good_rolls(data, good, bad)


def grade_from_good_rolls(data: dict, good_tags: list[str], bad_tags: list[str]) -> str:
    groups = data["goodAttrs"]
    negative = []
    for tag in bad_tags:
        if tag in data["acceptedBadAttrs"]:
            negative.append("Good")
        elif any(tag in g["mandatory"] or tag in g["optional"] for g in groups):
            negative.append("Bad")
        else:
            negative.append("NotHelping")
    positive = []
    for tag in good_tags:
        if any(tag in g["mandatory"] for g in groups):
            positive.append("Decisive")
        elif any(tag in g["optional"] for g in groups):
            positive.append("Good")
        else:
            positive.append("NotHelping")
    good_set = set(good_tags)
    flag = any(all(m in good_set for m in g["mandatory"]) and all(t in set(g["mandatory"]) | set(g["optional"])
                                                                   for t in good_tags) for g in groups)
    num = sum(1 for p in positive if p in ("Decisive", "Good"))
    if "Bad" in negative:
        return "OK" if (flag and num >= 2) or num >= 3 else "Bad"
    if "NotHelping" in negative:
        return "Good" if flag or num >= 2 else "OK" if num >= 1 else "Bad"
    if flag:
        return "Great" if num >= 2 and negative else "Good"
    return "Good" if num >= 2 else "OK" if num >= 1 else "Bad"


_goodrolls: GoodRolls | None = None


def goodrolls() -> GoodRolls:
    global _goodrolls
    if _goodrolls is None:
        cache_dir = None
        try:
            from ..app import get_app
            cache_dir = get_app().runtime_dir
        except Exception:  # noqa: BLE001
            pass
        _goodrolls = GoodRolls(cache_dir)
    return _goodrolls


def set_goodrolls(g: GoodRolls | None) -> None:
    global _goodrolls
    _goodrolls = g


# ---- inventory fingerprints (rivenFingerprint.ts) -----------------------------------

RIVEN_TYPE_LABELS = {
    "LotusRifleRandomModRare": "Rifle", "LotusShotgunRandomModRare": "Shotgun", "LotusPistolRandomModRare": "Pistol",
    "PlayerMeleeWeaponRandomModRare": "Melee", "LotusArchgunRandomModRare": "Archgun",
    "LotusModularPistolRandomModRare": "Kitgun", "LotusModularMeleeRandomModRare": "Zaw",
    "RawRifleRandomMod": "Rifle", "RawShotgunRandomMod": "Shotgun", "RawPistolRandomMod": "Pistol",
    "RawMeleeRandomMod": "Melee", "RawArchgunRandomMod": "Archgun", "RawModularPistolRandomMod": "Kitgun",
    "RawModularMeleeRandomMod": "Zaw",
}

CHALLENGE_DESCS = {
    "RandomizedKill": "Kill {n} Enemies",
    "RandomizedKillPassengers": "Kill {n} Enemies that are on a Dropship",
    "RandomizedKillFallingPilots": "Kill {n} Enemies with Headshots",
    "RandomizedFinisherKill": "Kill {n} Enemies with Finishers",
    "RandomizedHeadshot": "Kill {n} Enemies with Headshots",
    "RandomizedHeadshotGlide": "Get {n} Headshot kills in a single Aim Glide",
    "RandomizedStyleKill": "Kill {n} Enemies while Sliding",
    "RandomizedWallClingKillstreak": "Get {n} kills in a row while Wall Dashing or Wall Latching without touching the floor",
    "RandomizedHeadshotUnawareBallistas": "Kill {n} unalerted Tusk Ballistas with a Headshot",
    "RandomizedKillSentients": "Get the killing blow on {n} Sentients",
    "RandomizedLongRangeSniper": "Kill {n} Enemies with Headshots from at least 75m away",
    "RandomizedSkiffArcher": "Destroy {n} Dargyns in flight using a bow",
    "RandomizedAntiAntiAir": "Destroy {n} Vruush Turrets while in Archwing without dying or becoming downed",
    "RandomizedFlyingHeadshotSeries": "Land {n} consecutive headshots while in Archwing in the Plains of Eidolon",
    "RandomizedFindCaches": "Find {n} Caches",
    "RandomizedFindRareMedallions": "Pick up {n} Syndicate Medallions",
    "RandomizedFisherman": "Catch {n} fish without missing a throw",
    "SustainMeleeComboThree": "Sustain a 6x Melee Combo Multiplier for 30s",
    "HighSurvivalPacifist": "Complete a Survival mission with level 30 or higher enemies without killing anyone",
    "HighExterminationUndetected": "Complete an Extermination mission with level 30 or higher enemies without being detected",
    "HighPerfectDefense": "Complete a Defense mission with level 30 or higher enemies with the defense objective taking no damage",
    "HighSoloInterceptionHobbled": "Complete a Solo Interception mission with level 30 or higher enemies and a Hobbled Dragon Key equipped",
    "LimitedSynthesis": "Synthesize a Simaris target without using Traps or Abilities while having a Hobbled Dragon Key equipped",
    "PlainsTimedVariety": "Catch one fish, mine one gem or metal, and kill one enemy in 30 seconds",
    "KahlMissions": "Complete {n} Kahl missions",
    "DJRandomizedKill": "Kill {n} Enemies",
    "DJRandomizedFinisherKill": "Kill {n} Enemies with Finishers",
    "DJRandomizedHeadshot": "Kill {n} Enemies with Headshots",
    "DJRandomizedStyleKill": "Kill {n} Enemies while Sliding",
    "DJRandomizedWallClingKillstreak": "Get {n} kills in a row while Wall Dashing or Wall Latching without touching the floor",
    "DJRandomizedKillPassengers": "Kill {n} Enemies that are on a Dropship",
    "DJRandomizedHeadshotUnawareBallistas": "Kill {n} unalerted Tusk Ballistas with a Headshot",
    "DJRandomizedFindCaches": "Find {n} Caches",
}

COMPLICATION_DESCS = {
    "ResetOnDamageTaken": "without taking damage", "ResetOnDowned": "without dying or becoming downed",
    "ResetOnMissionFailure": "without failing a mission", "ResetOnAlarmRaised": "without raising any alarms",
    "ResetOnAllyDowned": "without an ally becoming downed",
    "ResetOnDisrupt": "without being disrupted by a Magnetic Status Effect",
    "ResetOnGearAirSupport": "without using air support", "ResetOnGearAmmoRestores": "without using ammo consumables",
    "ResetOnGearCipher": "without using ciphers", "ResetOnGearEnergyRestores": "without using energy consumables",
    "ResetOnGearHealthRestores": "without using health consumables",
    "ResetOnGearShieldRestores": "without using shield-restoring consumables",
    "ResetOnProc": "without getting afflicted by a Status Effect", "ResetOnNewDay": "in one day",
    "EquippedDamageDebuffKey": "with an Extinguished Dragon Key equipped",
    "EquippedHealthDebuffKey": "with a Bleeding Dragon Key equipped",
    "EquippedShieldDebuffKey": "with a Decaying Dragon Key equipped",
    "EquippedSpeedDebuffKey": "with a Hobbled Dragon Key equipped", "SoloPlayer": ", while alone or in Solo Mode",
    "PetPresent": "with an active pet present", "SentinelPresent": "with an active sentinel present",
    "Invisible": "while invisible", "AimGliding": "during Aim Glide", "Sliding": "while sliding",
    "Undetected": "while undetected",
}

MAX_ROLL_INT = 0x3FFFFFFF


def _js_round(x: float) -> float:
    return math.floor(x + 0.5)


def roll_int_to_float(i: Any) -> float:
    if not _finite(i):
        return 0.0
    f = i / MAX_ROLL_INT
    return f if 0.0 <= f <= 1.0 else 0.0


def _round_stat(raw: float, non_pct: bool) -> float:
    return _js_round(raw * 100) / 100 if non_pct else _js_round(raw * 1000) / 10


def buff_value_at(base, disp, f, nb, nc, lvl, non_pct, mult) -> float:
    if base == 0:
        return 1 if mult else 0
    raw = (base * SPECIFIC_FIT_ATTEN * disp * BASE_DRAIN * 1.25 ** nc * lerp(0.9, 1.1, f)
           * NUM_BUFFS_ATTEN[min(nb, 5)] * (lvl + 1))
    v = _round_stat(raw, non_pct)
    return _js_round((1 + v) * 100) / 100 if mult else v


def curse_value_at(base, disp, f, nb, nc, lvl, non_pct, mult) -> float:
    if base == 0:
        return 0
    raw = (abs(base) * SPECIFIC_FIT_ATTEN * disp * BASE_DRAIN * lerp(0.9, 1.1, f)
           * NUM_BUFFS_CURSE_ATTEN[min(nb, 5)] * NUM_BUFFS_ATTEN[min(nc, 5)] * (lvl + 1))
    mag = _round_stat(raw, non_pct)
    if mult:
        return _js_round((1 - mag) * 100) / 100
    return -mag if base > 0 else mag


def parse_fingerprint(raw: Any) -> dict | None:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(v, str):
            v = json.loads(v)
        return v if isinstance(v, dict) else None
    except (ValueError, TypeError):
        return None


def is_riven_item_type(t: str) -> bool:
    return "Randomized" in t or "RandomMod" in t


def type_label(item_type: str) -> str:
    for k, label in RIVEN_TYPE_LABELS.items():
        if k in item_type:
            return label
    return "Riven"


def _split_camel(name: str) -> str:
    return re.sub(r"([A-Z])", r" \1", name).strip()


def describe_challenge(ctype: str, required: int | None = None, complication: str | None = None) -> str:
    name = ctype.split("/")[-1] or ctype
    tpl = CHALLENGE_DESCS.get(name)
    desc = tpl.replace("{n}", str(required) if required is not None else "?") if tpl else _split_camel(name)
    if complication:
        comp = COMPLICATION_DESCS.get(complication.split("/")[-1])
        if comp:
            desc += " " + comp
    return desc


def describe_challenge_group(ctype: str) -> str:
    """The task without its count or complication, so like challenges share one group."""
    name = ctype.split("/")[-1] or ctype
    tpl = CHALLENGE_DESCS.get(name)
    if not tpl:
        return _split_camel(name)
    return re.sub(r"\s*\{n\}\s*", " ", tpl).strip()


def decode_riven(entry: dict, db: rd.RivenDb | None = None, good_rolls: GoodRolls | None = None) -> dict | None:
    db = db or rd.db()
    fp = parse_fingerprint(entry.get("UpgradeFingerprint"))
    if not fp or fp.get("challenge") or not fp.get("compat"):
        return None
    weapon = db.name_by_unique(fp["compat"])
    if not weapon:
        return None
    disp = db.disposition(weapon)
    key = db.resolve_riven_type(weapon)
    if disp is None or not key:
        return None
    melee = db.is_melee(weapon)
    lvl = fp.get("lvl") if isinstance(fp.get("lvl"), int) else 0
    buffs = fp.get("buffs") if isinstance(fp.get("buffs"), list) else []
    curses = fp.get("curses") if isinstance(fp.get("curses"), list) else []
    nb, nc = len(buffs), len(curses)
    stats = []
    total = 0.0
    for is_buff, group in ((True, buffs), (False, curses)):
        for b in group:
            tag = str(b.get("Tag", ""))
            f = roll_int_to_float(b.get("Value"))
            e = db.find_upgrade_entry(key, tag)
            base = e.base_value if e else 0
            non_pct = tag in NON_PERCENTAGE_TAGS
            mult = is_multiplier_tag(tag)
            fn = buff_value_at if is_buff else curse_value_at
            stats.append({
                "tag": tag, "name": db.stat_display_name(tag, melee),
                "displayValue": fn(base, disp, f, nb, nc, lvl, non_pct, mult),
                "maxRankValue": fn(base, disp, f, nb, nc, MAX_RIVEN_MOD_RANK, non_pct, mult),
                "rankValues": [fn(base, disp, f, nb, nc, r, non_pct, mult) for r in range(MAX_RIVEN_MOD_RANK + 1)],
                "rollFloat": f, "grade": float_to_grade(f, not is_buff), "positive": is_buff, "multiplier": mult,
            })
            total += f if is_buff else 1 - f
    avg = total / len(stats) if stats else 0.5
    attr = (good_rolls or goodrolls()).attribute_grade([{"name": s["name"], "positive": s["positive"]} for s in stats],
                                                       weapon, db)
    suffix = db.riven_suffix(key, [(str(b.get("Tag", "")), b.get("Value") or 0) for b in buffs])
    item_type = str(entry.get("ItemType", ""))
    return {
        "itemId": (entry.get("ItemId") or {}).get("$oid", "") if isinstance(entry.get("ItemId"), dict) else "",
        "weaponName": weapon, "weaponUniqueName": fp["compat"],
        "rivenName": f"{weapon} {suffix}" if suffix else weapon,
        "masteryReq": fp.get("lvlReq") if isinstance(fp.get("lvlReq"), int) else 0,
        "currentRank": lvl, "maxRank": MAX_RIVEN_MOD_RANK,
        "rerolls": fp.get("rerolls") if isinstance(fp.get("rerolls"), int) else 0,
        "polarity": fp.get("pol") or "", "disposition": disp, "stats": stats,
        "overallGrade": float_to_grade(avg, False), "attributeGrade": attr,
        "statPerfectness": total / len(stats) if stats else 0, "rivenType": type_label(item_type),
        "familySlug": db.family_slug(weapon),
    }


def decode_all_rivens(inventory: dict, db: rd.RivenDb | None = None, good_rolls: GoodRolls | None = None) -> dict:
    """{unveiled: [...], veiled: [...], veiledUnseen: [{itemType, label, count}]} from DE inventory JSON."""
    db = db or rd.db()
    unveiled, veiled = [], []
    unseen: dict[str, int] = {}
    for u in inventory.get("Upgrades") or []:
        if not isinstance(u, dict):
            continue
        t = u.get("ItemType")
        if not isinstance(t, str) or not is_riven_item_type(t):
            continue
        fp = parse_fingerprint(u.get("UpgradeFingerprint")) if u.get("UpgradeFingerprint") else None
        if fp is not None and (fp.get("challenge") or not fp.get("compat")):
            ent = {"itemType": t, "label": type_label(t)}
            ch = fp.get("challenge")
            if isinstance(ch, dict):
                if _finite(ch.get("Progress")):
                    ent["challengeProgress"] = ch["Progress"]
                if _finite(ch.get("Required")):
                    ent["challengeRequired"] = ch["Required"]
                if isinstance(ch.get("Type"), str):
                    ent["challengeType"] = ch["Type"]
                    ent["challengeDesc"] = describe_challenge(
                        ch["Type"], ent.get("challengeRequired"),
                        ch.get("Complication") if isinstance(ch.get("Complication"), str) else None)
                    ent["challengeGroup"] = describe_challenge_group(ch["Type"])
            veiled.append(ent)
            continue
        d = decode_riven(u, db, good_rolls)
        if d:
            unveiled.append(d)
    for u in inventory.get("RawUpgrades") or []:
        if not isinstance(u, dict):
            continue
        t = u.get("ItemType")
        if not isinstance(t, str) or not is_riven_item_type(t):
            continue
        n = u.get("ItemCount") if _finite(u.get("ItemCount")) else 1
        label = type_label(t)
        unseen[label] = unseen.get(label, 0) + int(n)
    return {"unveiled": unveiled, "veiled": veiled,
            "veiledUnseen": [{"itemType": k, "label": f"{k} Riven Mod", "count": v} for k, v in unseen.items()]}
