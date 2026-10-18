"""Inventory: memory scan patterns on synthetic /proc images, the inventory.php
fetch, payload parsing, ownership counts and the refresh scheduler."""

import asyncio
import json
import os
import time

import pytest

from wfh import inventory as inv_mod, net, rpc
from wfh.inventory import InventoryService, api, memory, model, payload
from wfh.inventory.memory import (
    InventoryCollector, Region, best_authz, new_diagnostics, parse_authz_at, region_from_maps_line,
    scan_buffer_for_authz, span_around, sync_id_time,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "inventory")
ACC = "0123456789abcdef01234567"
VALID = f"?accountId={ACC}&nonce=1712345678901"
OTHER = "?accountId=ffffffffffffffffffffffff&nonce=99"
PLASTIDS = "/Lotus/Types/Items/MiscItems/Plastids"
OLD_SYNC = "6aaf0000aaaaaaaaaaaaaaaa"
NEW_SYNC = "6aaf1000bbbbbbbbbbbbbbbb"


def fixture_bytes(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def inventory_json(sync_id: str, plastids: int, padding: int = 0) -> str:
    return json.dumps({
        "SubscribedToEmails": 0,
        "Created": {"$date": {"$numberLong": "1"}},
        "MiscItems": [{"ItemType": PLASTIDS, "ItemCount": plastids}],
        "Tricky": 'braces } { and a "quote" and a trailing backslash \\',
        "Filler": "x" * padding,
        "LastInventorySync": {"$oid": sync_id},
        "RegularCredits": 100,
    }, separators=(",", ":"))


# ---- payload ------------------------------------------------------------------------

def test_payload_shapes_and_envelopes():
    raw = fixture_bytes("inventory_api.json")
    data = payload.parse_inventory_bytes(raw)
    assert payload.has_inventory_shape(data)
    assert data["RewardSeed"] == -5916838464512853127  # 64-bit ints stay exact
    assert payload.parse_inventory_bytes(fixture_bytes("inventory_envelope.json")) == data
    assert payload.unwrap_inventory_payload({"data": {"payload": data}}) is data
    assert payload.unwrap_inventory_text(fixture_bytes("inventory_envelope.json").decode()) is not None
    assert payload.unwrap_inventory_payload({"x": 1}, return_input_on_failure=True) == {"x": 1}
    for bad in (b"[]", b"{}", b"not json", b'{"InventoryJson": "{broken"}'):
        with pytest.raises(payload.InventoryFormatError):
            payload.parse_inventory_bytes(bad)


# ---- auth query patterns (gameMemoryAuthz.test.ts) ------------------------------------

def test_parse_authz_at():
    buf = b"noise\x00\x01" + VALID.encode() + b"\x00tail"
    assert parse_authz_at(buf, buf.index(b"?accountId=")) == VALID
    assert parse_authz_at(f"?accountId={ACC}&nonce=42&extra=x".encode(), 0) == f"?accountId={ACC}&nonce=42"
    b2 = f"?platform=pc&accountId={ACC}&nonce=42".encode()
    assert parse_authz_at(b2, b2.index(b"&accountId=")) == f"?accountId={ACC}&nonce=42"
    assert parse_authz_at(f"?accountId={ACC.upper()}&nonce=42".encode(), 0) == f"?accountId={ACC}&nonce=42"
    assert parse_authz_at(b"?accountId=zzz456789abcdef01234567&nonce=42", 0) is None
    assert parse_authz_at(b"?accountId=abc&nonce=42", 0) is None
    assert parse_authz_at(f"?accountId={ACC}?nonce=42".encode(), 0) is None
    assert parse_authz_at(f"?accountId={ACC}&nonce=x".encode(), 0) is None
    n24 = "1" * 24
    assert parse_authz_at(f"?accountId={ACC}&nonce={n24}\x00".encode(), 0) == f"?accountId={ACC}&nonce={n24}"
    assert parse_authz_at(f"?accountId={ACC}&nonce={'1' * 25}\x00".encode(), 0) is None
    assert parse_authz_at(f"?accountId={ACC}&nonce=".encode(), 0) is None


def test_scan_buffer_counts_and_best():
    counts = {}
    scan_buffer_for_authz(f"{VALID} junk {VALID} \x00 {OTHER} {VALID}".encode("latin-1"), counts)
    assert counts == {VALID: 3, OTHER: 1}
    assert best_authz(counts) == (VALID, 3, False)
    counts = {}
    assert scan_buffer_for_authz(f"?accountId=bad&nonce=1 {VALID}".encode(), counts) == 2
    assert counts == {VALID: 1}
    assert best_authz({}) == (None, 0, False)
    assert best_authz({VALID: 2, OTHER: 2}) == (None, 2, True)
    leader = "?accountId=aaaaaaaaaaaaaaaaaaaaaaaa&nonce=7"
    assert best_authz({VALID: 2, OTHER: 2, leader: 3}) == (leader, 3, False)


def test_rejection_diagnostics():
    buf = " ".join([f"?accountId={'z' * 24}&nonce=1", f"?accountId={ACC}?nonce=1", f"?accountId={ACC}&nonce=x",
                    f"?accountId={ACC}&nonce={'1' * 25}", f"?accountId={ACC}&nonce="]).encode()
    d = new_diagnostics()
    assert scan_buffer_for_authz(buf, {}, d) == 5
    assert d == {"truncated": 1, "invalidAccountId": 1, "missingNonce": 1, "emptyNonce": 1, "nonceTooLong": 1}


def test_maps_lines():
    anon = "7f1e40000000-7f1e40021000 rw-p 00000000 00:00 0 "
    memfd = "7f1e2c000000-7f1e2c400000 rw-p 00000000 00:01 99   /memfd:wine-shm (deleted)"
    nvidia = "7f1e30000000-7f1e30010000 rw-p 00000000 00:06 1234    /dev/nvidia0"
    assert region_from_maps_line(anon) == Region(0x7F1E40000000, 0x7F1E40021000)
    assert region_from_maps_line("1000-2000 rw-p 00000000 00:00 0") == Region(0x1000, 0x2000)
    assert region_from_maps_line(nvidia) is None and region_from_maps_line(nvidia, True) is None
    assert region_from_maps_line("7f1e31000000-7f1e31010000 rw-p 00000000 00:06 1235 /dev/dri/renderD128") is None
    assert region_from_maps_line("55a1b2c00000-55a1b2c21000 r-xp 00000000 08:02 12345 /usr/bin/wine64", True) is None
    for special in ("[vvar]", "[vdso]"):
        assert region_from_maps_line(f"7ffd8b1f9000-7ffd8b1fd000 rw-p 00000000 00:00 0   {special}", True) is None
    assert region_from_maps_line("ffffffffff600000-ffffffffff601000 rw-p 00000000 00:00 0   [vsyscall]") is None
    for perms in ("r--p", "rw-s", "---p"):
        assert region_from_maps_line(f"1000-2000 {perms} 00000000 00:00 0 ") is None
    assert region_from_maps_line("100000000-500000001000 rw-p 00000000 00:00 0 ") is None
    assert region_from_maps_line("2000-2000 rw-p 00000000 00:00 0 ") is None
    assert region_from_maps_line("not a maps line") is None
    assert region_from_maps_line(memfd) is None
    assert region_from_maps_line(memfd, True) == Region(0x7F1E2C000000, 0x7F1E2C400000)


def test_span_around():
    regions = [Region(0x1000, 0x2000), Region(0x2000, 0x3000), Region(0x3000, 0x4000), Region(0x5000, 0x6000)]
    assert span_around(regions, 0x2800, 0x10000) == Region(0x1000, 0x4000)
    assert span_around(regions, 0x2800, 0x100) == Region(0x2700, 0x2900)
    assert span_around(regions, 0x5800, 0x10000) == Region(0x5000, 0x6000)
    for addr in (0x800, 0x4800, 0x7000):
        assert span_around(regions, addr, 0x10000) is None


# ---- resident inventory collector (gameMemoryInventory.test.ts) -----------------------

class BufReader:
    def __init__(self, memory: bytes, base: int):
        self.memory, self.base = memory, base

    def read(self, address, size):
        off = address - self.base
        return self.memory[off:off + size]

    def readable_span(self, address, limit):
        return None


def walk(memory: bytes, chunk: int, overlap: int = 256):
    base = 0x10000
    region = Region(base, base + len(memory))
    c = InventoryCollector()
    off = 0
    while off < len(memory):
        c.chunk(memory[off:off + chunk], base + off, region)
        if off + chunk >= len(memory):
            break
        off += chunk - overlap
    c.finish(BufReader(memory, base))
    return c.newest, c.stats()


def test_collector_object_bounds():
    js = inventory_json(NEW_SYNC, 3)
    newest, _ = walk(bytes([0, 0, 7, 0x7B, 0x7D]) + js.encode() + bytes([0, 0x7D, 0]), 64 * 1024)
    assert newest.inventory == json.loads(js)
    for broken in (bytes([1, 2, 3]) + js[1:].encode(), js[:-1].encode() + b"\x00\x00"):
        newest, stats = walk(broken, 64 * 1024)
        assert newest is None
        assert (stats.copies, stats.extractions, stats.failed_extractions) == (1, 1, 1)


def test_sync_ids():
    assert sync_id_time(NEW_SYNC) == 0x6AAF1000 * 1000
    assert memory.inventory_sync_id({"LastInventorySync": {"$oid": NEW_SYNC}}) == NEW_SYNC
    assert memory.inventory_sync_id({"LastInventorySync": {"$oid": "short"}}) is None
    assert memory.inventory_sync_id(None) is None


def test_collector_newest_and_fallbacks():
    mem = (inventory_json(OLD_SYNC, 1).encode() + bytes(64) + b'"LastInventorySync" in some unrelated string table'
           + bytes(64) + inventory_json(NEW_SYNC, 9).encode() + bytes(64))
    newest, stats = walk(mem, 64 * 1024)
    assert newest.sync_id == NEW_SYNC and newest.inventory["MiscItems"] == [{"ItemType": PLASTIDS, "ItemCount": 9}]
    assert stats.copies == 2 and stats.sync_times == [sync_id_time(NEW_SYNC), sync_id_time(OLD_SYNC)]
    assert (stats.extractions, stats.failed_extractions) == (1, 0)

    torn = inventory_json(NEW_SYNC, 9)
    mem = inventory_json(OLD_SYNC, 1).encode() + bytes(32) + torn[:40].encode() + bytes([0, 0, 0, 1]) + \
        torn[44:].encode() + bytes(32)
    newest, stats = walk(mem, 64 * 1024)
    assert newest.sync_id == OLD_SYNC and (stats.copies, stats.extractions, stats.failed_extractions) == (2, 2, 1)

    stale = f'"LastInventorySync":{{"$oid":"{OLD_SYNC}"}} padding'.encode()
    newest, stats = walk(stale * 300 + bytes(64) + inventory_json(NEW_SYNC, 9).encode() + bytes(64), 64 * 1024)
    assert stats.copies == 301 and newest.sync_id == NEW_SYNC

    newest, stats = walk(b"A" * 4096, 1024)
    assert newest is None and stats.copies == 0


def test_collector_chunk_boundaries():
    chunk, step = 1024, 1024 - 256
    js = inventory_json(NEW_SYNC, 4, 3000).encode()
    prefix = ((100 - js.index(b'"LastInventorySync"')) % step + step) % step
    mem = bytes(prefix) + js + bytes(100)
    assert mem.index(b'"LastInventorySync"') % step == 100
    newest, stats = walk(mem, chunk)
    assert newest.sync_id == NEW_SYNC and stats.copies == 1


# ---- synthetic /proc/<pid>/{maps,mem} -----------------------------------------------------

REGION = 64 * 1024


def make_proc(tmp_path, regions: list[tuple[int, bytes, str, str]], pid: int = 4242) -> str:
    """regions: (start, contents, perms, pathname). mem is a sparse file where
    file offset == virtual address, as /proc/<pid>/mem reads."""
    root = tmp_path / "proc"
    d = root / str(pid)
    d.mkdir(parents=True)
    lines = []
    with open(d / "mem", "wb") as f:
        for start, contents, perms, path in regions:
            f.seek(start)
            f.write(contents)
            lines.append(f"{start:x}-{start + len(contents):x} {perms} 00000000 00:00 0 {path}".rstrip() + " ")
    (d / "maps").write_text("\n".join(lines) + "\n")
    return str(root)


def test_scan_synthetic_process(tmp_path):
    heap1 = bytearray(REGION)
    for i, s in enumerate((VALID, OTHER, VALID, VALID)):
        heap1[1000 + i * 300:1000 + i * 300 + len(s)] = s.encode()
    # A resident copy spanning two adjacent anonymous regions.
    js = inventory_json(NEW_SYNC, 7, 70_000).encode()
    two = bytearray(2 * REGION)
    two[4000:4000 + len(js)] = js
    device = bytearray(REGION)
    device[0:len(OTHER)] = OTHER.encode()
    device[200:200 + len(OTHER)] = OTHER.encode()
    device[400:400 + len(OTHER)] = OTHER.encode()
    device[600:600 + len(OTHER)] = OTHER.encode()
    root = make_proc(tmp_path, [
        (0x100000, bytes(heap1), "rw-p", ""),
        (0x200000, bytes(two[:REGION]), "rw-p", ""),
        (0x200000 + REGION, bytes(two[REGION:]), "rw-p", ""),
        (0x400000, bytes(device), "rw-p", "/dev/nvidia0"),
        (0x500000, bytes(device), "r--p", ""),
    ])
    (authz, reason), resident, scan = memory.read_game(4242, root)
    assert scan.failure is None and scan.regions == 3
    assert (authz, reason) == (VALID, "ok-3x")
    assert resident.status == "ok"
    assert resident.newest.sync_id == NEW_SYNC
    assert resident.newest.inventory["MiscItems"] == [{"ItemType": PLASTIDS, "ItemCount": 7}]


def test_scan_widens_to_file_backed_when_anonymous_is_empty(tmp_path):
    shm = bytearray(REGION)
    shm[100:100 + len(VALID)] = VALID.encode()
    root = make_proc(tmp_path, [
        (0x100000, bytes(REGION), "rw-p", ""),
        (0x300000, bytes(shm), "rw-p", "/memfd:wine-shm (deleted)"),
    ])
    assert memory.read_game_authz(4242, root) == (VALID, "ok-1x")
    inv = memory.read_game_inventory(4242, root)
    assert inv.status == "not-found" and inv.newest is None


def test_scan_failures(tmp_path):
    root = make_proc(tmp_path, [(0x100000, bytes(REGION), "rw-p", "")])
    assert memory.read_game_authz(4242, root) == (None, "crumbs-not-found")
    assert memory.read_game_authz(999, root) == (None, "process-not-found")
    assert memory.scan_process([], None, root).failure == "process-not-found"
    tied = bytearray(REGION)
    tied[0:len(VALID)] = VALID.encode()
    tied[500:500 + len(OTHER)] = OTHER.encode()
    root2 = make_proc(tmp_path / "b", [(0x100000, bytes(tied), "rw-p", "")])
    assert memory.read_game_authz(4242, root2) == (None, "crumbs-ambiguous")


# ---- inventory.php -----------------------------------------------------------------------------

def test_fetch_inventory_falls_back_to_mobile_host():
    calls = []
    body = fixture_bytes("inventory_api.json")

    def fake_fetch(url, headers=None, timeout=None, retry_network=True):
        assert retry_network is False
        calls.append((url, headers, timeout))
        if "api.warframe.com" in url:
            raise net.HttpError(503, url, "busy")
        return body

    assert api.fetch_inventory(VALID, fake_fetch) == (body, "mobile.warframe.com")
    assert calls[0][0] == f"https://api.warframe.com/api/inventory.php{VALID}"
    assert calls[1][0] == f"https://mobile.warframe.com/api/inventory.php{VALID}"
    assert calls[0][1] == {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
    assert calls[0][2] == 30


def test_fetch_inventory_errors():
    def empty(url, headers=None, timeout=None):
        return b""
    with pytest.raises(api.InventoryFetchError):
        api.fetch_inventory(VALID, empty)
    with pytest.raises(ValueError):
        api.inventory_url("api.warframe.com", "?accountId=x&nonce=1&evil=1")


# ---- counts -------------------------------------------------------------------------------------

def test_owned_counts_and_relics():
    inv = model.Inventory(payload.parse_inventory_bytes(fixture_bytes("inventory_api.json")), "api")
    # Blueprint <-> Component alias: 2 built chassis vs 1 blueprint -> max is 2.
    assert inv.owned_count("/Lotus/Types/Recipes/WarframeRecipes/MesaPrimeChassisBlueprint") == 2
    # Helmet: stacks 2 + 1 minus one consumed by a pending foundry build.
    assert inv.owned_count("/Lotus/Types/Recipes/WarframeRecipes/MesaPrimeHelmetBlueprint") == 2
    # Weapon part listed without the Blueprint suffix matches the blueprint stack.
    assert inv.owned_count("/Lotus/Types/Recipes/Weapons/WeaponParts/BratonPrimeBarrel") == 3
    assert inv.owned_count("/Lotus/Powersuits/Mag/MagPrime") == 1
    assert inv.owned_count("/Lotus/Types/Items/MiscItems/Plastids") == 2210
    assert inv.owned_count("/Lotus/Nope") == 0
    assert inv.relic_counts()["/Lotus/Types/Game/Projections/T1VoidProjectionVPrimeBronze"] == 4
    assert inv.relic_counts()["/Lotus/Types/Game/Projections/T4VoidProjectionAxiA1Gold"] == 0
    names = {
        "/Lotus/Types/Game/Projections/T1VoidProjectionVPrimeBronze": "Lith V1 Intact",
        "/Lotus/Types/Game/Projections/T1VoidProjectionVPrimePlatinum": "Lith V1 Radiant",
        "/Lotus/Types/Game/Projections/T3VoidProjectionGaussPrimeDBronze": "Neo G5 Intact",
    }
    assert inv.owned_relics(names.get) == {("Lith V1", "intact"): 4, ("Lith V1", "radiant"): 1,
                                           ("Neo G5", "intact"): 7}
    assert inv.sync_id == "68f1a2b3c4d5e6f708192a3b" and inv.sync_time == 0x68F1A2B3 * 1000
    assert inv.summary()["relics"] == 12


def test_relic_counts_rules():
    data = {"MiscItems": [{"ItemType": "/A/VoidProjectionX", "ItemCount": 2},
                          {"ItemType": "/A/VoidProjectionX"}, {"ItemType": "/A/VoidProjectionY", "ItemCount": -3}],
            "Recipes": [{"ItemType": "/A/VoidProjectionX", "ItemCount": 1}]}
    assert model.collect_relic_counts(data) == {"/A/VoidProjectionX": 3, "/A/VoidProjectionY": 0}
    assert model.collect_relic_counts({"Other": [{"ItemType": "/Lotus/Relics/Z", "ItemCount": 2}]}) == \
        {"/Lotus/Relics/Z": 2}


# ---- the service ---------------------------------------------------------------------------------

class FakeSettings:
    def __init__(self, **values):
        self.values = {"inventory_source": "memory", "inventory_file": "", "inventory_auto_sync": True,
                       "inventory_refresh_min": 10, "inventory_mission_scan": True, **values}
        self.updates = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def update(self, patch):
        self.updates.append(patch)
        self.values.update(patch)
        return dict(self.values)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def resident_read(sync_id: str | None, plastids: int = 5):
    if sync_id is None:
        return memory.GameInventoryRead("not-found", None, memory.InventoryScanStats(), 1, 1, 1)
    data = json.loads(inventory_json(sync_id, plastids))
    copy = memory.GameInventoryCopy(sync_id, sync_id_time(sync_id), data)
    return memory.GameInventoryRead("ok", copy, memory.InventoryScanStats(1, [copy.sync_time], 1, 0), 1, 1, 1)


def sync_at(epoch_s: float) -> str:
    return f"{int(epoch_s):08x}" + "c" * 16


def make_service(tmp_path, clock, authz=(VALID, "ok-3x"), resident=None, fetch=None, running=True,
                 ee_log=None, **settings):
    scans = []

    def scan(want_inventory):
        scans.append(want_inventory)
        r = resident() if callable(resident) else resident
        return authz, r, memory.ScanResult(None, 1, 1024 * 1024, 5, 4242)

    fetches = []

    def default_fetch(a):
        fetches.append(a)
        return fixture_bytes("inventory_api.json"), "api.warframe.com"

    events = []
    svc = InventoryService(str(tmp_path / "runtime"), FakeSettings(**settings),
                           emit=lambda n, d: events.append(n), clock=clock, scan=scan,
                           fetcher=fetch or default_fetch, game_running=lambda: running,
                           ee_log_path=lambda: ee_log, home=lambda: str(tmp_path))
    svc.apply_settings(force=True)
    return svc, scans, fetches, events


def test_service_fetches_and_persists(tmp_path):
    clock = Clock()
    svc, scans, fetches, events = make_service(tmp_path, clock)
    seen = []
    svc.on_update(seen.append)
    assert svc.current is None
    assert svc.run_memory("manual") is True
    assert fetches == [VALID]
    assert svc.current.origin == "api" and svc.current.owned_count(PLASTIDS) == 2210
    assert seen == [svc.current] and "inventory.updated" in events
    saved = (tmp_path / "runtime" / "inventory.json").read_bytes()
    assert saved == fixture_bytes("inventory_api.json")  # raw body kept byte for byte
    # A restarted service reloads the snapshot and keeps the cooldown.
    svc2, *_ = make_service(tmp_path, clock)
    assert svc2.current.sync_id == "68f1a2b3c4d5e6f708192a3b"
    assert svc2.last_api_at == clock.t
    os.utime(tmp_path / "runtime" / "inventory.json", (clock.t, clock.t))
    assert svc2.next_auto_at() == pytest.approx(clock.t + 600)


def test_service_failure_reasons(tmp_path):
    clock = Clock()
    svc, *_ = make_service(tmp_path, clock, running=False)
    assert svc.run_memory() is False and svc.last_reason == "game-not-running"
    assert svc.next_auto_at() == clock.t + 90

    svc, *_ = make_service(tmp_path / "a", clock, authz=(None, "process-not-found"))
    svc.run_memory()
    assert svc.last_reason == "game-not-running"
    svc, *_ = make_service(tmp_path / "b", clock, authz=(None, "mem-open-EACCES"))
    svc.run_memory()
    assert svc.last_reason == "access-denied"

    ee = tmp_path / "EE.log"
    ee.write_text("1.0 Sys [Info]: Starting\n")
    svc, *_ = make_service(tmp_path / "c", clock, authz=(None, "crumbs-not-found"), ee_log=str(ee))
    svc.run_memory()
    assert svc.last_reason == "not-logged-in"
    ee.write_text("1.0 Sys [Info]: Starting\n20.0 Sys [Info]: Logging in as Tenno123\n")
    svc.run_memory()
    assert svc.last_reason == "token-not-found"
    assert svc.next_auto_at() == clock.t + 600  # persistent failure: no fast retry


def test_service_api_failure_uses_resident_copy(tmp_path):
    clock = Clock()

    def down(a):
        raise api.InventoryFetchError("api.warframe.com returned HTTP 503")

    svc, *_ = make_service(tmp_path, clock, resident=resident_read(NEW_SYNC, 42), fetch=down)
    assert svc.run_memory() is False
    assert svc.last_reason == "api-failed"
    assert svc.current.origin == "memory" and svc.current.owned_count(PLASTIDS) == 42


def test_mission_end_reads_resident_inventory(tmp_path):
    clock = Clock()
    reads = iter([resident_read(sync_at(clock.t - 3600)), resident_read(sync_at(clock.t + 2), 77)])
    svc, scans, fetches, _ = make_service(tmp_path, clock, resident=lambda: next(reads), inventory_auto_sync=False)
    svc.on_line("812.4 Sys [Info]: EOM missionLocationUnlocked=1")
    svc.on_line("813.0 Script [Info]: TopMenu.lua: Abort: mission failed")  # same mission: joined
    svc.tick()
    assert scans == []  # waits READ_DELAY first
    clock.t += 3
    svc.tick()
    assert len(scans) == 1  # copy older than the mission end: retry
    clock.t += 1
    svc.tick()
    assert len(scans) == 2
    assert svc.current.origin == "memory" and svc.current.owned_count(PLASTIDS) == 77
    assert fetches == [] and svc.last_trigger == "mission-end"


def test_mission_end_falls_back_to_api_when_memory_is_stale(tmp_path):
    clock = Clock()
    svc, scans, fetches, _ = make_service(tmp_path, clock, resident=resident_read(None), inventory_auto_sync=False)
    svc.on_mission_end("Sys [Info]: EOM missionLocationUnlocked=1")
    clock.t += 3
    svc.tick()
    clock.t += 1
    svc.tick()
    assert len(scans) == 2 and fetches == [VALID]
    # Next mission within the refresh interval: no second API call.
    clock.t += 60
    svc.on_mission_end("Sys [Info]: EOM missionLocationUnlocked=1")
    clock.t += 3
    svc.tick()
    clock.t += 1
    svc.tick()
    assert len(scans) == 4 and fetches == [VALID]


def test_hub_abort_and_disabled_scan_are_ignored(tmp_path):
    clock = Clock()
    svc, *_ = make_service(tmp_path, clock)
    svc.on_line("Script [Info]: TopMenu.lua: Abort: in hub")
    assert svc._pending_mission is None
    svc2, *_ = make_service(tmp_path / "x", clock, inventory_mission_scan=False)
    svc2.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert svc2._pending_mission is None


def test_login_and_auto_schedule(tmp_path):
    clock = Clock()
    svc, scans, fetches, _ = make_service(tmp_path, clock)
    svc.run_memory()  # fresh snapshot
    assert len(fetches) == 1
    svc.on_line("Script [Info]: ThemedMainMenu.lua: MainMenu::LoginDone result=true")
    clock.t += 2
    svc.tick()
    assert len(fetches) == 1  # last run was ok: no login retry
    clock.t += 600
    svc.tick()
    assert len(fetches) == 2  # interval elapsed

    svc2, _, fetches2, _ = make_service(tmp_path / "y", clock, authz=(None, "crumbs-not-found"))
    svc2.run_memory()
    svc2.on_line("Script [Info]: ThemedMainMenu.lua: MainMenu::LoginDone result=true")
    clock.t += 2
    svc2.tick()
    assert svc2.last_trigger == "login"


def test_file_source_import_and_reload(tmp_path):
    clock = Clock()
    svc, scans, fetches, _ = make_service(tmp_path, clock)
    target = tmp_path / "export.json"
    target.write_bytes(fixture_bytes("inventory_envelope.json"))
    st = svc.import_file("~/export.json")  # ~ is the player's home, not root's
    assert st["source"] == "file" and st["file"] == str(target)
    assert svc.settings.updates[-1] == {"inventory_source": "file", "inventory_file": str(target)}
    assert svc.current.origin == "file" and svc.current.owned_count(PLASTIDS) == 2210
    data = json.loads(fixture_bytes("inventory_api.json"))
    data["MiscItems"][0]["ItemCount"] = 1
    target.write_text(json.dumps(data))
    os.utime(target, (time.time() + 5, time.time() + 5))
    clock.t += 11
    svc.tick()
    assert svc.current.owned_count(PLASTIDS) == 1
    svc.refresh()
    assert scans == [] and fetches == []  # a user-owned file is never replaced by a scan
    with pytest.raises(ValueError):
        svc.import_file(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": 1}')
    with pytest.raises(ValueError):
        svc.import_file(str(bad))
    assert svc.current.origin == "file"


def test_source_switch(tmp_path):
    clock = Clock()
    svc, *_ = make_service(tmp_path, clock)
    svc.run_memory()
    svc.settings.values["inventory_source"] = "none"
    svc.apply_settings()
    assert svc.current is None
    svc.settings.values["inventory_source"] = "helper"  # WFHelper's name
    svc.apply_settings()
    assert svc.current is not None and svc.current.origin == "api"


# ---- module API through the worker ------------------------------------------------------------------

def test_module_api_and_rpcs(app, tmp_path):
    assert "inventory.status" in rpc.methods()
    assert "inventory.refresh" in rpc.methods() and "inventory.import_file" in rpc.methods()
    st = asyncio.run(rpc.call_local("inventory.status"))
    assert st["source"] == "memory"
    target = tmp_path / "inv.json"
    target.write_bytes(fixture_bytes("inventory_api.json"))
    st = asyncio.run(rpc.call_local("inventory.import_file", {"path": str(target)}))
    assert st["found"] and st["inventory"]["origin"] == "file"
    assert inv_mod.owned_count("/Lotus/Types/Items/MiscItems/Plastids") == 2210
    assert isinstance(inv_mod.owned_relics(), dict)
    assert inv_mod.inventory().sync_id == "68f1a2b3c4d5e6f708192a3b"
    app.settings.update({"inventory_source": "memory", "inventory_file": ""})


# ---- review fixes ------------------------------------------------------------------------------

def test_late_file_end_line_is_dated_by_its_uptime_stamp(tmp_path):
    clock = Clock()
    svc, scans, _, _ = make_service(tmp_path, clock, inventory_auto_sync=False)
    real_end = clock.t
    # A prompt line fixes the offset; the EOM line itself reaches the file 27 s late.
    svc.observe_file_line("800.000 Sys [Info]: earlier")
    clock.t += 39
    eom = "812.000 Sys [Info]: EOM missionLocationUnlocked=1"
    svc.observe_file_line(eom)
    svc.reads = iter([resident_read(sync_at(real_end - 5), 55)])
    svc._scan = lambda eager: (scans.append(eager) or ((VALID, "ok-1x"), next(svc.reads),
                                                        memory.ScanResult(None, 1, 1, 1, 4242)))
    svc.on_line(eom)
    assert svc._pending_mission["ended_at"] == pytest.approx(real_end + 12)
    svc.tick()  # already past end + 3 s: read at once
    assert scans == [True]
    # Synced 5 s before the end, 32 s before the line arrived: still fresh enough.
    assert svc.current.origin == "memory" and svc.current.owned_count(PLASTIDS) == 55


def test_realtime_end_line_is_now(tmp_path):
    clock = Clock()
    src = {"v": "realtime"}
    svc, *_ = make_service(tmp_path, clock)
    svc._line_source = lambda: src["v"]
    svc.observe_file_line("800.000 Sys [Info]: earlier")
    clock.t += 60
    svc.observe_file_line("810.000 Sys [Info]: late batch")  # file is 50 s behind
    svc.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert svc._pending_mission["ended_at"] == clock.t


def test_hub_node_end_is_ignored(tmp_path):
    clock = Clock()
    svc, *_ = make_service(tmp_path, clock)
    svc.on_line("Script [Info]: SyncAutoPopulatedConsumables for mission MT_PVP with location CetusHub4")
    svc.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert svc._pending_mission is None
    # A regular node is kept through OnStateStarted of the same type...
    svc.on_line("Script [Info]: SyncAutoPopulatedConsumables for mission MT_SURVIVAL with location SolNode1")
    svc.on_line("Game [Info]: OnStateStarted, mission type=MT_SURVIVAL")
    assert svc._mission_info == {"type": "MT_SURVIVAL", "node": "SolNode1"}
    # ...but a hub node is never carried (missionRewards.ts observeLine).
    svc.on_line("Script [Info]: SyncAutoPopulatedConsumables for mission MT_PVP with location CetusHub4")
    svc.on_line("Game [Info]: OnStateStarted, mission type=MT_PVP")
    assert svc._mission_info == {"type": "MT_PVP", "node": None}
    clock.t += 60
    svc.on_line("Sys [Info]: EOM missionLocationUnlocked=1")
    assert svc._pending_mission is not None


def test_settings_change_reloads_on_the_inventory_thread(tmp_path):
    clock = Clock()
    svc, *_ = make_service(tmp_path, clock)
    target = tmp_path / "inv.json"
    target.write_bytes(fixture_bytes("inventory_api.json"))
    svc.settings.values.update({"inventory_source": "file", "inventory_file": str(target)})
    svc.settings_changed()
    assert svc.current is None  # nothing parsed in the listener
    svc.tick()
    assert svc.current.origin == "file"


def test_broken_file_is_not_reparsed_and_clears_old_snapshot(tmp_path, monkeypatch):
    clock = Clock()
    svc, *_ = make_service(tmp_path, clock)
    svc.run_memory()
    assert svc.current.origin == "api"
    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": 1}')
    svc.settings.values.update({"inventory_source": "file", "inventory_file": str(bad)})
    svc.apply_settings()
    st = svc.status()
    assert st["source"] == "file" and st["found"] is False
    loads = []
    real = svc._load_file
    monkeypatch.setattr(svc, "_load_file", lambda p, quiet=False: loads.append(p) or real(p, quiet))
    clock.t += 11
    svc.tick()
    clock.t += 11
    svc.tick()
    assert loads == []  # unchanged broken file: not read again
    os.utime(bad, (time.time() + 5, time.time() + 5))
    clock.t += 11
    svc.tick()
    assert loads == [str(bad)]


def test_login_retries_after_api_failure(tmp_path):
    clock = Clock()

    def down(a):
        raise api.InventoryFetchError("unreachable")

    svc, scans, _, _ = make_service(tmp_path, clock, fetch=down, inventory_auto_sync=False)
    svc.run_memory()
    assert svc.last_reason == "api-failed"
    svc.on_line("Script [Info]: ThemedMainMenu.lua: MainMenu::LoginDone result=true")
    clock.t += 2
    svc.tick()
    assert svc.last_trigger == "login" and len(scans) == 2


def test_api_runs_defer_resident_extraction(tmp_path):
    clock = Clock()
    resolved = []

    def lazy():
        resolved.append(1)
        return resident_read(NEW_SYNC, 9)

    svc, scans, _, _ = make_service(tmp_path, clock)
    svc._scan = lambda eager: (scans.append(eager) or ((VALID, "ok-1x"), lazy, memory.ScanResult(None, 1, 1, 1, 4242)))
    assert svc.run_memory() is True
    assert scans == [False] and resolved == []  # API answered: resident copy never parsed

    def down(a):
        raise api.InventoryFetchError("unreachable")

    svc._fetch = down
    svc.run_memory()
    assert resolved == [1] and svc.current.origin == "memory"


def test_read_game_deferred_extracts_on_demand(tmp_path):
    js = inventory_json(NEW_SYNC, 7).encode()
    heap = bytearray(REGION)
    heap[100:100 + len(VALID)] = VALID.encode()
    heap[2000:2000 + len(js)] = js
    root = make_proc(tmp_path, [(0x100000, bytes(heap), "rw-p", "")])
    (authz, _), resident, scan = memory.read_game(4242, root, defer_inventory=True)
    assert authz == VALID and callable(resident)
    read = resident()
    assert read.status == "ok" and read.newest.sync_id == NEW_SYNC


def test_listener_registered_before_setup(app, monkeypatch):
    got = []
    svc = inv_mod._svc
    monkeypatch.setattr(inv_mod, "_svc", None)
    inv_mod.on_update(got.append)
    assert inv_mod._pending_listeners == [got.append]
    monkeypatch.setattr(inv_mod, "_svc", svc)
    inv_mod.setup(app)
    assert inv_mod._pending_listeners == []
    assert got.append in inv_mod._svc._listeners


def test_owned_relics_falls_back_per_relic_and_caches_by_identity(monkeypatch):
    import types
    import sys

    class EmptyRelics:
        def by_unique(self, u):
            return None

    class Items:
        def by_unique(self, u):
            return types.SimpleNamespace(name="Lith V1 Intact") if u.endswith("Bronze") else None

    monkeypatch.setitem(sys.modules, "wfh.data.relics", types.SimpleNamespace(relics=lambda: EmptyRelics()))
    monkeypatch.setitem(sys.modules, "wfh.data.items", types.SimpleNamespace(items=lambda: Items()))
    name_of = inv_mod._relic_name_of()
    assert name_of("/Lotus/Types/Game/Projections/T1VoidProjectionVPABronze") == "Lith V1 Intact"
    assert name_of("/Lotus/Unknown") is None
    inv = model.Inventory({"MiscItems": []}, "file", 0.0, "")
    monkeypatch.setattr(inv_mod, "_relic_cache", None)
    svc = types.SimpleNamespace(current=inv)
    monkeypatch.setattr(inv_mod, "_svc", svc)
    assert inv_mod.owned_relics() == {}
    assert inv_mod._relic_cache[0] is inv
