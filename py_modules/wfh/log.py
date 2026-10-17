"""Logging to a file in the plugin log dir (stdout is reserved for RPC)."""

import logging
import os
import sys

_configured = False


def setup(log_dir: str | None) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(log_dir, "worker.log"), encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("WFH_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"wfh.{name}")
