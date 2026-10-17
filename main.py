"""Decky backend for WFHelper.

Decky's embedded Python cannot load numpy/onnxruntime, so everything runs in a
worker process on the bundled CPython in bin/python (see py_modules/wfh).
This module only starts that worker and relays calls and events:
the frontend calls `rpc(method, params)`, the worker's events are re-emitted
with decky.emit("wfh_event", name, data).
"""

import asyncio
import json
import os

import decky

PLUGIN_DIR = os.path.dirname(os.path.realpath(__file__))
PYTHON = os.path.join(PLUGIN_DIR, "bin", "python", "bin", "python3")
RPC_TIMEOUT = 60


class Plugin:
    proc: asyncio.subprocess.Process | None = None
    pending: dict[int, asyncio.Future] = {}
    next_id = 1
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    stopping = False
    restarts = 0

    async def _spawn(self) -> None:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": decky.HOME,
            "LANG": "C.UTF-8",
            "PYTHONPATH": os.path.join(PLUGIN_DIR, "py_modules"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "WFH_PLUGIN_DIR": PLUGIN_DIR,
            "WFH_SETTINGS_DIR": decky.DECKY_PLUGIN_SETTINGS_DIR,
            "WFH_RUNTIME_DIR": decky.DECKY_PLUGIN_RUNTIME_DIR,
            "WFH_LOG_DIR": decky.DECKY_PLUGIN_LOG_DIR,
            "DECKY_USER": decky.DECKY_USER,
            "DECKY_USER_HOME": decky.DECKY_USER_HOME,
        }
        decky.logger.info("starting worker: %s", PYTHON)
        self.proc = await asyncio.create_subprocess_exec(
            PYTHON, "-s", "-m", "wfh",
            cwd=PLUGIN_DIR, env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024,
        )
        self.reader_task = asyncio.create_task(self._read_stdout(self.proc))
        self.stderr_task = asyncio.create_task(self._read_stderr(self.proc))

    async def _read_stdout(self, proc) -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                decky.logger.warning("worker: %s", line[:300])
                continue
            if "event" in msg:
                await decky.emit("wfh_event", msg["event"], msg.get("data"))
                continue
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("worker exited"))
        self.pending.clear()

    async def _read_stderr(self, proc) -> None:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            decky.logger.info("worker| %s", line.decode(errors="replace").rstrip())

    async def _watchdog(self) -> None:
        while not self.stopping:
            await asyncio.sleep(2)
            if self.proc is not None and self.proc.returncode is not None and not self.stopping:
                self.restarts += 1
                delay = min(60, 2 ** min(self.restarts, 6))
                decky.logger.error("worker exited with %s, restarting in %ss", self.proc.returncode, delay)
                await asyncio.sleep(delay)
                await self._spawn()

    async def rpc(self, method: str, params: dict | None = None):
        """Called from the frontend: forward one call to the worker."""
        if self.proc is None or self.proc.returncode is not None:
            raise RuntimeError("WFHelper worker is not running")
        rid = self.next_id
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self.proc.stdin.write((json.dumps({"id": rid, "method": method, "params": params or {}}) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(fut, RPC_TIMEOUT)
        finally:
            self.pending.pop(rid, None)

    async def worker_alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _main(self):
        self.stopping = False
        if not os.path.exists(PYTHON):
            decky.logger.error("bundled runtime missing at %s", PYTHON)
            return
        await self._spawn()
        self.watchdog_task = asyncio.create_task(self._watchdog())

    async def _unload(self):
        self.stopping = True
        if self.watchdog_task:
            self.watchdog_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, OSError):
                self.proc.kill()
        decky.logger.info("WFHelper unloaded")

    async def _uninstall(self):
        pass
