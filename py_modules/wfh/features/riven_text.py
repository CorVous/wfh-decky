"""Riven OCR text -> stats (line-for-line port of WFHelper ipc/overlay/rivenScanText.ts and the
postprocessOcrText / mergeSplitLines part of services/rivenOcrOnnx.ts).

docs/port/riven-scanner.md Appendices A and B hold the TypeScript this follows.
JS regex semantics are kept with re.ASCII (\\b \\w \\d are ASCII in JS); JS \\s also
matches Unicode spaces, so those are folded to " " before parsing. JS "$n"
replacements are written as \\g<n>. Values are unsigned magnitudes; multipliers
keep the printed factor.

A stat is a dict: {"name", "positive", "value", ["displayPositive"], ["multiplier"]}.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .riven_data import levenshtein

A = re.ASCII
AI = re.ASCII | re.I
AM = re.ASCII | re.M
AIM = re.ASCII | re.I | re.M

RivenStat = dict[str, Any]

# ---- Appendix A tables -------------------------------------------------------------

RIVEN_STAT_ALIAS_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Dannage", AI), "Damage"),
    (re.compile(r"Darnage", AI), "Damage"),
    (re.compile(r"Darnoge", AI), "Damage"),
    (re.compile(r"\bDamage\s+to\s+Corpu\b", AI), "Damage to Corpus"),
    (re.compile(r"\bDamage\s+to\s+Infeste\b", AI), "Damage to Infested"),
    (re.compile(r"Crit\s*ical", AI), "Critical"),
    (re.compile(r"Cri tical", AI), "Critical"),
    (re.compile(r"Critica\b", AI), "Critical"),
    (re.compile(r"Multi\s*shot", AI), "Multishot"),
    (re.compile(r"Sta tus", AI), "Status"),
    (re.compile(r"Statuc", AI), "Status"),
    (re.compile(r"Re load", AI), "Reload"),
    (re.compile(r"Elec tricity", AI), "Electricity"),
    (re.compile(r"Punc ture", AI), "Puncture"),
    (re.compile(r"Pi[uo]ncture", AI), "Puncture"),
    (re.compile(r"Puincture", AI), "Puncture"),
    (re.compile(r"\bReload\s+Spe[de]\b", AI), "Reload Speed"),
    (re.compile(r"Maga zine", AI), "Magazine"),
    (re.compile(r"Capaclty", AI), "Capacity"),
    (re.compile(r"Maxinnunn", AI), "Maximum"),
    (re.compile(r"Maximu[rn]", AI), "Maximum"),
    (re.compile(r"Annnno", AI), "Ammo"),
    (re.compile(r"Mel[ae]e", AI), "Melee"),
    (re.compile(r"Fini sher", AI), "Finisher"),
    (re.compile(r"Finlsher", AI), "Finisher"),
    (re.compile(r"[>]?[lh]mpact", AI), "Impact"),
    (re.compile(r"\bG[Ll]ash\b", AI), "Slash"),
    (re.compile(r"\b\(Glash\b", AI), "Slash"),
    (re.compile(r"\bY\s*Puncture\b", AI), "Puncture"),
    (re.compile(r"\bA\s*Slash\b", AI), "Slash"),
    (re.compile(r"\bO\s*Cold\b", AI), "Cold"),
    (re.compile(r"\bO\s*Heat\b", AI), "Heat"),
    (re.compile(r"\bl\s*eat\b", AI), "Heat"),
    (re.compile(r"\bQ\s*Toxin\b", AI), "Toxin"),
    (re.compile(r"\bQ\s*Electricity\b", AI), "Electricity"),
    # The unique tail recovers "Additional" when PaddleOCR garbles or drops it.
    (re.compile(r"\b(?:[A-Za-z]{2,12}\s+)?Combo\s+Count\s+Chance\b", AI), "Additional Combo Count Chance"),
]

KNOWN_RIVEN_STATS: list[str] = [
    "Additional Combo Count Chance", "Chance to Gain Combo Count", "Critical Chance for Slide Attack",
    "Heavy Attack Efficiency", "Magazine Capacity", "Damage to Grineer", "Damage to Corpus", "Damage to Infested",
    "Critical Chance", "Critical Damage", "Finisher Damage", "Melee Damage", "Weapon Recoil", "Status Duration",
    "Status Chance", "Projectile Speed", "Reload Speed", "Attack Speed", "Flight Speed", "Fire Rate",
    "Punch Through", "Combo Duration", "Initial Combo", "Ammo Maximum", "Heavy Attack", "Channeling Damage",
    "Channeling Efficiency", "Multishot", "Electricity", "Corrosive", "Radiation", "Magnetic", "Cold", "Heat",
    "Toxin", "Viral", "Blast", "Gas", "Impact", "Puncture", "Slash", "Magazine", "Recoil", "Damage", "Range",
    "Slide", "Zoom",
]
_KNOWN_LC = [(s, s.lower()) for s in KNOWN_RIVEN_STATS]

MAX_REASONABLE_VALUE = 500
INVERTED_POLARITY_STATS = {"weapon recoil", "recoil"}

STAT_END_WORDS = "|".join(dict.fromkeys(s[s.rfind(" ") + 1:] for s in KNOWN_RIVEN_STATS))
# The padlock closing a trait-locked stat reads as one glued character ("Chancee").
TRAIT_LOCK_TAIL = re.compile(r"\b(" + STAT_END_WORDS + r")[^\s%)]$", AIM)

DIGIT_LOOKALIKES = {"g": "9", "q": "9", "l": "1", "I": "1", "O": "0", "S": "5", "B": "8"}
VALUE_WITH_LOOKALIKE = re.compile(
    r"([+\-–]\s*|\bx\s*)((?:\d[\dgqlIOSB]*|[gqlIOSB](?=\.))(?:\.[\dgqlIOSB]+)?)(?=[\s%]|$)", AM)
NUMBER_END = r"(?!\.?\d|[A-Za-z](?![A-Za-z]))"
X_VALUE = re.compile(r"x\s*(\d+\.?\d*)" + NUMBER_END, AI)
SIGNED_VALUE = re.compile(r"[+\-–]\s*(\d+\.?\d*)" + NUMBER_END, A)

_ELEMENTS = r"(?:Slash|Cold|Heat|Electricity|Toxin|Impact|Puncture|Radiation|Viral|Corrosive|Blast|Magnetic|Gas)\b"

_UNICODE_SPACE = re.compile(r"[^\S\n\r\t ]")


def _fold_spaces(text: str) -> str:
    """JS \\s matches Unicode spaces (U+3000, U+00A0 ...); Python's ASCII \\s does not."""
    return _UNICODE_SPACE.sub(" ", text) if not text.isascii() else text


