"""Read-only scans of Warframe's memory through /proc/<pid>/maps + /proc/<pid>/mem.

Ports WFHelper services/gameMemoryAuthz.ts (the `?accountId=...&nonce=...`
query the game uses for inventory.php) and services/gameMemoryInventory.ts (the
game's own resident inventory JSON, anchored on `"LastInventorySync"`).

The worker runs as root, which has CAP_SYS_PTRACE, so Yama does not block the
read. Only private writable mappings are read: the auth string and the
inventory are built at runtime, and touching /dev/ or special mappings faults
drivers and stalls the game. One walk can feed several visitors, so the auth
scan and the resident-inventory scan share a single pass over the heap.
"""

from __future__ import annotations

import errno
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Protocol

from .. import log
from .payload import has_inventory_shape

_log = log.get("inventory.memory")

CHUNK = 16 * 1024 * 1024
# An auth string is <70 bytes; overlap chunks so a match can't split across them.
OVERLAP = 256
MAX_REGION_BYTES = 4 * 1024 * 1024 * 1024  # reserve guard, never a real heap
_MAX_OFFSET = (1 << 63) - 1  # pread's off_t
_MAPS_LINE = re.compile(r"^([0-9a-f]+)-([0-9a-f]+) (\S{4}) \S+ \S+ +\S+(?:\s+(.*))?$")


@dataclass
class Region:
    start: int
    end: int


# ---- /proc/<pid>/maps ---------------------------------------------------------

def region_from_maps_line(line: str, widen: bool = False) -> Region | None:
    """A scannable region: private, readable and writable, anonymous (or, when
    widened, private file-backed), never a device or a kernel special mapping."""
    m = _MAPS_LINE.match(line)
    if not m:
        return None
    perms = m.group(3)
    if perms[0] != "r" or perms[1] != "w" or perms[3] != "p":
        return None
    path = (m.group(4) or "").strip()
    if path.startswith("/dev/"):
        return None
    if path.startswith("[vvar") or path in ("[vdso]", "[vsyscall]"):
        return None
    if not widen and path != "":
        return None
    start, end = int(m.group(1), 16), int(m.group(2), 16)
    if end <= start or end > _MAX_OFFSET:
        return None
    if end - start > MAX_REGION_BYTES:
        return None
    return Region(start, end)


def scannable_regions(lines: Iterable[str], widen: bool) -> Iterator[Region]:
    for line in lines:
        r = region_from_maps_line(line, widen)
        if r:
            yield r


def span_around(ascending: Iterable[Region], address: int, limit: int) -> Region | None:
    """Back-to-back regions joined around address, clipped to +-limit."""
    run: Region | None = None
    for region in ascending:
        if run is not None and region.start == run.end:
            run.end = region.end
        else:
            if run is not None and run.end > address:
                break
            if region.start > address:
                return None
            run = Region(region.start, region.end)
        if run.end >= address + limit:
            break
    if run is None or address < run.start or address >= run.end:
        return None
    return Region(max(run.start, address - limit), min(run.end, address + limit))


# ---- the walk -------------------------------------------------------------------

class MemoryReader:
    """Random access to the process image; failures read as zero bytes."""

    def __init__(self, fd: int, maps: list[str]):
        self._fd = fd
        self._maps = maps
        self.widened = False

    def read(self, address: int, size: int) -> bytes:
        try:
            return os.pread(self._fd, size, address)
        except (OSError, OverflowError, ValueError):
            return b""

    def readable_span(self, address: int, limit: int) -> Region | None:
        return span_around(scannable_regions(self._maps, self.widened), address, limit)


class Visitor(Protocol):
    def chunk(self, view: bytes, address: int, region: Region) -> None: ...
    def finish(self, reader: MemoryReader) -> None: ...
    def wants_wider_scan(self) -> bool: ...


@dataclass
class ScanResult:
    # None once the walk ran; else "process-not-found" or "mem-open-<ERRNO>".
    failure: str | None
    regions: int = 0
    bytes: int = 0
    ms: int = 0
    pid: int | None = None


