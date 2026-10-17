import asyncio

from wfh import rpc


def test_modules_load(app):
    assert app.services["failed_modules"] == []


def test_overlay_test_card(app):
    status = asyncio.run(rpc.call_local("overlay.test", {"seconds": 1}))
    assert "test" in status["layers"]
    assert asyncio.run(rpc.call_local("overlay.preview")).startswith("data:image/png;base64,")


def test_settings_roundtrip(app):
    out = asyncio.run(rpc.call_local("settings.update", {"patch": {"relic_rewards": {"duration_s": 5}}}))
    assert out["relic_rewards"]["duration_s"] == 5
    assert out["relic_rewards"]["enabled"] is True
