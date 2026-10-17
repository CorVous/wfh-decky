"""OCR reward-name matching, ported from WFHelper services/rewardScannerMatch.ts.

The reward scanner reads one title strip per card and must turn noisy OCR
("Rhino Prlme BlueDrint", "2 XForma Blueprint", "Caliban Prime81ueprint") into
one name from the relic reward pool. The scoring bands, thresholds and the
alias table are copied verbatim so behaviour (and WFHelper's tests) carry over:

    exact (1000/990) > substring (400 + conf*92 + len bonus) > fuzzy (conf*100 + 2*words)

Pool entries may be any object with a ``name`` attribute or a dict with a
"name" key; the matcher hands the same object back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

# Verbatim from REWARD_TOKEN_ALIASES (rewardScannerMatch.ts:74-147). Keys with
# capitals ("bIueprint") can never match after lowercasing; kept for parity.
REWARD_TOKEN_ALIASES: dict[str, str] = {
    # Blueprint variants
    "bluedrint": "blueprint", "blueorint": "blueprint", "blueprlnt": "blueprint",
    "blueprini": "blueprint", "bluepnnt": "blueprint", "blueprint": "blueprint",
    "bIueprint": "blueprint", "biueprint": "blueprint", "lueprint": "blueprint",
    # Systems variants
    "svst": "systems", "svstems": "systems", "systerns": "systems", "syst": "systems",
    "syslems": "systems",
    # Neuroptics variants
    "neurootics": "neuroptics", "neurotics": "neuroptics", "neuroptlcs": "neuroptics",
    "neurcptics": "neuroptics", "neuropiics": "neuroptics", "neuraptics": "neuroptics",
    "eurobtic": "neuroptics", "europtics": "neuroptics",
    # Chassis variants
    "chassls": "chassis", "chassi": "chassis", "chassl": "chassis", "chassi5": "chassis",
    "hassis": "chassis",
    # Receiver variants
    "recelver": "receiver", "recelvar": "receiver", "recei": "receiver", "rece1ver": "receiver",
    # Common Warframe name misreads
    "wukon": "wukong", "rhln": "rhino", "rhin": "rhino", "sarv": "saryn", "nek": "nekros",
    "nekr": "nekros", "obero": "oberon", "obenon": "oberon", "trinlty": "trinity",
    "trini": "trinity", "trinit": "trinity", "bans": "banshee", "bansh": "banshee",
    "equlnox": "equinox", "equln": "equinox", "voIt": "volt", "hy": "hydroid",
    "hyd": "hydroid", "ivar": "ivara", "llmbo": "limbo", "Iimbo": "limbo",
    # Weapon / part name misreads
    "prlme": "prime", "pnme": "prime", "prix": "prime", "priime": "prime", "barre": "barrel",
    "banel": "barrel", "bIade": "blade", "bilade": "blade", "stoc": "stock", "slock": "stock",
    "grlp": "grip", "forrna": "forma", "forna": "forma",
}

# Thresholds, verbatim.
UNIQUE_STRUCTURAL_CONFIDENCE = 0.93
MIN_FUZZY_NAME_LENGTH_RATIO = 0.45
MIN_FUZZY_WORD_RATIO = 0.45
MIN_SUBSEQUENCE_WORD_RATIO = 0.6
MIN_MEANINGFUL_READ_WORD_LENGTH = 3
SUBSTRING_SCORE_FLOOR = 0.88
FOLDED_EXACT_CONFIDENCE = 0.99
FOLDED_NEAR_MIN_SIMILARITY = 0.94
FOLDED_NEAR_MIN_LENGTH = 12
# The reward scanner accepts a slot read at or above this (relic-rewards.md §7).
SLOT_ACCEPT_CONFIDENCE = 0.86

_QUANTITY_PREFIX = re.compile(r"^\d+\s*x\s+", re.IGNORECASE | re.ASCII)
_QUANTITY_PREFIX_SPACING = re.compile(r"^(\d+)\s+x(?=\s|$)", re.ASCII)
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_OCR_CONFUSIONS = str.maketrans({"8": "b", "0": "o", "1": "l", "5": "s"})


@dataclass
class Candidate:
    item: Any | None
    confidence: float
    score: float
    mode: str  # exact | substring | fuzzy | none

    @property
    def name(self) -> str | None:
        return item_name(self.item) if self.item is not None else None

    def to_json(self) -> dict:
        return {"name": self.name, "confidence": round(self.confidence, 3),
                "score": round(self.score, 3), "mode": self.mode}


def item_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "")
    if isinstance(item, str):
        return item
    return str(getattr(item, "name", "") or "")


# ---- text helpers -------------------------------------------------------

def normalize_for_search(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def normalize_for_slug(value: str | None) -> str | None:
    """WFHelper normalizeForSlug: "Medi-Ray" -> "medi_ray"."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    s = re.sub(r"['’‘]", "", s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or None


# WFM slug regex approximated to the characters warframe.market actually mints.
_WFM_SLUG_RE = re.compile(r"^[a-z0-9à-ÿ](?:[a-z0-9à-ÿ_()'’-]{0,118}[a-z0-9à-ÿ)])?$")


def sanitize_wfm_slug(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    slug = value.strip().lower()
    return slug if _WFM_SLUG_RE.match(slug) else None


def normalize_wfm_slug(value: Any) -> str | None:
    """Keep a slug WFM minted, else fold the name into one (config/shared/wfm.ts:96-98)."""
    return sanitize_wfm_slug(value) or normalize_for_slug(value if isinstance(value, str) else None)


def has_quantity_prefix(value: str) -> bool:
    return bool(_QUANTITY_PREFIX.match(value))


def strip_quantity_prefix(value: str) -> str:
    return _QUANTITY_PREFIX.sub("", value, count=1)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return max(0.0, 1 - levenshtein(a, b) / max(len(a), len(b), 1))


def normalize_reward_word(word: str) -> str:
    w = _NON_ALNUM.sub("", normalize_for_search(word))
    if not w:
        return ""
    return REWARD_TOKEN_ALIASES.get(w, w)


def normalize_reward_text(text: str) -> str:
    """Per-word clean-up plus "2 x forma" -> "2x forma" (the card prints "2 X")."""
    words = [normalize_reward_word(w) for w in str(text or "").split()]
    joined = " ".join(w for w in words if w).strip()
    return _QUANTITY_PREFIX_SPACING.sub(r"\1x", joined, count=1)


def fold_ocr_confusions(value: str) -> str:
    """8->b, 0->o, 1->l, 5->s and drop spacing, for merged-glyph reads ("Prime81ueprint")."""
    return _NON_ALNUM.sub("", str(value or "").lower().translate(_OCR_CONFUSIONS))


@lru_cache(maxsize=8192)
def _normalized_name(name: str) -> str:
    return normalize_reward_text(name)


@lru_cache(maxsize=8192)
def _folded_name(name: str) -> str:
    return fold_ocr_confusions(_normalized_name(name))


def _phrase_pos(text: str, phrase: str) -> int:
    if not text or not phrase:
        return -1
    m = re.search(r"(^|[^a-z0-9])" + re.escape(phrase) + r"(?=$|[^a-z0-9])", text)
    if not m:
        return -1
    return m.start() + len(m.group(1))


def _contains_phrase(text: str, phrase: str) -> bool:
    return _phrase_pos(text, phrase) >= 0


def _useful_partial(text: str) -> bool:
    return len([w for w in text.split(" ") if len(w) > 1]) >= 2 or len(text) >= 5


def _words_equivalent(text_word: str, name_word: str) -> bool:
    if text_word == name_word:
        return True
    return similarity(text_word, name_word) >= (0.7 if len(name_word) >= 7 else 0.78)


def _ordered_subsequence(text_words: list[str], name_words: list[str]) -> bool:
    i = 0
    for word in text_words:
        while i < len(name_words) and not _words_equivalent(word, name_words[i]):
            i += 1
        if i >= len(name_words):
            return False
        i += 1
    return True


def _substring_score(conf: float, name_len: int) -> float:
    return 400 + conf * 92 + min(8, name_len / 4)


# ---- ranking ------------------------------------------------------------

def _drop_quantity_mismatches(ranked: list[Candidate], text: str) -> list[Candidate]:
    """"2X Forma Blueprint" vs "Forma Blueprint": the read's own count decides."""
    info: dict[int, tuple[str, bool]] = {}
    bare_seen: set[str] = set()
    counted_seen: set[str] = set()
    for idx, c in enumerate(ranked):
        if c.item is None:
            continue
        name = _normalized_name(item_name(c.item))
        bare = strip_quantity_prefix(name)
        if not bare:
            continue
        counted = bare != name
        info[idx] = (bare, counted)
        (counted_seen if counted else bare_seen).add(bare)
    if not counted_seen:
        return ranked
    text_qty = has_quantity_prefix(text)
    out = []
    for idx, c in enumerate(ranked):
        meta = info.get(idx)
        if meta is None or meta[0] not in bare_seen or meta[0] not in counted_seen:
            out.append(c)
        elif c.mode == "exact" or text_qty == meta[1]:
            out.append(c)
    return out


def _boost_unique_structural(ranked: list[Candidate], text: str, text_words: list[str]) -> None:
    """Lift a partial read to 0.93 only when exactly one name explains it."""
    if len(text_words) < 2 or any(c.mode == "exact" for c in ranked):
        return
    prefix_hits: dict[str, Candidate] = {}
    contains_hits: dict[str, Candidate] = {}
    subseq_hits: dict[str, Candidate] = {}
    thin: set[str] = set()
    for c in ranked:
        if c.item is None:
            continue
        name = _normalized_name(item_name(c.item))
        if c.mode == "substring" and name.startswith(text + " "):
            prefix_hits[name] = c
        elif c.mode == "substring" and _contains_phrase(text, name):
            contains_hits[name] = c
        else:
            name_words = [w for w in name.split(" ") if len(w) > 1]
            if len(name_words) <= len(text_words):
                continue
            if not _ordered_subsequence(text_words, name_words):
                continue
            subseq_hits[name] = c
            if len(text_words) / len(name_words) < MIN_SUBSEQUENCE_WORD_RATIO:
                thin.add(name)
    tier = next((t for t in (prefix_hits, contains_hits, subseq_hits) if t), None)
    if not tier or len(tier) != 1:
        return
    name, target = next(iter(tier.items()))
    if target.item is None or name in thin or target.confidence >= UNIQUE_STRUCTURAL_CONFIDENCE:
        return
    target.confidence = UNIQUE_STRUCTURAL_CONFIDENCE
    target.score = _substring_score(target.confidence, len(name))


def rank_candidates(ocr_text: str, items: Iterable[Any], limit: int = 5) -> list[Candidate]:
    """rankRewardCandidatesDetailed: best candidates for one OCR'd title, best first."""
    text = normalize_reward_text(ocr_text)
    if not text:
        return [Candidate(None, 0.0, 0.0, "none")]
    text_words = [w for w in text.split(" ") if len(w) > 1]
    folded_text = fold_ocr_confusions(text)
    text_qty = has_quantity_prefix(text)
    ranked: list[Candidate] = []

    for item in items:
        raw_name = item_name(item)
        name = _normalized_name(raw_name)
        if not name:
            continue
        if text == name:
            ranked.append(Candidate(item, 1.0, 1000.0, "exact"))
            continue
        folded_name = _folded_name(raw_name)
        if folded_text and folded_text == folded_name:
            ranked.append(Candidate(item, FOLDED_EXACT_CONFIDENCE, 990.0, "exact"))
            continue
        if _contains_phrase(text, name) or (_useful_partial(text) and _contains_phrase(name, text)):
            conf = max(SUBSTRING_SCORE_FLOOR, similarity(text, name))
            ranked.append(Candidate(item, conf, _substring_score(conf, len(name)), "substring"))
            continue
        if (not text_qty and not has_quantity_prefix(name)
                and len(folded_text) >= FOLDED_NEAR_MIN_LENGTH and len(folded_name) >= FOLDED_NEAR_MIN_LENGTH):
            folded = similarity(folded_text, folded_name)
            if folded >= FOLDED_NEAR_MIN_SIMILARITY:
                ranked.append(Candidate(item, folded, _substring_score(folded, len(name)), "substring"))
                continue
        if len(name) < len(text) * MIN_FUZZY_NAME_LENGTH_RATIO:
            continue
        item_words = [w for w in name.split(" ") if len(w) > 1]
        if not item_words:
            continue
        matched = 0
        explained = [False] * len(text_words)
        for iw in item_words:
            hit = False
            for idx, tw in enumerate(text_words):
                if _words_equivalent(tw, iw):
                    hit = True
                    explained[idx] = True
            if hit:
                matched += 1
        word_ratio = matched / len(item_words)
        if word_ratio < MIN_FUZZY_WORD_RATIO:
            continue
        n_explained = sum(explained)
        unexplained = sum(1 for idx, w in enumerate(text_words)
                          if not explained[idx] and len(w) >= MIN_MEANINGFUL_READ_WORD_LENGTH)
        coverage = min(word_ratio, n_explained / (n_explained + unexplained))
        span = similarity(text, name)
        if len(text_words) >= len(item_words):
            for start in range(len(text_words) - len(item_words) + 1):
                span = max(span, similarity(" ".join(text_words[start:start + len(item_words)]), name))
        conf = min(0.97, coverage * 0.6 + span * 0.4)
        ranked.append(Candidate(item, conf, conf * 100 + matched * 2, "fuzzy"))

    survivors = _drop_quantity_mismatches(ranked, text)
    _boost_unique_structural(survivors, text, text_words)
    survivors.sort(key=lambda c: (-c.score, -c.confidence, -len(item_name(c.item)) if c.item is not None else 0))
    return survivors[:max(1, limit)]


def match_single(ocr_text: str, items: Iterable[Any]) -> Candidate:
    """matchSingleRewardTextDetailed: the single best candidate (mode "none" when empty)."""
    ranked = rank_candidates(ocr_text, items, 1)
    return ranked[0] if ranked else Candidate(None, 0.0, 0.0, "none")


def sort_pool(items: Sequence[Any]) -> list[Any]:
    """The scanner pre-sorts the pool longest name first (rewardScanner.ts:39-43)."""
    return sorted(items, key=lambda i: -len(item_name(i)))