def find_warframe_pid() -> int | None:
    from ..session import find_warframe_pid as by_cmdline

    pid = by_cmdline()
    if pid:
        return pid
    # WFHelper's rule: comm (truncated to 15 chars) still contains "warframe".
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open(f"/proc/{e}/comm") as f:
                if "warframe" in f.read().lower():
                    return int(e)
        except OSError:
            continue
    return None


def scan_process(visitors: list, pid: int | None = None, proc_root: str = "/proc",
                 pause_every: int = 8) -> ScanResult:
    """Walk the game's private writable memory once, feeding every visitor.

    Visitors that find nothing in anonymous memory get a second pass over
    private file-backed mappings (wants_wider_scan), as WFHelper does.
    proc_root/pid are injectable so tests can point at a fake maps/mem pair.
    """
    t0 = time.monotonic()
    if pid is None:
        pid = find_warframe_pid() if proc_root == "/proc" else None
    if not pid:
        return ScanResult("process-not-found")
    base = os.path.join(proc_root, str(pid))
    try:
        fd = os.open(os.path.join(base, "mem"), os.O_RDONLY)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return ScanResult("process-not-found", pid=pid)
        return ScanResult(f"mem-open-{errno.errorcode.get(e.errno, e.errno)}", pid=pid)
    result = ScanResult(None, pid=pid)
    chunk_no = 0
    try:
        try:
            with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as f:
                maps = f.read().split("\n")
        except OSError:
            return ScanResult("process-not-found", pid=pid)
        reader = MemoryReader(fd, maps)

        def scan_pass(targets: list, widen: bool) -> None:
            nonlocal chunk_no
            for region in scannable_regions(maps, widen):
                result.regions += 1
                addr = region.start
                while addr < region.end:
                    length = min(CHUNK, region.end - addr)
                    view = reader.read(addr, length)
                    if view:
                        result.bytes += len(view)
                        for v in targets:
                            v.chunk(view, addr, region)
                        chunk_no += 1
                        if pause_every and chunk_no % pause_every == 0:
                            time.sleep(0.001)  # let the game breathe
                    addr += CHUNK - OVERLAP

        scan_pass(list(visitors), False)
        wider = [v for v in visitors if v.wants_wider_scan()]
        if wider:
            _log.info("no match in anonymous memory - widening to private file-backed regions")
            reader.widened = True
            scan_pass(wider, True)
        for v in visitors:
            v.finish(reader)
    finally:
        os.close(fd)
        result.ms = round((time.monotonic() - t0) * 1000)
    return result


# ---- auth query (gameMemoryAuthz.ts) ----------------------------------------------

ACCOUNT_ID_MARKERS = (b"?accountId=", b"&accountId=")
ACCOUNT_ID_LEN = 24  # DE account ids are 24-hex Mongo ObjectIds
NONCE_SEP = b"&nonce="
MAX_NONCE_DIGITS = 24
_HEX24 = re.compile(rb"^[0-9a-fA-F]{24}$")
AUTHZ_RE = re.compile(r"^\?accountId=[0-9a-f]{24}&nonce=\d{1,24}$")
REJECTIONS = ("truncated", "invalidAccountId", "missingNonce", "emptyNonce", "nonceTooLong")


def new_diagnostics() -> dict[str, int]:
    return {k: 0 for k in REJECTIONS}


def _parse_candidate(view: bytes, at: int, marker_len: int) -> tuple[str | None, str | None]:
    id_start = at + marker_len
    id_end = id_start + ACCOUNT_ID_LEN
    if id_end > len(view):
        return None, "truncated"
    account = view[id_start:id_end]
    if not _HEX24.match(account):
        return None, "invalidAccountId"
    if id_end + len(NONCE_SEP) > len(view):
        return None, "truncated"
    if view[id_end:id_end + len(NONCE_SEP)] != NONCE_SEP:
        return None, "missingNonce"
    p = id_end + len(NONCE_SEP)
    start = p
    n = len(view)
    while p < n and p - start < MAX_NONCE_DIGITS and 0x30 <= view[p] <= 0x39:
        p += 1
    if p == start:
        return None, "truncated" if p == n else "emptyNonce"
    if p < n and 0x30 <= view[p] <= 0x39:
        return None, "nonceTooLong"
    nonce = view[start:p].decode("ascii")
    return f"?accountId={account.decode('ascii').lower()}&nonce={nonce}", None


