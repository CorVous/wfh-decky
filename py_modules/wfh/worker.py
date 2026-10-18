"""Worker entry point: `python -m wfh` with WFH_* env vars set by main.py."""

import asyncio
import os
import sys

from . import app as app_mod, log, rpc

_IOPRIO_SET_NR = {"x86_64": 251, "aarch64": 30}


def _lower_priority() -> list[str]:
    """Drop CPU and I/O priority below the game's. Linux applies nice, sched
    policy and ioprio per thread, but every thread created afterwards (ORT
    pools, refresh/scan threads, the asyncio executor) inherits them, so this
    must run before any thread starts. Each step is best effort."""
    done = []
    try:
        os.nice(10)
        done.append("nice10")
    except OSError:
        pass
    try:
        os.sched_setscheduler(0, os.SCHED_BATCH, os.sched_param(0))
        done.append("batch")
    except (OSError, AttributeError):
        pass
    try:
        import ctypes
        import platform

        nr = _IOPRIO_SET_NR.get(platform.machine())
        if nr is not None:
            libc = ctypes.CDLL(None, use_errno=True)
            # IOPRIO_WHO_PROCESS=1, who=0 (self); best-effort class (2) at the
            # lowest level (7). Not the idle class: it can starve model/data
            # loads indefinitely while the game streams assets.
            if libc.syscall(nr, 1, 0, (2 << 13) | 7) == 0:
                done.append("io-be7")
    except Exception:  # noqa: BLE001
        pass
    return done


def main() -> None:
    prio = _lower_priority()
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
    lg.info("worker up, python %s, %d methods, failed modules: %s, priority: %s",
            sys.version.split()[0], len(rpc.methods()), failed, ",".join(prio) or "unchanged")

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