def _sub(pattern: str, repl, text: str, flags: int = A, count: int = 0) -> str:
    return re.sub(pattern, repl, text, count=count, flags=flags)


def preprocess_ocr_text(raw: str) -> str:
    text = TRAIT_LOCK_TAIL.sub(r"\g<1>", raw)
    # The padlock leading a locked stat can read as a digit ("8 +99,5%").
    text = _sub(r"^\d(?=x\s*\d)", "", text, AIM)

    # Colored stat icons split two-word names; rejoin before other repairs.
    text = _sub(r"\bFinisher\s*\n+\s*(?=Damage\b)", "Finisher ", text, AI)
    text = _sub(r"\bMelee\s*\n+\s*(?=Damage\b)", "Melee ", text, AI)
    text = _sub(r"\bCritical?\s*\n+\s*(?=(?:Chance|Damage)\b)", "Critical ", text, AI)
    text = _sub(r"\bStatus\s*\n+\s*(?=(?:Chance|Duration)\b)", "Status ", text, AI)
    text = _sub(r"\bAttack\s*\n+\s*(?=Speed\b)", "Attack ", text, AI)
    text = _sub(r"\bReload\s*\n+\s*(?=Speed\b)", "Reload ", text, AI)
    text = _sub(r"\bFlight\s*\n+\s*(?=Speed\b)", "Flight ", text, AI)
    text = _sub(r"\bProjectile\s*\n+\s*(?=Speed\b)", "Projectile ", text, AI)
    text = _sub(r"\bFire\s*\n+\s*(?=Rate\b)", "Fire ", text, AI)
    text = _sub(r"\bCombo\s*\n+[^\w\n]*(?=(?:Duration|Count)\b)", "Combo ", text, AI)
    text = _sub(r"\bAdditional\s*\n+\s*(?=Combo\b)", "Additional ", text, AI)
    text = _sub(r"\bAmmo\s*\n+\s*(?=Maximum\b)", "Ammo ", text, AI)
    text = _sub(r"\bPunch\s*\n+\s*(?=Through\b)", "Punch ", text, AI)
    text = _sub(r"\bChanneling\s*\n+\s*(?=(?:Damage|Efficiency)\b)", "Channeling ", text, AI)
    text = _sub(r"\bWeapon\s*\n+\s*(?=Recoil\b)", "Weapon ", text, AI)
    text = _sub(r"\bHeavy\s*\n+\s*(?=Attack\b)", "Heavy ", text, AI)
    text = _sub(r"\bInitial\s*\n+\s*(?=Combo\b)", "Initial ", text, AI)
    text = _sub(r"\bMagazine\s*\n+\s*(?=Capacity\b)", "Magazine ", text, AI)
    text = _sub(r"\bDamage\s*\n+\s*(?=to\s+(?:Grineer|Corpus|Infested)\b)", "Damage ", text, AI)
    text = _sub(r"\bto\s*\n+\s*(?=(?:Grineer|Corpus|Infested)\b)", "to ", text, AI)
    text = text.replace("0/0", "%")
    text = _sub(r"O/O", "%", text, AI)
    text = text.replace("o/o", "%")
    text = _sub(r"(\d)\s*Z\b", r"\g<1>%", text)
    # "<1,32 Damage to Infeste" -> "x1.32 Damage to Infested".
    text = _sub(r"[<‹]\s*(\d+[.,]\d+)\s+(?=Damage\s+to\s+)", r"x\g<1> ", text, AI)
    text = _sub(r"\bx\s*O([.,]\d)", r"x0\g<1>", text, AI)
    text = _sub(r"\bx\s+[lI1]\s*[,.]\s*(\d+)", r"x1.\g<1>", text, AI)
    text = _sub(r"\bx\s+[lI1]\b", "x1", text, AI)
    text = _sub(r"\bx[lI]([,.]?\d)", r"x1\g<1>", text)
    text = _sub(r"\bx[lI]\b", "x1", text)
    text = _sub(r"\bx(\d)\s*,\s*(\d+)", r"x\g<1>.\g<2>", text)
    text = _sub(r"([+\-–]?\d+),\s+(\d+)\s*%", r"\g<1>.\g<2>%", text)
    text = _sub(r",(\d)", r".\g<1>", text)
    text = _sub(r"(x\d+)\n(\.\d+)", r"\g<1>\g<2>", text)
    text = _sub(r"\b(x\d+)\s+\.(\d+)", r"\g<1>.\g<2>", text)
    text = _sub(r"\b(x\d+)(\s+(?:Damage\s+to\s+\w+|[A-Z][a-z]+))\n\.(\d+)", r"\g<1>.\g<3>\g<2>", text)
    text = _sub(r"(x\d+)\n([1-9]\d?\s+(?:Damage|[A-Z]))", r"\g<1>.\g<2>", text)
    text = _sub(r"(\d)\s+\.(\d)", r"\g<1>.\g<2>", text)
    text = _sub(r"(\d)\s([1-9])\s*%", r"\g<1>.\g<2>%", text)

    for _ in range(5):
        text = _sub(r"([+\-–]\s*\d+)\s+(\d)", r"\g<1>\g<2>", text)
    for _ in range(5):
        text = _sub(r"(\d)\s+(\d)", r"\g<1>\g<2>", text)

    # Combo Duration prints a seconds unit, so its S is that unit, not a 5.
    text = _sub(r"(\d)S(?=\s+Combo\s+Dur)", r"\g<1>s", text)

    def lookalike(m: re.Match) -> str:
        lead, value = m.group(1), m.group(2)
        if re.search(r"\d", value, A):
            return lead + re.sub(r"[gqlIOSB]", lambda c: DIGIT_LOOKALIKES[c.group(0)], value)
        return m.group(0)

    text = VALUE_WITH_LOOKALIKE.sub(lookalike, text)
    text = _sub(r"(\d)[A-Za-z](\d)", r"\g<1>\g<2>", text)
    for _ in range(3):
        text = _sub(r"(\d)\s+(\d)", r"\g<1>\g<2>", text)

    for pattern, replacement in RIVEN_STAT_ALIAS_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    text = _sub(r"\(x\d+\s*(?:for\s*)?Heavy\s*Attack[a-z]*\)", "", text, AI)
    text = _sub(r"\(\s*x\d+\s*fo[a-z]*\s*Hea[a-z]*\s*\)?(?:\s*Attack[a-z]*\s*\)?)?", "", text, AI)
    text = _sub(r"\(\s*x\d+\s*fo[a-z]*\s*Bows?\s*\)?", "", text, AI)
    text = _sub(r"\(\s*x\d+\s*fo[a-z]*\s*$", "", text, AIM)
    text = _sub(r"\(\s*(\d+[.,]\d+)", r"x\g<1>", text)
    text = _sub(r"[*()\[\]{}|\\<>^~°©®™•→←↑↓↗↘►◄▸▾▲▼■□●○]+\s*", " ", text)
    text = _sub(r"\bx\d+\s*(?:for\s*)?Heavy\s*Attack[a-z]*\b", "", text, AI)
    text = _sub(r"%\s+[A-Z0-9]\s+(?=[A-Z])", "% ", text)
    # "+ A0,58 Damage to Grineer" -> "+0,58 Damage to Grineer"
    text = _sub(r"([+\-–]\s*)[A-Z]\s*(\d)", r"\g<1>\g<2>", text)
    # "Ao-102.5% Status Dura" -> "-102.5% ..."
    text = _sub(r"^[A-Za-z]{1,2}\s*(?=[+\-–]\s*\d)", "", text, AM)
    text = _sub(r"[0-9'\"`]\s*(?=" + _ELEMENTS + ")", "", text, AI)
    text = _sub(r"\b[A-Z]\s+(?=" + _ELEMENTS + ")", "", text)
    text = _sub(r"[^\w\s+.%\-x]{1,3}\s+(?=" + _ELEMENTS + ")", "", text, AI)
    text = _sub(r"Critical\s+Chance[^a-zA-Z]{0,20}for\s+Slide\s+Attack", "Critical Chance for Slide Attack", text, AI)
    text = _sub(r"(\d)s(?=\s|\Z)", r"\g<1>", text)
    text = _sub(r"\s+([+\-–]\d)", r"\n\g<1>", text)
    text = _sub(r"\s+(x\d)", r"\n\g<1>", text, AI)
    return text


