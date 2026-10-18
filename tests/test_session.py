import os

import pytest

from wfh import session


class Env:
    uid = 1000
    gid = 1000
    user = "deck"
    display = ":0"

    def x_env(self):
        return {"DISPLAY": ":0"}


def test_x_connect_retries_as_session_user(monkeypatch):
    calls, switched = [], []

    class AsUser:
        def __init__(self, uid, gid):
            switched.append((uid, gid))

        def __enter__(self):
            calls.append("enter")

        def __exit__(self, *a):
            calls.append("exit")

    monkeypatch.setattr(session.os, "geteuid", lambda: 0)
    monkeypatch.setattr(session, "thread_as_user", AsUser)

    def connect():
        if not calls:
            calls.append("root")
            raise ConnectionError("Authorization required")
        calls.append("user")
        return "display"

    assert session.x_connect(Env(), connect) == "display"
    assert calls == ["root", "enter", "user", "exit"]
    assert switched == [(1000, 1000)]


def test_x_connect_does_not_retry_when_not_root(monkeypatch):
    monkeypatch.setattr(session.os, "geteuid", lambda: 1000)

    def connect():
        raise ConnectionError("nope")

    with pytest.raises(ConnectionError):
        session.x_connect(Env(), connect)


def test_thread_as_user_is_noop_when_not_root():
    with session.thread_as_user(1000, 1000):
        assert os.geteuid() == os.geteuid()


def test_xwayland_auth_reads_cmdline(monkeypatch, tmp_path):
    cookie = tmp_path / "cookie"
    cookie.write_bytes(b"x")
    monkeypatch.setattr(session, "_pids", lambda: ["7"])
    monkeypatch.setattr(session, "_comm", lambda pid: "Xwayland")
    real_stat = os.stat
    monkeypatch.setattr(session.os, "stat", lambda p: real_stat(str(tmp_path)) if p == "/proc/7" else real_stat(p))
    real_open = open

    def fake_open(path, *a, **k):
        if path == "/proc/7/cmdline":
            import io
            return io.BytesIO(b"Xwayland\0:0\0-rootless\0-auth\0" + str(cookie).encode() + b"\0")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    assert session._xwayland_auth(":0", os.getuid()) == str(cookie)
    assert session._xwayland_auth(":1", os.getuid()) is None
