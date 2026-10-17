"""EE.log discovery, the Proton real-time feed, echo de-duplication and EE.cfg."""

import os
import pwd

import pytest

from wfh import eecfg, eelog, protonlog
from wfh.eelog import EELogTailer
from wfh.protonlog import DebugLineGate, ProtonLogTail, parse_wine_debugstr

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "inventory")


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---- parse_wine_debugstr (protonDebugstrMonitor.test.ts) ---------------------

def test_parse_modern_proton_line():
    raw = '1234.567:0048:004c:warn:seh:OutputDebugStringA "Sys [Info]: ThemedProjectionManager.lua: LoadingCompleteEnd\\n"'
    p = parse_wine_debugstr(raw)
    assert p.text == "Sys [Info]: ThemedProjectionManager.lua: LoadingCompleteEnd\n"
    assert p.truncated is False


def test_parse_legacy_debugstr_channel():
    assert parse_wine_debugstr('0048:004c:trace:debugstr:OutputDebugStringA "Pause countdown done"').text == \
        "Pause countdown done"


def test_parse_unescapes():
    assert parse_wine_debugstr('0048:004c:warn:seh:OutputDebugStringA "a \\"b\\" \\\\ c\\td"').text == 'a "b" \\ c\td'


def test_parse_hex_bytes_as_utf8():
    raw = ('104.279:0120:0124:warn:seh:OutputDebugStringA "ChatRedux::AddTab: Adding tab with channel name: '
           'FPlayer\\xee\\x80\\x80 to index 5\\n"')
    assert parse_wine_debugstr(raw).text == "ChatRedux::AddTab: Adding tab with channel name: FPlayer\ue000 to index 5\n"


def test_parse_truncated_and_wide():
    p = parse_wine_debugstr('0048:004c:warn:seh:OutputDebugStringA "You are offering: many items"...')
    assert (p.text, p.truncated) == ("You are offering: many items", True)
    assert parse_wine_debugstr('0048:004c:warn:seh:OutputDebugStringW L"wide \\x266a end"').text == "wide \u266a end"


def test_parse_rejects_other_lines():
    assert parse_wine_debugstr("0048:004c:fixme:d3d:wined3d_check something") is None
    assert parse_wine_debugstr('0048:004c:warn:seh:RtlRestoreContext "not ODS"') is None
    assert parse_wine_debugstr("plain EE.log style line") is None
    assert parse_wine_debugstr('0048:004c:warn:seh:OutputDebugStringA "cut off') is None


# ---- DebugLineGate (debugLineFilter.test.ts) ---------------------------------

RELIC = "Script [Info]: ThemedProjectionManager.lua: LoadingCompleteEnd"
RELIC_FALLBACK = "Script [Info]: ThemedProjectionManager.lua: PopulateInventoryGrid"
CHAT = "Sys [Info]: ChatRedux::AddTab: Adding tab with channel name: FUser to index 3"
REWARD = "Sys [Info]: Pause countdown done"


def test_gate_filters_and_cooldowns():
    g = DebugLineGate()
    assert not g.wants("Sys [Info]: some unrelated engine noise", 0)
    assert g.wants(REWARD.upper(), 0)
    g = DebugLineGate()
    assert g.wants(RELIC, 1)
    assert not g.wants(RELIC, 2)
    assert not g.wants(RELIC_FALLBACK, 8)
    assert g.wants(RELIC, 1 + 7.5)
    g = DebugLineGate()
    assert g.wants(CHAT, 1) and not g.wants(CHAT, 2.5) and g.wants(CHAT, 3)
    assert g.wants(REWARD, 1) and g.wants(REWARD, 1.001)
    for line in ("Sys [Info]: EOM missionLocationUnlocked=1", "Script [Info]: TopMenu.lua: Abort: mission failed",
                 "Script [Info]: ThemedMainMenu.lua: MainMenu::LoginDone result=true",
                 "Game [Info]: OnStateStarted, mission type=MT_CAPTURE"):
        assert DebugLineGate().wants(line, 0), line