def _tally(res: tuple[str | None, str | None], diagnostics: dict | None) -> str | None:
    if res[1] and diagnostics is not None:
        diagnostics[res[1]] += 1
    return res[0]


def parse_authz_at(view: bytes, at: int, diagnostics: dict | None = None) -> str | None:
    """The auth string at a marker hit, or None when the offset is not one."""
    for marker in ACCOUNT_ID_MARKERS:
        if view[at:at + len(marker)] == marker:
            return _tally(_parse_candidate(view, at, len(marker)), diagnostics)
    return None


def scan_buffer_for_authz(view: bytes, counts: dict[str, int], diagnostics: dict | None = None) -> int:
    """Tally every valid auth string in view; returns the number of marker hits."""
    hits = 0
    for marker in ACCOUNT_ID_MARKERS:
        idx = view.find(marker)
        while idx != -1:
            hits += 1
            authz = _tally(_parse_candidate(view, idx, len(marker)), diagnostics)
            if authz is not None:
                counts[authz] = counts.get(authz, 0) + 1
            idx = view.find(marker, idx + len(marker))
    return hits


def best_authz(counts: dict[str, int]) -> tuple[str | None, int, bool]:
    """(authz, hits, ambiguous): a unique most-frequent match; tied leaders are unsafe."""
    authz = None
    hits = 0
    ambiguous = False
    for k, v in counts.items():
        if v > hits:
            authz, hits, ambiguous = k, v, False
        elif v == hits and v > 0:
            ambiguous = True
    return (None if ambiguous else authz), hits, ambiguous


class AuthzVisitor:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.diagnostics = new_diagnostics()
        self.marker_hits = 0

    def chunk(self, view: bytes, address: int, region: Region) -> None:
        self.marker_hits += scan_buffer_for_authz(view, self.counts, self.diagnostics)

    def wants_wider_scan(self) -> bool:
        return not self.counts

    def finish(self, reader: MemoryReader) -> None:
        pass

    def result(self, scan: ScanResult) -> tuple[str | None, str]:
        """(authz, reason) with WFHelper's reasons: ok-Nx, process-not-found,
        mem-open-<ERRNO>, crumbs-not-found, crumbs-ambiguous."""
        if scan.failure:
            return None, scan.failure
        scanned = f"{scan.regions} regions, {scan.bytes // (1024 * 1024)} MB"
        if not self.counts:
            # Markers without a usable string: the shape changed. No markers: wrong place.
            _log.warning("no auth crumbs found: markers=%d, scanned=%s, rejects=%s",
                         self.marker_hits, scanned, json.dumps(self.diagnostics))
            return None, "crumbs-not-found"
        authz, hits, ambiguous = best_authz(self.counts)
        if ambiguous:
            _log.warning("multiple auth matches share the highest frequency (%d) - refusing all", hits)
            return None, "crumbs-ambiguous"
        if len(self.counts) > 1:
            _log.warning("multiple distinct auth matches (%d) - using the most frequent", len(self.counts))
        _log.info("auth match found %dx (scanned %s)", hits, scanned)
        return authz, f"ok-{hits}x"


def read_game_authz(pid: int | None = None, proc_root: str = "/proc") -> tuple[str | None, str]:
    v = AuthzVisitor()
    return v.result(scan_process([v], pid, proc_root))


# ---- resident inventory (gameMemoryInventory.ts) -----------------------------------

ANCHOR = b'"LastInventorySync"'
_SYNC_ID_AFTER_ANCHOR = re.compile(r'^\s*:\s*\{\s*"\$oid"\s*:\s*"([0-9a-f]{24})"')
SYNC_ID_WINDOW = 64
ANCHOR_WITH_ID = len(ANCHOR) + SYNC_ID_WINDOW
# A 2026-09 inventory is 0.9 MB with this key 3 KB before its end.
MAX_INVENTORY_JSON_BYTES = 8 * 1024 * 1024
MAX_BYTES_AFTER_ANCHOR = 1024 * 1024
MAX_CANDIDATES = 256
MAX_EXTRACTIONS_PER_PROCESS = 8
MAX_REPORTED_SYNC_TIMES = 10

