"""Rivens page view-models (owned rivens from the inventory, graded; WFM look-ups).

RPCs
  rivens.owned(sort, weapon)  unveiled rivens decoded from inventory UpgradeFingerprints (stats with
                              current / rank-8 values, roll floats, grades, attribute grade, name),
                              veiled rivens with their challenge, unseen veiled counts per type
  rivens.similar(id, auctions) warframe.market listings for an owned riven's weapon family, ranked
                              by stat similarity to that riven (network; [] on failure)
  rivens.sheet()              good-roll sheet status + best attributes for a weapon (no network)
Event rivens.updated: inventory changed, or the good-roll sheet finished loading in the background
(rivens.owned starts that load when the sheet is missing or older than 7 days).
The live scanner session (riven.state / riven.rescan / riven.close ...) lives in
features/riven_scanner.py.
"""

from __future__ import annotations

import threading
import time

from .. import log, rpc
from ..app import App, get_app
from ..features import riven_data as rd
from ..features import riven_grading as rg
from ..features import riven_market as rm

_log = log.get("menus.rivens")

SORTS = ("grade", "weapon", "rerolls", "perfect")

_cache_lock = threading.Lock()
_cache: dict = {"key": None, "decoded": None}


def _inventory():
    from .. import inventory as inv_mod

    return inv_mod.inventory()


def _decoded(inv) -> dict:
    """decode_all_rivens, cached per inventory snapshot and sheet/data version."""
    gr = rg.goodrolls()
    key = (id(inv), getattr(inv, "fetched_at", None), rd.db().version, gr.updated_at)
    with _cache_lock:
        if _cache["key"] == key and _cache["decoded"] is not None:
            return _cache["decoded"]
    decoded = rg.decode_all_rivens(inv.data or {}) if inv is not None else {"unveiled": [], "veiled": [],
                                                                            "veiledUnseen": []}
    with _cache_lock:
        _cache["key"], _cache["decoded"] = key, decoded
    return decoded


_GRADE_ORDER = ["S", "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "F", "?"]
_ATTR_ORDER = ["Great", "Good", "OK", "Bad", "?"]


def _sort_key(sort: str):
    if sort == "weapon":
        return lambda r: (str(r.get("weaponName") or "").lower(), str(r.get("rivenName") or ""))
    if sort == "rerolls":
        return lambda r: (-(r.get("rerolls") or 0), str(r.get("weaponName") or ""))
    if sort == "perfect":
        return lambda r: (-(r.get("statPerfectness") or 0), str(r.get("weaponName") or ""))
    return lambda r: (_ATTR_ORDER.index(r.get("attributeGrade")) if r.get("attributeGrade") in _ATTR_ORDER else 9,
                      _GRADE_ORDER.index(r.get("overallGrade")) if r.get("overallGrade") in _GRADE_ORDER else 99,
                      -(r.get("statPerfectness") or 0))


def build_owned(sort: str = "grade", weapon: str = "") -> dict:
    inv = _inventory()
    d = _decoded(inv)
    rows = list(d.get("unveiled") or [])
    q = str(weapon or "").lower().strip()
    if q:
        rows = [r for r in rows if q in str(r.get("weaponName") or "").lower() or q in str(r.get("rivenName") or "").lower()]
    rows.sort(key=_sort_key(sort if sort in SORTS else "grade"))
    return {
        "inventory": None if inv is None else {"origin": inv.origin, "syncTime": inv.sync_time,
                                               "fetchedAt": int(inv.fetched_at * 1000)},
        "data": rd.status(),
        "sheet": rg.goodrolls().status(),
        "total": len(d.get("unveiled") or []),
        "rivens": rows,
        "veiled": d.get("veiled") or [],
        "veiledUnseen": d.get("veiledUnseen") or [],
    }


def find_owned(riven_id: str) -> dict | None:
    inv = _inventory()
    for r in _decoded(inv).get("unveiled") or []:
        if str(r.get("itemId")) == str(riven_id):
            return r
    return None


def build_similar(riven_id: str, auctions: bool | None = None, search: rm.SimilarSearch | None = None) -> dict:
    r = find_owned(riven_id)
    if r is None:
        raise ValueError(f"no owned riven {riven_id}")
    slug = r.get("familySlug") or rd.db().family_slug(r.get("weaponName") or "")
    shown = True if auctions is None else bool(auctions)
    if not slug:
        return {"slug": None, "rows": [], "auctionsShown": shown, "error": "unknown weapon"}
    s = search or rm.search()
    pool = rm.similar_pool(s.search(slug), rm.SIMILAR_LISTING_COUNT)
    stats = [{"name": st.get("name"), "positive": st.get("positive")} for st in r.get("stats") or []]
    rows = rm.rank_similar(stats, pool, shown)
    for row in rows:
        row["url"] = rm.auction_url(row["id"])
    return {"slug": slug, "rows": rows, "auctionsShown": shown, "error": s.last_error, "at": int(time.time() * 1000)}


_sheet_lock = threading.Lock()
_sheet_busy = False


def _sheet_needs_load(gr) -> bool:
    gr.load_cache_if_needed()
    return not gr.has_sheet() or gr.stale()


def refresh_sheet_in_background(app: App) -> bool:
    """Fetch the good-roll sheet off-thread when missing/stale; emit rivens.updated if it changed.

    The decoded cache is keyed on the sheet's updated_at, so the page's reload regrades.
    Returns whether a refresh was started."""
    global _sheet_busy
    gr = rg.goodrolls()
    if not _sheet_needs_load(gr):
        return False
    with _sheet_lock:
        if _sheet_busy:
            return False
        _sheet_busy = True
    before = gr.updated_at

    def run() -> None:
        global _sheet_busy
        try:
            gr.ensure_loaded(wait=True)
        finally:
            with _sheet_lock:
                _sheet_busy = False
        if gr.updated_at != before:
            with _cache_lock:
                _cache["key"] = None
            app.emit("rivens.updated", {"at": int(time.time() * 1000), "reason": "sheet"})

    app.submit(run)
    return True


def setup(app: App) -> None:
    from .. import inventory as inv_mod

    def changed(_inv) -> None:
        with _cache_lock:
            _cache["key"] = None
        app.emit("rivens.updated", {"at": int(time.time() * 1000)})

    inv_mod.on_update(changed)


@rpc.method("rivens.owned")
async def _rpc_owned(sort: str = "grade", weapon: str = "") -> dict:
    app = get_app()
    out = await app.run_blocking(build_owned, sort, weapon)
    # Attribute grades need the sheet; without it every row is "?" until a scan fetched it.
    if out.get("rivens"):
        await app.run_blocking(refresh_sheet_in_background, app)
    return out


@rpc.method("rivens.similar")
async def _rpc_similar(id: str, auctions: bool | None = None) -> dict:  # noqa: A002 - RPC param name
    return await get_app().run_blocking(build_similar, id, auctions)


@rpc.method("rivens.sheet")
def _rpc_sheet(weapon: str = "") -> dict:
    g = rg.goodrolls()
    best = None
    if weapon:
        db = rd.db()
        best = g.best_attributes(weapon, db.is_melee(weapon), db)
    return {**g.status(), "best": best}