def test_gate_weapon_loads_only_in_riven_window():
    g = DebugLineGate()
    load = "Sys [Info]: ResourceLoader 0x1234 (/Lotus/Weapons/Tenno/Shotgun/PrimeBoar) Found 1,081 items to load"
    assert not g.wants(load, 0.9)
    assert g.wants("ThemedDetailedPurchaseDialog.lua: DBG: HudVis 1", 1.0)
    assert g.wants(load, 1.1)
    assert not g.wants(load, 3.5)


# ---- ProtonLogTail -------------------------------------------------------------------

def test_tail_skips_existing_and_handles_partial_and_truncation(tmp_path):
    f = tmp_path / "steam-230410.log"
    f.write_text("stale 1\nstale 2\n")
    t = ProtonLogTail(str(f))
    assert t.poll(1) == []
    with open(f, "a") as h:
        h.write("new line\npart")
    assert t.poll(2) == ["new line"]
    with open(f, "a") as h:
        h.write("ial\n")
    assert t.poll(3) == ["partial"]
    assert t.is_fresh(3) and t.is_fresh(3 + 29) and not t.is_fresh(3 + 31)
    f.write_text("fresh\n")  # game relaunch truncates
    assert t.poll(4) == ["fresh"]
    t.close()


def test_tail_waits_for_missing_log(tmp_path):
    f = tmp_path / "steam-230410.log"
    t = ProtonLogTail(str(f))
    assert t.poll(1) == [] and t.poll(2) == []
    f.write_text("boot line\n")
    assert t.poll(3) == ["boot line"]


# ---- merged stream + echo de-duplication ------------------------------------------

def _tailer(clock):
    t = EELogTailer(clock=clock)
    merged, files, rt = [], [], []
    t.subscribe(merged.append)
    t.subscribe(files.append, source="file")
    t.subscribe(rt.append, source="realtime")
    return t, merged, files, rt


def test_realtime_line_fires_once_and_file_echo_is_dropped():
    clock = Clock()
    t, merged, files, rt = _tailer(clock)
    t.feed_proton_raw('1260.010:0048:004c:warn:seh:OutputDebugStringA "Sys [Info]: Pause countdown done\\n"')
    assert merged == [REWARD] and rt == [REWARD] and files == []
    clock.t += 14  # engine flush lag
    t.feed("1260.013 Sys [Info]: Pause countdown done")
    t.feed("1260.020 Sys [Info]: Some other line")
    assert merged == [REWARD, "1260.020 Sys [Info]: Some other line"]
    assert files == ["1260.013 Sys [Info]: Pause countdown done", "1260.020 Sys [Info]: Some other line"]
    assert t.echoes_dropped == 1 and t.realtime_lines == 1


def test_echo_window_and_pairing():
    clock = Clock()
    t, merged, _, _ = _tailer(clock)
    t.feed_realtime(REWARD + "\n")
    t.feed_realtime(REWARD + "\n")  # the game really logged it twice
    clock.t += 5
    t.feed("10.000 " + REWARD)
    t.feed("10.000 " + REWARD)  # same stamp: a repeated copy, still an echo
    t.feed("10.500 " + REWARD)  # second real line pairs with second real-time copy
    assert merged == [REWARD, REWARD]
    clock.t += 31
    t.feed("50.000 " + REWARD)  # nothing real-time inside the window: a new event
    assert merged[-1] == "50.000 " + REWARD


def test_gate_suppressed_repeat_echo_is_dropped_too():
    clock = Clock()
    t, merged, files, _ = _tailer(clock)
    t.feed_realtime(RELIC)
    clock.t += 0.4
    t.feed_realtime(RELIC)  # within the picker cooldown: not forwarded
    clock.t += 12
    t.feed("1.0 " + RELIC)
    t.feed("1.4 " + RELIC)
    assert merged == [RELIC]
    assert len(files) == 2


def test_truncated_payload_comes_from_the_file():
    clock = Clock()
    t, merged, _, _ = _tailer(clock)
    t.feed_proton_raw('0048:004c:warn:seh:OutputDebugStringA "Sys [Info]: You are offering: [a, b"...')
    assert merged == []
    t.feed("5.0 Sys [Info]: You are offering: [a, b, c]")
    assert merged == ["5.0 Sys [Info]: You are offering: [a, b, c]"]