_QUOTE, _BACKSLASH = 0x22, 0x5C
_OPEN = (0x7B, 0x5B)
_CLOSE = (0x7D, 0x5D)
_WS = (0x20, 0x09, 0x0A, 0x0D)
# Bytes the bracket matchers must look at; everything else is skipped in bulk.
_OUTSIDE_STOP = re.compile(rb'["{}\[\]\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\xff]')
_INSIDE_STOP_FWD = re.compile(rb'["\\\x00-\x1f]')
_INSIDE_STOP_BWD = re.compile(rb'["\x00-\x1f]')


def sync_id_time(sync_id: str) -> int:
    """Seconds in the ObjectId's first four bytes, as epoch milliseconds."""
    return int(sync_id[:8], 16) * 1000


def inventory_sync_id(inventory) -> str | None:
    if not isinstance(inventory, dict):
        return None
    sync = inventory.get("LastInventorySync")
    if not isinstance(sync, dict):
        return None
    oid = sync.get("$oid")
    return oid if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{24}", oid) else None


def _parse_sync_id_at(view: bytes, at: int) -> tuple[str | None, bool]:
    """(sync id, truncated)."""
    frm = at + len(ANCHOR)
    to = min(len(view), frm + SYNC_ID_WINDOW)
    m = _SYNC_ID_AFTER_ANCHOR.match(view[frm:to].decode("latin-1"))
    if m:
        return m.group(1), False
    return None, to - frm < SYNC_ID_WINDOW


def _is_escaped(buf: bytes, quote_at: int) -> bool:
    slashes = 0
    i = quote_at - 1
    while i >= 0 and buf[i] == _BACKSLASH:
        slashes += 1
        i -= 1
    return slashes % 2 == 1


def find_object_start(buf: bytes, key_at: int) -> int | None:
    """Offset of the `{` that opens the object holding the key at key_at,
    walking backwards over JSON; None as soon as the bytes stop being JSON."""
    rev = buf[:key_at][::-1]  # j in rev is i = key_at - 1 - j in buf
    depth = 0
    in_string = False
    j = 0
    n = len(rev)
    while j < n:
        m = (_INSIDE_STOP_BWD if in_string else _OUTSIDE_STOP).search(rev, j)
        if not m:
            return None
        j = m.start()
        byte = rev[j]
        i = key_at - 1 - j
        if in_string:
            if byte == _QUOTE:
                if not _is_escaped(buf, i):
                    in_string = False
            else:  # control character inside a string
                return None
        elif byte == _QUOTE:
            if _is_escaped(buf, i):
                return None
            in_string = True
        elif byte in _CLOSE:
            depth += 1
        elif byte in _OPEN:
            if depth == 0:
                return i if byte == 0x7B else None
            depth -= 1
        else:  # control (non-whitespace) or non-ASCII byte outside a string
            return None
        j += 1
    return None


def find_object_end(buf: bytes, start: int) -> int | None:
    depth = 0
    in_string = False
    i = start
    n = len(buf)
    while i < n:
        m = (_INSIDE_STOP_FWD if in_string else _OUTSIDE_STOP).search(buf, i)
        if not m:
            return None
        i = m.start()
        byte = buf[i]
        if in_string:
            if byte == _BACKSLASH:
                i += 1
            elif byte == _QUOTE:
                in_string = False
            else:
                return None
        elif byte == _QUOTE:
            in_string = True
        elif byte in _OPEN:
            depth += 1
        elif byte in _CLOSE:
            depth -= 1
            if depth == 0:
                return i + 1
        else:
            return None
        i += 1
    return None


def find_inventory_object(buf: bytes, anchor_at: int) -> tuple[int, int] | None:
    start = find_object_start(buf, anchor_at)
    if start is None:
        return None
    end = find_object_end(buf, start)
    if end is None or end <= anchor_at:
        return None
    return start, end


