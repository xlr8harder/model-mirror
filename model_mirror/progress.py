from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROGRESS_DIR = ".model-mirror"
PROGRESS_FILE = "progress.json"
PROGRESS_SCHEMA = "model-mirror-progress"
PROGRESS_VERSION = 1
DEFAULT_STALL_TIMEOUT_SECONDS = 600
DEFAULT_STALL_RETRIES = 3
SKIP_DIRS = {".model-mirror", ".cache", ".archive"}


@dataclass(frozen=True, slots=True)
class ProgressEntry:
    path: str
    stage: str
    bytes_done: int
    bytes_total: int | None
    updated_at_utc: str
    idle_seconds: int | None
    stalled: bool
    source: str
    rate_bytes_per_second: float | None = None


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    entries: list[ProgressEntry]
    source: str

    @property
    def active(self) -> bool:
        return bool(self.entries)

    @property
    def stalled_count(self) -> int:
        return sum(1 for entry in self.entries if entry.stalled)

    @property
    def any_stalled(self) -> bool:
        return self.stalled_count > 0


class ProgressRecorder:
    def __init__(
        self,
        root: Path,
        *,
        min_interval_seconds: float = 5.0,
        min_bytes: int = 64 * 1024 * 1024,
    ):
        self.root = root
        self.min_interval_seconds = min_interval_seconds
        self.min_bytes = min_bytes
        self._active: dict[str, dict] = {}
        self._lock = threading.Lock()

    def track(self, path: str, *, total: int | None, stage: str, bytes_done: int = 0) -> FileProgress:
        progress = FileProgress(self, path, total=total)
        progress.update(bytes_done, stage=stage, force=True)
        return progress

    def update(
        self,
        path: str,
        *,
        total: int | None,
        stage: str,
        bytes_done: int,
        rate_bytes_per_second: float | None,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            entry = self._active.get(path)
            if entry is None:
                entry = {
                    "path": path,
                    "bytes_total": total,
                    "started_at_utc": now,
                }
            entry.update(
                {
                    "stage": stage,
                    "bytes_done": bytes_done,
                    "bytes_total": total,
                    "updated_at_utc": now,
                    "rate_bytes_per_second": rate_bytes_per_second,
                }
            )
            self._active[path] = entry
            self._write_locked()

    def finish(self, path: str) -> None:
        with self._lock:
            self._active.pop(path, None)
            if self._active:
                self._write_locked()
            else:
                progress_path(self.root).unlink(missing_ok=True)

    def _write_locked(self) -> None:
        path = progress_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": PROGRESS_SCHEMA,
            "version": PROGRESS_VERSION,
            "pid": os.getpid(),
            "updated_at_utc": utc_now_iso(),
            "active_files": self._active,
        }
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


class FileProgress:
    def __init__(self, recorder: ProgressRecorder, path: str, *, total: int | None):
        self.recorder = recorder
        self.path = path
        self.total = total
        self.stage = "starting"
        self.last_emit_time: float | None = None
        self.last_emit_bytes: int | None = None

    def update(self, bytes_done: int, *, stage: str | None = None, force: bool = False) -> None:
        now = time.monotonic()
        selected_stage = stage or self.stage
        should_emit = force or self.last_emit_time is None
        if not should_emit and now - self.last_emit_time >= self.recorder.min_interval_seconds:
            should_emit = True
        if (
            not should_emit
            and self.last_emit_bytes is not None
            and bytes_done - self.last_emit_bytes >= self.recorder.min_bytes
        ):
            should_emit = True
        if not should_emit:
            self.stage = selected_stage
            return

        rate = None
        if self.last_emit_time is not None and self.last_emit_bytes is not None:
            elapsed = now - self.last_emit_time
            if elapsed > 0:
                rate = max(0, bytes_done - self.last_emit_bytes) / elapsed
        self.last_emit_time = now
        self.last_emit_bytes = bytes_done
        self.stage = selected_stage
        self.recorder.update(
            self.path,
            total=self.total,
            stage=selected_stage,
            bytes_done=bytes_done,
            rate_bytes_per_second=rate,
        )

    def finish(self) -> None:
        self.recorder.finish(self.path)


def progress_path(root: Path) -> Path:
    return root / PROGRESS_DIR / PROGRESS_FILE


def progress_snapshot(
    root: Path,
    *,
    stall_timeout_seconds: int = DEFAULT_STALL_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> ProgressSnapshot:
    now = now or datetime.now(timezone.utc)
    entries = progress_file_entries(root, stall_timeout_seconds=stall_timeout_seconds, now=now)
    if entries:
        return ProgressSnapshot(entries=entries, source="heartbeat")
    return ProgressSnapshot(
        entries=incomplete_file_entries(root, stall_timeout_seconds=stall_timeout_seconds, now=now),
        source="partial-file",
    )


def progress_file_entries(root: Path, *, stall_timeout_seconds: int, now: datetime) -> list[ProgressEntry]:
    path = progress_path(root)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != PROGRESS_SCHEMA or data.get("version") != PROGRESS_VERSION:
        return []
    entries = []
    for rel, item in sorted((data.get("active_files") or {}).items()):
        updated_at = str(item.get("updated_at_utc") or "")
        idle = idle_seconds(updated_at, now)
        entries.append(
            ProgressEntry(
                path=str(item.get("path") or rel),
                stage=str(item.get("stage") or "unknown"),
                bytes_done=int(item.get("bytes_done") or 0),
                bytes_total=item.get("bytes_total"),
                updated_at_utc=updated_at,
                idle_seconds=idle,
                stalled=is_stalled(idle, stall_timeout_seconds),
                source="heartbeat",
                rate_bytes_per_second=item.get("rate_bytes_per_second"),
            )
        )
    return entries


def incomplete_file_entries(root: Path, *, stall_timeout_seconds: int, now: datetime) -> list[ProgressEntry]:
    entries = []
    for path in sorted(root.rglob("*.incomplete")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        idle = idle_seconds(updated, now)
        rel_path = rel.as_posix()[: -len(".incomplete")]
        entries.append(
            ProgressEntry(
                path=rel_path,
                stage="partial",
                bytes_done=path.stat().st_size,
                bytes_total=None,
                updated_at_utc=updated,
                idle_seconds=idle,
                stalled=is_stalled(idle, stall_timeout_seconds),
                source="partial-file",
            )
        )
    return entries


def idle_seconds(updated_at_utc: str, now: datetime) -> int | None:
    try:
        updated = datetime.fromisoformat(updated_at_utc)
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return max(0, int((now - updated).total_seconds()))


def is_stalled(idle: int | None, stall_timeout_seconds: int) -> bool:
    return stall_timeout_seconds > 0 and idle is not None and idle >= stall_timeout_seconds


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