def _js_num_str(v: float) -> str:
    """JS String(number) for the values the parser sees."""
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    return repr(float(v))


def _is_integer(v: float) -> bool:
    return math.isfinite(v) and v == int(v)


def sanitise_value(value: float) -> float:
    if value > MAX_REASONABLE_VALUE and _is_integer(value) and value >= 100:
        s = _js_num_str(value)
        try:
            corrected = float(s[:-1] + "." + s[-1:])
            if math.isfinite(corrected):
                return corrected
        except ValueError:
            pass
    # Merged OCR strips can prepend a digit; strip it only when the result is plausible.
    if value > 1000 and not _is_integer(value):
        s = _js_num_str(math.floor(value * 10 + 0.5) / 10)
        dot = s.find(".")
        int_part = s[:dot] if dot >= 0 else s
        dec_part = s[dot + 1:] if dot >= 0 else ""
        if len(int_part) > 3:
            try:
                corrected = float(int_part[1:] + ("." + dec_part if dec_part else ""))
            except ValueError:
                corrected = float("nan")
            if math.isfinite(corrected) and 0 < corrected <= MAX_REASONABLE_VALUE:
                return corrected
    return value


def _parse_float(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def extract_sign_and_value(fragment: str) -> dict | None:
    signs = list(re.finditer(r"[+\-–](?=\s*\d)", fragment, A))
    last = signs[-1].group(0) if signs else None
    positive = last is None or (last != "-" and last != "–")

    pct = list(re.finditer(r"(\d+\.?\d*)\s*%", fragment, A))
    if pct:
        v = _parse_float(pct[-1].group(1))
        if v is not None:
            return {"positive": positive, "value": sanitise_value(v)}

    xm = list(X_VALUE.finditer(fragment))
    if xm:
        v = _parse_float(xm[-1].group(1))
        if v is not None:
            return {"positive": v >= 1, "value": v, "multiplier": True}

    sv = list(SIGNED_VALUE.finditer(fragment))
    if sv:
        v = _parse_float(sv[-1].group(1))
        if v is not None:
            return {"positive": positive, "value": sanitise_value(v)}

    if signs or xm:
        return {"positive": positive, "value": None}
    return None


STAT_NAME_WORDS = set(w for s in KNOWN_RIVEN_STATS for w in s.lower().split(" ")) | {"bows", "attacks"}


def looks_stat_like(line: str) -> bool:
    return bool(re.search(r"[+\-–]\s*\d", line, A) or re.search(r"\bx\s*\d", line, AI))


def has_title_above_stats(cleaned: str) -> bool:
    for line in re.split(r"\r?\n", cleaned):
        if looks_stat_like(line):
            return False
        words = re.findall(r"[a-z]{4,}", line.lower())
        if any(w not in STAT_NAME_WORDS for w in words):
            return True
    return False


def _stat_score(stats: list[RivenStat]) -> int:
    return sum(10 if s["value"] is not None else 3 for s in stats)


def parse_riven_stats(text: str, diagnostics: dict | None = None) -> list[RivenStat]:
    """OCR text -> stats; diagnostics gets "droppedLines" (list) and "titleSeen" (bool)."""
    if not text:
        return []
    cleaned = preprocess_ocr_text(_fold_spaces(text))
    if diagnostics is not None:
        diagnostics.setdefault("droppedLines", [])
        diagnostics["titleSeen"] = has_title_above_stats(cleaned)
    line_dropped: list[str] = []
    line_results = parse_stats_from_lines(cleaned, line_dropped)
    if line_results and any(s["value"] is not None for s in line_results):
        if diagnostics is not None:
            diagnostics["droppedLines"].extend(line_dropped)
        return line_results
    blob = re.sub(r"\r?\n", " ", cleaned)
    blob_dropped: list[str] = []
    blob_results = parse_stats_from_lines(blob, blob_dropped)
    use_blob = _stat_score(blob_results) > _stat_score(line_results)
    if diagnostics is not None:
        diagnostics["droppedLines"].extend(blob_dropped if use_blob else line_dropped)
    return blob_results if use_blob else line_results


def line_contains_known_stat(line: str) -> bool:
    lc = line.lower()
    return any(s_lc in lc for _s, s_lc in _KNOWN_LC)


def prefix_distance(fragment: str, stat: str) -> int:
    """Edit distance from the fragment to the closest prefix of the stat name."""
    prev = list(range(len(stat) + 1))
    for i in range(1, len(fragment) + 1):
        cur = [i]
        for j in range(1, len(stat) + 1):
            sub = prev[j - 1] + (0 if fragment[i - 1] == stat[j - 1] else 1)
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, sub))
        prev = cur
    return min(prev)