def test_ignored_payloads_do_not_suppress_file_lines():
    clock = Clock()
    t, merged, _, _ = _tailer(clock)
    t.feed_realtime("Sys [Info]: Some unrelated engine noise")
    t.feed("1.0 Sys [Info]: Some unrelated engine noise")
    assert merged == ["1.0 Sys [Info]: Some unrelated engine noise"]


def test_handlers_see_source():
    clock = Clock()
    t = EELogTailer(clock=clock)
    seen = []
    t.subscribe(lambda line: seen.append(t.source))
    t.feed_realtime(REWARD)
    t.feed("1.0 other")
    assert seen == ["realtime", "file"]


def test_poll_loop_with_fixture_files(tmp_path):
    """Both files tailed by poll_once: each event reaches merged subscribers once."""
    clock = Clock()
    ee = tmp_path / "EE.log"
    proton = tmp_path / "steam-230410.log"
    ee.write_text("0.1 Sys [Info]: boot\n")
    proton.write_text("0.1:0048:004c:warn:seh:OutputDebugStringA \"boot\"\n")
    t = EELogTailer(lambda: str(ee), clock=clock, proton_path=lambda: str(proton))
    merged = []
    t.subscribe(merged.append)
    t.poll_once()  # attaches at the end of both
    clock.t += 1
    with open(proton, "a") as h:
        h.write(open(os.path.join(FIX, "proton_steam-230410.txt")).read())
    t.poll_once()
    clock.t += 15
    with open(ee, "a") as h:
        h.write(open(os.path.join(FIX, "EE_tail.txt")).read())
    t.poll_once()
    assert merged == [
        RELIC,
        REWARD,
        "1234.401 Sys [Info]: Some unrelated engine noise",
        "1261.003 Sys [Info]: You are offering: [Volt Prime Systems Blueprint, Mesa Prime Chassis Blueprint, "
        "Braton Prime Barrel, Akbronco Prime Link, Ash Prime Neuroptics Blueprint]",
    ]
    assert t.realtime_active()
    assert t.echoes_dropped == 3


# ---- discovery -------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    pw = pwd.struct_passwd(("deck", "x", 1000, 1000, "", str(home), "/bin/bash"))
    monkeypatch.setattr(eelog, "deck_user", lambda: pw)
    monkeypatch.setattr(protonlog, "deck_user", lambda: pw)
    monkeypatch.delenv("WFHELPER_EE_LOG", raising=False)
    monkeypatch.delenv("WFHELPER_PROTON_LOG", raising=False)
    eelog._reset_discovery()
    yield home
    eelog._reset_discovery()


def _mklog(prefix, user="steamuser", text="x"):
    d = prefix / "drive_c" / "users" / user / "AppData" / "Local" / "Warframe"
    d.mkdir(parents=True)
    (d / "EE.log").write_text(text)
    return d / "EE.log"


def test_find_ee_log_sd_card_library_and_newest_wins(fake_home, tmp_path):
    steam = fake_home / ".local" / "share" / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    sd = tmp_path / "mmcblk0p1"
    (sd / "steamapps" / "compatdata").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n "0"\n {\n  "path"  "%s"\n }\n "1"\n {\n  "path"  "%s"\n }\n}\n' % (steam, sd))
    old = _mklog(steam / "steamapps" / "compatdata" / "230410" / "pfx")
    new = _mklog(sd / "steamapps" / "compatdata" / "230410" / "pfx")
    os.utime(old, (1, 1))
    assert eelog.find_ee_log() == str(new)
    assert str(sd) in eelog.steam_libraries(str(fake_home))


def test_find_ee_log_non_steam_shortcut_and_override(fake_home, tmp_path):
    steam = fake_home / ".steam" / "steam"
    (steam / "steamapps" / "compatdata").mkdir(parents=True)
    log = _mklog(steam / "steamapps" / "compatdata" / "3141592653" / "pfx")
    assert eelog.find_ee_log() == str(log)
    other = tmp_path / "custom.log"
    other.write_text("")
    assert eelog.find_ee_log(str(other)) == str(other)