@dataclass
class Candidate:
    address: int
    region: Region
    sync_id: str


@dataclass
class GameInventoryCopy:
    sync_id: str
    sync_time: int  # epoch ms
    inventory: dict
    size: int = 0


def extract_candidate(reader, cand: Candidate) -> GameInventoryCopy | None:
    # A copy measured in 2026-09 spanned several adjacent 64 KB regions.
    readable = reader.readable_span(cand.address, MAX_INVENTORY_JSON_BYTES) or cand.region
    low = max(readable.start, cand.address - MAX_INVENTORY_JSON_BYTES)
    high = min(readable.end, cand.address + MAX_BYTES_AFTER_ANCHOR)
    anchor_at = cand.address - low
    if high - low < anchor_at + len(ANCHOR):
        return None
    view = reader.read(low, high - low)
    if len(view) < anchor_at + len(ANCHOR):
        return None
    if view[anchor_at:anchor_at + len(ANCHOR)] != ANCHOR:
        return None
    span = find_inventory_object(view, anchor_at)
    if not span:
        return None
    try:
        parsed = json.loads(view[span[0]:span[1]].decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not has_inventory_shape(parsed):
        return None
    # The anchor may have been rewritten between the walk and this read.
    if inventory_sync_id(parsed) != cand.sync_id:
        return None
    return GameInventoryCopy(cand.sync_id, sync_id_time(cand.sync_id), parsed, span[1] - span[0])


@dataclass
class InventoryScanStats:
    copies: int = 0
    sync_times: list[int] = field(default_factory=list)
    extractions: int = 0
    failed_extractions: int = 0


class InventoryCollector:
    """Collects `LastInventorySync` anchors during a walk and parses the newest
    complete copy at the end."""

    def __init__(self, defer: bool = False) -> None:
        # defer: finish() keeps the anchors and extract() parses later, so a run
        # that only needs the auth query skips the ~9 MB reads + JSON parse.
        self.defer = defer
        self._candidates: list[Candidate] = []
        self._seen: set[int] = set()
        self._sync_times: set[int] = set()
        self.newest: GameInventoryCopy | None = None
        self.copies = 0
        self.extractions = 0
        self.failed_extractions = 0
        # The previous chunk's tail, joined to the next chunk's head when contiguous.
        self._tail = b""
        self._tail_end = -1
        self._tail_region: Region | None = None

    def _keep_newest(self, nxt: Candidate) -> None:
        if len(self._candidates) < MAX_CANDIDATES:
            self._candidates.append(nxt)
            return
        oldest = min(range(len(self._candidates)), key=lambda i: sync_id_time(self._candidates[i].sync_id))
        if sync_id_time(nxt.sync_id) > sync_id_time(self._candidates[oldest].sync_id):
            self._candidates[oldest] = nxt

    def _collect(self, view: bytes, address: int, region: Region, before: int) -> None:
        idx = view.find(ANCHOR)
        while idx != -1 and idx < before:
            at = idx
            idx = view.find(ANCHOR, idx + len(ANCHOR))
            if address + at in self._seen:
                continue
            sync_id, truncated = _parse_sync_id_at(view, at)
            # A cut-off id is read again from the next chunk's overlap or joint.
            if sync_id is None and truncated:
                continue
            self._seen.add(address + at)
            if sync_id is None:
                continue
            self.copies += 1
            self._sync_times.add(sync_id_time(sync_id))
            self._keep_newest(Candidate(address + at, region, sync_id))

    def chunk(self, view: bytes, address: int, region: Region) -> None:
        if self._tail_region is not None and self._tail and address == self._tail_end:
            joined = self._tail + view[:ANCHOR_WITH_ID]
            self._collect(joined, self._tail_end - len(self._tail), self._tail_region, len(self._tail))
        self._collect(view, address, region, len(view))
        self._tail = view[max(0, len(view) - ANCHOR_WITH_ID):]
        self._tail_end = address + len(view)
        self._tail_region = region

    def wants_wider_scan(self) -> bool:
        return self.copies == 0

    def finish(self, reader) -> None:
        self._seen = set()
        self._tail_region = None
        if not self.defer:
            self.extract(reader)

    def has_candidates(self) -> bool:
        return bool(self._candidates)

    def extract(self, reader) -> None:
        """Parse the newest complete copy among the collected anchors."""
        ordered = sorted(self._candidates, key=lambda c: sync_id_time(c.sync_id), reverse=True)
        self._candidates = []
        attempts = 0
        for cand in ordered:
            if self.newest and sync_id_time(cand.sync_id) <= self.newest.sync_time:
                break
            if attempts >= MAX_EXTRACTIONS_PER_PROCESS:
                break
            attempts += 1
            self.extractions += 1
            copy = extract_candidate(reader, cand)
            if copy:
                self.newest = copy
                break
            self.failed_extractions += 1

    def stats(self) -> InventoryScanStats:
        return InventoryScanStats(self.copies, sorted(self._sync_times, reverse=True)[:MAX_REPORTED_SYNC_TIMES],
                                  self.extractions, self.failed_extractions)


@dataclass
class GameInventoryRead:
    status: str  # ok | process-not-found | access-denied | not-found
    newest: GameInventoryCopy | None
    stats: InventoryScanStats
    scan_ms: int
    scanned_mb: int
    regions: int

    def summary(self) -> dict:
        return {"status": self.status, "scan_ms": self.scan_ms, "scanned_mb": self.scanned_mb,
                "regions": self.regions, "copies": self.stats.copies,
                "extractions": self.stats.extractions, "failed_extractions": self.stats.failed_extractions,
                "newest_sync_time": self.newest.sync_time if self.newest else None}


def inventory_read_from(collector: InventoryCollector, scan: ScanResult) -> GameInventoryRead:
    if scan.failure:
        status = "process-not-found" if scan.failure == "process-not-found" else "access-denied"
    else:
        status = "ok" if collector.newest else "not-found"
    return GameInventoryRead(status, collector.newest, collector.stats(), scan.ms,
                             scan.bytes // (1024 * 1024), scan.regions)


def read_game_inventory(pid: int | None = None, proc_root: str = "/proc") -> GameInventoryRead:
    """The game's own resident inventory JSON, read-only, without any request to DE."""
    c = InventoryCollector()
    return inventory_read_from(c, scan_process([c], pid, proc_root))


def complete_deferred(collector: InventoryCollector, scan: ScanResult, proc_root: str = "/proc"
                      ) -> GameInventoryRead:
    """Extract a deferred collector's newest copy by reopening the same process.
    The anchors' addresses may have moved since the walk; extract_candidate
    validates each copy, so a stale anchor just yields no copy."""
    if collector.has_candidates() and scan.failure is None and scan.pid:
        base = os.path.join(proc_root, str(scan.pid))
        try:
            fd = os.open(os.path.join(base, "mem"), os.O_RDONLY)
        except OSError as e:
            _log.warning("deferred inventory read: cannot reopen game memory: %s", e)
        else:
            try:
                with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as f:
                    maps = f.read().split("\n")
                collector.extract(MemoryReader(fd, maps))
            except OSError as e:
                _log.warning("deferred inventory read failed: %s", e)
            finally:
                os.close(fd)
    return inventory_read_from(collector, scan)


def read_game(pid: int | None = None, proc_root: str = "/proc", want_inventory: bool = True,
              defer_inventory: bool = False
              ) -> tuple[tuple[str | None, str], GameInventoryRead | Callable[[], GameInventoryRead] | None,
                         ScanResult]:
    """One walk for both: ((authz, reason), resident read or None, scan).

    defer_inventory: the resident part comes back as a zero-argument callable
    that does the extraction only when called (the API fallback path)."""
    a = AuthzVisitor()
    visitors: list = [a]
    c = InventoryCollector(defer=defer_inventory) if want_inventory else None
    if c:
        visitors.append(c)
    scan = scan_process(visitors, pid, proc_root)
    if c is None:
        resident = None
    elif defer_inventory and scan.failure is None:
        resident = lambda: complete_deferred(c, scan, proc_root)  # noqa: E731
    else:
        resident = inventory_read_from(c, scan)
    return a.result(scan), resident, scan