def shortest_of_chain(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    ordered = sorted(candidates, key=len)
    shortest = ordered[0].lower()
    return ordered[0] if all(s.lower().startswith(shortest) for s in ordered) else None


def normalize_stat_fragment(text: str) -> str:
    t = re.sub(r"[^a-z]+\Z", "", text.lower(), flags=A)
    return re.sub(r"\s+", " ", t, flags=A).strip()


def complete_truncated_stat_name(fragment: str, allow_slip: bool = True) -> str | None:
    frag = normalize_stat_fragment(fragment)
    if len(frag) < 5:
        return None
    whole = next((s for s, lc in _KNOWN_LC if lc == frag), None)
    if whole:
        return whole
    # A lone word that ends other stats ("Chance") lost its head, not its tail.
    if " " not in frag and any(lc.endswith(" " + frag) for _s, lc in _KNOWN_LC):
        return None
    exact = [s for s, lc in _KNOWN_LC if len(lc) > len(frag) and lc.startswith(frag)]
    if exact:
        return shortest_of_chain(exact)
    if not allow_slip or len(frag) < 8:
        return None
    return shortest_of_chain([s for s, lc in _KNOWN_LC if prefix_distance(frag, lc) <= 1])


FACTION_TAIL = re.compile(r"\b(grineer|corpus|infested)\W*$", AI)


def faction_multiplier_stat(line: str, name_part: str) -> str | None:
    m = FACTION_TAIL.search(name_part)
    ext = extract_sign_and_value(line)
    if not m or not (ext and ext.get("multiplier")):
        return None
    word = m.group(1).lower()
    return f"Damage to {word[0].upper()}{word[1:]}"


WRAP_HEAD_NAMES = {"Magazine", "Heavy Attack"}

LONGER_STATS_BY_TAIL = {
    tail.lower(): [s for s in KNOWN_RIVEN_STATS if s.lower().endswith(" " + tail.lower())]
    for tail in KNOWN_RIVEN_STATS
}

_UNSET = object()


def resolve_garbled_head(line: str, hit: dict, prefix_start: int):
    """Longer stat (dict), None when the head names none, or _UNSET when there is no head word."""
    longer = LONGER_STATS_BY_TAIL.get(hit["stat"].lower(), [])
    if not longer:
        return _UNSET
    head = re.search(r"([A-Za-z]{3,})[^A-Za-z\d]*$", line[prefix_start:hit["idx"]], A)
    if not head:
        return _UNSET
    frag = head.group(1).lower()
    n_tail = len(hit["stat"].split(" "))
    matches = []
    for stat in longer:
        words = stat.lower().split(" ")
        k = len(words) - n_tail - 1
        word = words[k] if 0 <= k < len(words) else ""
        if word.endswith(frag) or (len(frag) >= 4 and levenshtein(frag, word[-len(frag):]) <= 1):
            matches.append(stat)
    if len(matches) != 1:
        return None
    return {"stat": matches[0], "idx": prefix_start + head.start()}


def collapse_orphan_value_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    pending: list[str] = []
    for raw in lines:
        current = raw.strip()
        if not current:
            continue
        ext = extract_sign_and_value(current)
        value_only = (ext is not None and ext["value"] is not None and not line_contains_known_stat(current)
                      and re.fullmatch(r"[+\-–x\d\s.,% ]+", current, AI) is not None
                      and re.fullmatch(r"\d{1,4}", current.strip(), A) is None)
        if value_only:
            pending.append(current)
            continue
        if pending and line_contains_known_stat(current):
            own = extract_sign_and_value(current)
            if own is None or own["value"] is None:
                collapsed.append(f"{pending.pop(0)} {current}".strip())
            else:
                collapsed.append(current)
        else:
            collapsed.append(current)
    collapsed.extend(pending)
    return collapsed


DAMAGE_TYPE_STAT_NAMES = {"electricity", "corrosive", "radiation", "magnetic", "cold", "heat", "toxin", "viral",
                          "blast", "gas", "impact", "puncture", "slash"}
MIN_DROPPED_STAT_NAME_CHARS = 3


def looks_like_whole_stat_line(line: str) -> bool:
    if not looks_stat_like(line):
        return False
    if re.search(r"\bx\d+\s*fo", line, AI) and not re.search(r"[+\-–]\s*\d", line, A):
        return False
    return len(re.findall(r"[A-Za-z]", line)) >= MIN_DROPPED_STAT_NAME_CHARS


def _make_stat(name: str, positive: bool, display_positive: bool, value, multiplier: bool) -> RivenStat:
    out: RivenStat = {"name": name, "positive": positive}
    if display_positive != positive:
        out["displayPositive"] = display_positive
    out["value"] = value
    if multiplier:
        out["multiplier"] = True
    return out


def parse_stats_from_lines(text: str, dropped: list[str] | None = None) -> list[RivenStat]:
    lines = collapse_orphan_value_lines(re.split(r"\r?\n", text))
    results: list[RivenStat] = []
    seen: set[str] = set()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        hits: list[dict] = []
        for stat, lc in _KNOWN_LC:
            idx = lower.find(lc)
            if idx != -1:
                hits.append({"stat": stat, "idx": idx})

        if not hits:
            if re.match(r"[+\-–x\xd7]", line, AI):
                name_part = re.sub(r"^[+\-–x\xd7]?[\d.,\s%]*", "", line, count=1, flags=AI).strip()
                completed = faction_multiplier_stat(line, name_part) or complete_truncated_stat_name(name_part)
                if completed:
                    idx = lower.find(name_part.lower()[:4])
                    if idx >= 0:
                        hits.append({"stat": completed, "idx": idx})
                fuzzy = re.sub(r"^[^A-Za-z]+", "", name_part).lower()
                if not hits and len(fuzzy) >= 3:
                    max_dist = 2 if len(fuzzy) >= 7 else 1
                    best_stat, best_dist, best_idx = "", math.inf, -1
                    np_lower = name_part.lower()
                    for stat, lc in _KNOWN_LC:
                        dist = levenshtein(fuzzy[:len(lc) + 2], lc)
                        if dist < best_dist and dist <= max_dist:
                            best_dist, best_stat = dist, stat
                            best_idx = lower.find(np_lower)
                    if best_stat and best_idx >= 0:
                        hits.append({"stat": best_stat, "idx": best_idx})
            if not hits:
                if dropped is not None and looks_stat_like(line):
                    dropped.append(line)
                continue

        hits.sort(key=lambda h: (h["idx"], -len(h["stat"])))
        filtered: list[dict] = []
        last_end = -1
        for h in hits:
            if h["idx"] >= last_end:
                filtered.append(h)
                last_end = h["idx"] + len(h["stat"])

        # Extend short matches only when the cleaned tail is an unambiguous longer stat.
        for i, h in enumerate(filtered):
            tail_end = filtered[i + 1]["idx"] if i + 1 < len(filtered) else len(line)
            clean_tail = normalize_stat_fragment(line[h["idx"]:tail_end])
            if len(clean_tail) <= len(h["stat"]):
                continue
            extension = re.sub(r"[^a-z]", "", clean_tail[len(h["stat"]):])
            if len(extension) < 2 and h["stat"] not in WRAP_HEAD_NAMES:
                continue
            completed = complete_truncated_stat_name(clean_tail, len(clean_tail) - len(h["stat"]) > 3)
            if completed and completed.lower().startswith(h["stat"].lower()):
                filtered[i] = {"stat": completed, "idx": h["idx"]}

        # A garbled head word naming no longer stat voids the hit, as does any
        # letter between the value and a base Damage.
        for i in range(len(filtered) - 1, -1, -1):
            h = filtered[i]
            prefix_start = filtered[i - 1]["idx"] + len(filtered[i - 1]["stat"]) if i > 0 else 0
            resolved = resolve_garbled_head(line, h, prefix_start)
            if resolved is not _UNSET and resolved is not None:
                filtered[i] = resolved
            elif h["stat"] == "Damage" and (
                    resolved is None or re.search(r"[A-Za-z][^\d%]*\Z", line[prefix_start:h["idx"]], A)):
                filtered.pop(i)
        if not filtered:
            if dropped is not None and looks_stat_like(line):
                dropped.append(line)
            continue

        for i, h in enumerate(filtered):
            stat, idx = h["stat"], h["idx"]
            key = stat.lower()
            prefix_start = filtered[i - 1]["idx"] + len(filtered[i - 1]["stat"]) if i > 0 else 0
            prefix = line[prefix_start:idx]
            ext = extract_sign_and_value(prefix)
            if not ext or ext["value"] is None:
                suffix_end = filtered[i + 1]["idx"] if i + 1 < len(filtered) else len(line)
                sx = extract_sign_and_value(line[idx + len(stat):suffix_end])
                if sx and sx["value"] is not None:
                    ext = sx
            positive = ext["positive"] if ext else True
            value = ext["value"] if ext else None
            effective = positive
            display_positive = positive
            multiplier = bool(ext.get("multiplier")) if ext else False

            # Multipliers belong only to faction damage; elsewhere they are qualifier junk.
            if multiplier and not re.match(r"Damage\b", stat, A):
                continue

            if key in seen:
                existing_idx = -1 if value is None else next(
                    (k for k, r in enumerate(results) if r["name"].lower() == key), -1)
                existing_value = results[existing_idx]["value"] if existing_idx >= 0 else _UNSET
                if value is not None and existing_value is None:
                    results.pop(existing_idx)
                else:
                    if (value is not None and existing_value is not _UNSET and existing_value is not None
                            and _is_integer(existing_value) and not _is_integer(value)
                            and math.floor(value) == existing_value):
                        results[existing_idx] = _make_stat(stat, effective, display_positive, value, multiplier)
                    continue
            seen.add(key)

            noisy = re.search(r"[+\-–]\s*\S", prefix, A) is not None
            if value is None and i > 0 and key in DAMAGE_TYPE_STAT_NAMES and not noisy:
                prev = results[-1] if results else None
                if (prev and prev["value"] is not None and not prev.get("multiplier")
                        and prev["name"].lower() in DAMAGE_TYPE_STAT_NAMES):
                    value = prev["value"]
                    effective = prev["positive"]

            if key in INVERTED_POLARITY_STATS:
                effective = not effective
            results.append(_make_stat(stat, effective, display_positive, value, multiplier))
    return results


def _count_exact_value_matches(scanned: list[RivenStat], known: list[RivenStat]) -> int:
    by_name = {}
    for s in known:
        by_name[s["name"].lower()] = s  # Map keeps the last duplicate
    count = 0
    for s in scanned:
        m = by_name.get(s["name"].lower())
        if not m or s["positive"] != m["positive"]:
            continue
        if s.get("value") is None or m.get("value") is None:
            continue
        tol = 0.005 if (s.get("multiplier") or m.get("multiplier")) else 0.05
        if abs(s["value"] - m["value"]) <= tol:
            count += 1
    return count


def looks_like_stale_card_read(scanned: list[RivenStat], known_cards: list[list[RivenStat]]) -> bool:
    """A reroll never repeats exact values: two value-exact matches mean the old card was read."""
    if len(scanned) < 2:
        return False
    return any(card and _count_exact_value_matches(scanned, card) >= 2 for card in known_cards)


def roll_rescan_reason(scanned: list[RivenStat], known_cards: list[list[RivenStat]]) -> str | None:
    if not scanned:
        return "read nothing"
    if looks_like_stale_card_read(scanned, known_cards):
        return "matches a pre-roll card"
    return None


def score_stat_similarity(left: list[RivenStat], right: list[RivenStat]) -> float:
    """scoreRivenStatSimilarity (rivenOverlayIpc.ts): which side did the choice keep?"""
    if not left or not right:
        return 0
    by_name = {s["name"].lower(): s for s in right}
    score = 0.0
    for s in left:
        m = by_name.get(s["name"].lower())
        if not m:
            score -= 4
            continue
        score += 12 if s["positive"] == m["positive"] else 2
        if s.get("value") is not None and m.get("value") is not None:
            base = max(5, abs(s["value"]), abs(m["value"]))
            score += max(0, 8 - (abs(s["value"] - m["value"]) / base) * 24)
        elif s.get("value") == m.get("value"):
            score += 2
    score -= max(0, len(right) - len(left)) * 3
    return score


def format_stat(stat: RivenStat) -> str:
    """"+159 Multishot", "x0.84 Damage to Corpus" (log / toast text)."""
    shown = stat.get("displayPositive", stat["positive"])
    v = stat.get("value")
    vs = "?" if v is None else _js_num_str(v)
    if stat.get("multiplier") and v is not None:
        return f"x{vs} {stat['name']}"
    return f"{'+' if shown else '-'}{vs} {stat['name']}"


# ---- Appendix B: OCR post-processing -------------------------------------------------

def postprocess_ocr_text(text: str) -> str:
    """Deterministic corrections for known PaddleOCR CH misreads (one recognized line)."""
    text = _fold_spaces(text)
    text = text.replace("*-", "-")
    text = _sub(r">([A-Z])", r"\g<1>", text)
    text = _sub(r"%([A-Z])", lambda m: m.group(0)[0] + " " + m.group(0)[1], text)
    text = _sub(r"([a-z])([A-Z])", r"\g<1> \g<2>", text)
    text = text.replace("Damageto", "Damage to")
    text = _sub(r"\bfor([A-Z])", r"for \g<1>", text)
    text = _sub(r"(\d)for\b", r"\g<1> for", text)
    text = _sub(r"(?:^|\n)(\*?)[A-Za-z]?(\d[,.]?\d*)\s*Damage\s+to\b",
                lambda m: f"x{m.group(2)} Damage to", text, AM)
    text = text.replace("Mmpact", "Impact")
    text = _sub(r"%\s*mpact", "% Impact", text)
    text = _sub(r"([+-])i(\d)", r"\g<1>1\g<2>", text)
    text = _sub(r"\bx[il]([,.])", r"x1\g<1>", text)
    text = _sub(r"(\d)i([,.])", r"\g<1>1\g<2>", text)
    text = _sub(r"(\d)\.,(\d)", r"\g<1>,\g<2>", text)
    text = _sub(r"(\d)\.\.(\d)", r"\g<1>.\g<2>", text)
    text = _sub(r"(\d) (\d)", r"\g<1>\g<2>", text)
    text = _sub(r"\bAditional\b", "Additional", text)
    return text


SPLIT_STAT_TAILS = {
    "slide attack": "Critical Chance for Slide Attack",
    "for slide attack": "Critical Chance for Slide Attack",
    "for slide": "Critical Chance for Slide Attack",
    "count chance": "Additional Combo Count Chance",
    "combo count chance": "Additional Combo Count Chance",
    "combo count": "Chance to Gain Combo Count",
    "gain combo count": "Chance to Gain Combo Count",
    "damage": "Finisher Damage",
    "efficiency": "Heavy Attack Efficiency",
    "attack efficiency": "Heavy Attack Efficiency",
    "capacity": "Magazine Capacity",
    "heavy attacks": "Critical Chance",
    "for heavy attacks": "Critical Chance",
    "x2 for heavy attacks": "Critical Chance",
    "for bows": "Fire Rate",
    "x2 for bows": "Fire Rate",
    "duration": "Status Duration",
    "speed": "Reload Speed",
    "maximum": "Ammo Maximum",
    "recoil": "Weapon Recoil",
    "chance": "Status Chance",
}

SPLIT_STAT_HEADS = [
    "critical chance", "critical chance for", "additional combo", "additional combo count", "aditional combo",
    "aditional combo count", "chance to gain", "chance to gain combo", "finisher", "melee", "heavy attack", "heavy",
    "magazine", "fire rate", "fire rate x2", "fire rate x2 for", "critical chance x2", "critical chance x2 for",
    "status", "reload", "ammo", "weapon",
]
_HEADS_SET = set(SPLIT_STAT_HEADS)

GLYPH_JUNK_LINE = re.compile(r"^[^A-Za-z0-9%+\-–×]*$")


def _norm_for_merge(s: str) -> str:
    n = s.lower().strip()
    n = re.sub(r"[()]", "", n)
    return re.sub(r"\s+", " ", n, flags=A)


def merge_split_lines(lines: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Merge consecutive fragments of a known multi-word stat; (text, confidence) pairs."""
    merged: list[tuple[str, float]] = []
    i = 0
    while i < len(lines):
        text, conf = lines[i]
        norm = _norm_for_merge(text)
        stat_part = re.sub(r"^[+\-x]?[\d.,]+%?\s*", "", norm, count=1, flags=A).strip()
        stat_part = re.sub(r"[^a-z0-9 ]", "", stat_part).strip()
        head = stat_part in _HEADS_SET or (len(stat_part) >= 4 and any(h.startswith(stat_part) for h in SPLIT_STAT_HEADS))
        nxt = i + 1
        while head and nxt < len(lines) and GLYPH_JUNK_LINE.match(lines[nxt][0]):
            nxt += 1
        if nxt < len(lines) and head:
            tail_text, tail_conf = lines[nxt]
            clean = re.sub(r"[^a-z0-9 ]", "", _norm_for_merge(tail_text)).strip()
            if clean in SPLIT_STAT_TAILS:
                merged.append((text.rstrip() + " " + tail_text.lstrip(), min(conf, tail_conf)))
                i = nxt + 1
                continue
        merged.append((text, conf))
        i += 1
    return merged


STAT_LINE_RE = re.compile(r"^[+\-x×]", re.I)
LOW_CONF_LINE_RE = re.compile(r"^[+-]|^[x×]\s*\d", re.I)
LOW_CONFIDENCE_THRESHOLD = 0.8


def min_confidence(lines: list[tuple[str, float]]) -> float:
    stat = [c for t, c in lines if STAT_LINE_RE.match(t.strip())]
    if stat:
        return min(stat)
    return min((c for _t, c in lines), default=-1)


def has_low_confidence_line(lines: list[tuple[str, float]]) -> bool:
    stat = [c for t, c in lines if LOW_CONF_LINE_RE.match(t.strip())]
    return any(c < LOW_CONFIDENCE_THRESHOLD for c in stat)
