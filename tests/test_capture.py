import subprocess
import time

from wfh import capture



def test_gamescopectl_nonzero_exit_returns_immediately(monkeypatch, tmp_path):
    class Env:
        wayland_display = "gamescope-1"
        display = ":1"

        def x_env(self):
            return {"DISPLAY": ":1"}

    seen = {}

    def fake_run(args, env, **kw):
        seen["env"] = env
        return subprocess.CompletedProcess(args, 1, b"", b"Failed to get Gamescope interfaces\n")

    monkeypatch.setattr(capture.shutil, "which", lambda _: "/usr/bin/gamescopectl")
    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    t0 = time.monotonic()
    assert capture._gamescopectl(Env(), str(tmp_path / "x.png"), 2.0) is False
    assert time.monotonic() - t0 < 0.5
    assert seen["env"]["GAMESCOPE_WAYLAND_DISPLAY"] == "gamescope-1"
