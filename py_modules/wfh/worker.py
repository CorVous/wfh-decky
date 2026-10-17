"""Worker entry point: `python -m wfh` with WFH_* env vars set by main.py."""

import asyncio
import os
import sys

from . import app as app_mod, log, rpc


def main() -> None:
    plugin_dir = os.environ.get("WFH_PLUGIN_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    settings_dir = os.environ.get("WFH_SETTINGS_DIR", "/tmp/wfh-decky/settings")
    runtime_dir = os.environ.get("WFH_RUNTIME_DIR", "/tmp/wfh-decky/runtime")
    log_dir = os.environ.get("WFH_LOG_DIR", "/tmp/wfh-decky/log")
    for d in (settings_dir, runtime_dir, log_dir):
        os.makedirs(d, exist_ok=True)
    log.setup(log_dir)
    lg = log.get("worker")
    headless = os.environ.get("WFH_HEADLESS") == "1"
    app = app_mod.create(plugin_dir, settings_dir, runtime_dir, log_dir, headless=headless)
    failed = app_mod.load_modules(app)
    lg.info("worker up, python %s, %d methods, failed modules: %s", sys.version.split()[0], len(rpc.methods()), failed)

    async def run():
        app.loop = asyncio.get_running_loop()
        app.services["failed_modules"] = failed
        for start in app.services.get("on_start", []):
            try:
                start()
            except Exception:  # noqa: BLE001
                lg.exception("start hook failed")
        app.eelog.start()
        rpc.emit("worker.ready", {"methods": rpc.methods(), "failed": failed})

        async def stop():
            app.eelog.stop()
            app.overlays.stop()

        await rpc.serve(stop)

    asyncio.run(run())
