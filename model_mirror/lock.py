from __future__ import annotations

import errno
import fcntl
import os
import socket
from dataclasses import dataclass
from pathlib import Path

import yaml

from .state import utc_now


LOCK_FILE = ".verification.lock"


class ModelBusyError(RuntimeError):
    def __init__(self, root: Path, info: dict):
        self.root = root
        self.info = info
        detail = lock_label(info)
        super().__init__(f"model mirror is busy: {root} ({detail})")


@dataclass(slots=True)
class ModelLock:
    root: Path
    command: str
    repo_id: str
    repo_type: str = "model"
    wait: bool = False
    handle: object | None = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = lock_path(self.root)
        self.handle = path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX if self.wait else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self.handle.fileno(), flags)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            self.handle.seek(0)
            info = yaml.safe_load(self.handle.read()) or {}
            raise ModelBusyError(self.root, info) from exc

        info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "command": self.command,
            "repo_id": self.repo_id,
            "repo_type": self.repo_type,
            "started_at_utc": utc_now(),
        }
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(yaml.safe_dump(info, sort_keys=True))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.flush()
        os.fsync(self.handle.fileno())
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        return False


def lock_path(root: Path) -> Path:
    return root / LOCK_FILE


def read_active_lock(root: Path) -> dict | None:
    path = lock_path(root)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            handle.seek(0)
            return yaml.safe_load(handle.read()) or {}
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return None


def lock_label(info: dict | None) -> str:
    if not info:
        return "lock held"
    parts = []
    for key in ("command", "pid", "host", "started_at_utc"):
        value = info.get(key)
        if value:
            parts.append(f"{key}={value}")
    return ", ".join(parts) if parts else "lock held"