def test_find_ee_log_lutris_prefix(fake_home, tmp_path):
    prefix = fake_home / "Games" / "warframe"
    log = _mklog(prefix)
    cfg = fake_home / ".config" / "lutris" / "games"
    cfg.mkdir(parents=True)
    (cfg / "warframe.yml").write_text("game:\n  exe: x\n  prefix: '~/Games/warframe'\n")
    assert eelog.find_ee_log() == str(log)


def test_parse_library_paths():
    assert eelog.parse_steam_library_paths('"path"  "D:\\\\Games\\\\Steam"\n"path" "/run/media/deck/SD"') == \
        ["D:\\Games\\Steam", "/run/media/deck/SD"]


# ---- PROTON_LOG detection ---------------------------------------------------------------

def test_launch_options_and_env(fake_home):
    vdf = open(os.path.join(FIX, "localconfig.vdf")).read()
    assert protonlog.parse_launch_options(vdf) == "PROTON_LOG=1 %command% -cluster:public"
    assert protonlog.parse_launch_options(vdf, "1091500") == "-skipStartScreen"
    assert protonlog.launch_option_has_proton_log("PROTON_LOG=1 %command%")
    assert not protonlog.launch_option_has_proton_log("PROTON_LOG=0 %command%")
    assert not protonlog.launch_option_has_proton_log(None)
    cfg = fake_home / ".local" / "share" / "Steam" / "userdata" / "12345" / "config"
    cfg.mkdir(parents=True)
    (cfg / "localconfig.vdf").write_text(vdf)
    assert protonlog.steam_launch_options(str(fake_home)) == "PROTON_LOG=1 %command% -cluster:public"
    assert protonlog.proton_log_env_active({"PROTON_LOG": "1"}) is True
    assert protonlog.proton_log_env_active({}) is False
    assert protonlog.proton_log_env_active(None) is None
    assert protonlog.resolve_proton_log_path(env={}) == str(fake_home / "steam-230410.log")
    assert protonlog.resolve_proton_log_path(env={"PROTON_LOG_DIR": "/tmp/pl"}) == "/tmp/pl/steam-230410.log"


def test_status_reports_proton_state(fake_home):
    (fake_home / "steam-230410.log").write_text("")
    t = EELogTailer()
    st = t.status()
    assert st["proton_log"] == str(fake_home / "steam-230410.log")
    assert st["proton_log_exists"] is True
    assert st["realtime_active"] is False
    assert st["launch_option_hint"] == "PROTON_LOG=1 %command%"
    for key in ("ee_log", "lines_seen", "proton_log_env", "launch_option_set", "echoes_dropped"):
        assert key in st


# ---- EE.cfg ---------------------------------------------------------------------------

def test_ui_scale_parse():
    assert eecfg.parse_ui_scale("Flash.FlashDrawScaleMode=DSM_CUSTOM\nFlash.FlashDrawScale=0.75\n") == 0.75
    assert eecfg.parse_ui_scale("Flash.FlashDrawScaleMode=MSM_CUSTOM\nFlash.FlashDrawScale=0.3\n") == 0.5
    assert eecfg.parse_ui_scale("Flash.FlashDrawScaleMode=DSM_CUSTOM\nFlash.FlashDrawScale=1.4\n") == 1.0
    assert eecfg.parse_ui_scale("Flash.FlashDrawScaleMode=DSM_MATCH_SCREEN\nFlash.FlashDrawScale=0.75\n") is None
    assert eecfg.parse_ui_scale("Flash.FlashDrawScale=0.75\n") is None
    assert eecfg.parse_ui_scale("Flash.FlashDrawScaleMode=DSM_CUSTOM\nFlash.FlashDrawScale=0\n") is None
    assert eecfg.is_cfg_saved_line("12.3 Sys [Info]: Saved package: /Configs/EE.cfg")


def test_ui_scale_from_file():
    info = eecfg.ui_scale_info(os.path.join(FIX, "EE.cfg"))
    assert info["scale"] == 0.87  # last occurrence wins, rounded to 2 dp
    assert info["mode"] == "DSM_CUSTOM" and info["reason"] == "custom"
    assert eecfg.ui_scale("/nonexistent/EE.cfg") is None
