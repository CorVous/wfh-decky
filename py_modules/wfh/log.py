"""Logging to a file in the plugin log dir (stdout is reserved for RPC).

The worker runs as root, but Decky creates the plugin log dir as the deck user
(mkdir_as_user), so deck controls its entries. A plain open("worker.log", "a")
would follow a symlink planted there (e.g. to /etc/ld.so.preload) and append
partly deck-steerable text to it as root. _open_log therefore opens the file
relative to a directory fd, with O_NOFOLLOW, and only accepts a regular,
single-link file owned by this process's euid; anything else is unlinked
(unlink never follows) and recreated with O_EXCL.
"""

import logging
import os
import stat
import sys

_configured = False
_LOG_NAME = "worker.log"
_MODE = 0o644  # Decky's own plugin logs are world-readable; nothing secret is logged


def _ok(fd: int) -> bool:
    st = os.fstat(fd)
    return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1


def _open_log(log_dir: str):
    """Return a text stream appending to log_dir/worker.log, or None if unsafe."""
    base = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    dfd = os.open(log_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = -1
        try:
            fd = os.open(_LOG_NAME, base, _MODE, dir_fd=dfd)
        except OSError:
            fd = -1  # ELOOP (symlink), EISDIR, ... -> replace below
        if fd >= 0 and not _ok(fd):
            os.close(fd)
            fd = -1
        if fd < 0:
            try:
                os.unlink(_LOG_NAME, dir_fd=dfd)
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                return None
            fd = os.open(_LOG_NAME, base | os.O_EXCL, _MODE, dir_fd=dfd)
            if not _ok(fd):
                os.close(fd)
                return None
        try:
            os.fchmod(fd, _MODE)
        except OSError:
            pass
        return os.fdopen(fd, "a", encoding="utf-8", buffering=1)
    finally:
        os.close(dfd)


def setup(log_dir: str | None) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    problem = None
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            stream = _open_log(log_dir)
        except OSError as e:
            stream, problem = None, f"{e}"
        if stream is not None:
            handlers.append(logging.StreamHandler(stream))
        elif problem is None:
            problem = "refusing unsafe log file"
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("WFH_DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if problem:
        logging.getLogger("wfh.log").warning("file logging disabled in %s: %s", log_dir, problem)


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"wfh.{name}")
