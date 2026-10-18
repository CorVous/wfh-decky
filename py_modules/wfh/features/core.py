"""Plugin-wide RPCs: status, settings, overlay test and preview."""

import time

from .. import __version__, capture, rpc, session
from ..app import App, get_app
from ..overlay import draw


def setup(app: App) -> None:
    def apply(data: dict) -> None:
        app.overlays.opacity = float(data.get("overlay_opacity", 1.0))
        app.overlays.set_enabled(bool(data.get("overlays_enabled", True)))

    app.settings.on_change(apply)


@rpc.method("core.status")
def status() -> dict:
    app = get_app()
    env = session.find_session_env()
    return {
        "version": __version__,
        "warframe_running": session.warframe_running(),
        "ee_log": app.eelog.path,
        "ee_log_lines": app.eelog.lines_seen,
        "display": env.display if env else None,
        "overlay": app.overlays.status(),
        "failed_modules": app.services.get("failed_modules", []),
    }


@rpc.method("settings.get")
def settings_get() -> dict:
    return get_app().settings.all()


@rpc.method("settings.update")
def settings_update(patch: dict) -> dict:
    return get_app().settings.update(patch)


@rpc.method("overlay.preview")
def overlay_preview() -> str:
    return get_app().overlays.snapshot_png()


@rpc.method("overlay.clear")
def overlay_clear() -> None:
    get_app().overlays.clear_all()


@rpc.method("overlay.test")
def overlay_test(seconds: float = 6) -> dict:
    """Draws a sample card so the user can check overlays reach the screen."""
    app = get_app()
    card = draw.Card(320, title="WFHelper overlay test")
    card.add(draw.Cell("If you can read this over the game,", color=draw.TEXT))
    card.add(draw.Cell("overlays are working.", color=draw.TEXT))
    card.add(draw.Cell(time.strftime("%H:%M:%S"), color=draw.MUTED), draw.Cell("OK", color=draw.GOOD, align="right", bold=True))
    img = card.render()
    w, _ = app.overlays.size
    app.overlays.show("test", img, (w - img.width) // 2, 40, ttl=seconds, z=100)
    return app.overlays.status()


@rpc.method("capture.test")
async def capture_test() -> dict:
    """Takes one gamescope screenshot and reports its size and timing."""
    app = get_app()
    t0 = time.monotonic()
    img = await app.run_blocking(capture.grab)
    return {"width": img.width, "height": img.height, "ms": round((time.monotonic() - t0) * 1000)}
