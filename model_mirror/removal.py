from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .checksums import SKIP_DIRS, SKIP_FILES
from .config import Config, REPO_TYPE_DIRS, safe_repo_path
from .lock import LOCK_FILE, REMOVAL_MARKER
from .state import utc_now


REMOVALS_DIR = ".model-mirror-removals"
REMOVAL_RECORD = REMOVAL_MARKER
REMOVAL_SCHEMA = "model-mirror-removal"
REMOVAL_VERSION = 1


@dataclass(slots=True)
class RemovalRecord:
    repo_id: str
    repo_type: str
    original_path: str
    status: str
    exceptions: str
    resolved_commit: str
    checked_at_utc: str
    check_age: str
    payload_files: int
    payload_size: int
    started_at_utc: str = ""

    def to_dict(self) -> dict:
        return {
            "schema": REMOVAL_SCHEMA,
            "version": REMOVAL_VERSION,
            **asdict(self),
            "started_at_utc": self.started_at_utc or utc_now(),
        }

    @classmethod
    def from_dict(cls, value: object, *, source: Path) -> RemovalRecord:
        if (
            not isinstance(value, dict)
            or value.get("schema") != REMOVAL_SCHEMA
            or value.get("version") != REMOVAL_VERSION
        ):
            raise ValueError(f"Unsupported removal record: {source}")
        try:
            return cls(
                repo_id=str(value["repo_id"]),
                repo_type=str(value["repo_type"]),
                original_path=str(value["original_path"]),
                status=str(value["status"]),
                exceptions=str(value["exceptions"]),
                resolved_commit=str(value["resolved_commit"]),
                checked_at_utc=str(value["checked_at_utc"]),
                check_age=str(value["check_age"]),
                payload_files=int(value["payload_files"]),
                payload_size=int(value["payload_size"]),
                started_at_utc=str(value["started_at_utc"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed removal record: {source}") from exc


def removal_path(config: Config, repo_id: str, repo_type: str) -> Path:
    return (
        Path(config.directory)
        / REMOVALS_DIR
        / REPO_TYPE_DIRS[repo_type]
        / safe_repo_path(repo_id)
    )


def removal_record_path(root: Path) -> Path:
    return root / REMOVAL_RECORD


def stage_removal(root: Path, target: Path, record: RemovalRecord) -> Path:
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"interrupted removal already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    write_removal_record(root, record)
    root.rename(target)
    return target


def write_removal_record(root: Path, record: RemovalRecord) -> Path:
    path = removal_record_path(root)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(record.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_removal_record(root: Path) -> RemovalRecord | None:
    path = removal_record_path(root)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed removal record: {path}") from exc
    return RemovalRecord.from_dict(value, source=path)


def complete_removal(root: Path) -> None:
    paths, directories = removable_paths(root)
    payload = [path for path in paths if is_payload_path(root, path)]
    metadata = [
        path
        for path in paths
        if path not in payload and path.name not in {REMOVAL_RECORD, LOCK_FILE}
    ]
    for path in payload:
        unlink_removal_path(path)
    for path in metadata:
        unlink_removal_path(path)
    for path in directories:
        path.rmdir()
    removal_record_path(root).unlink(missing_ok=True)
    (root / LOCK_FILE).unlink(missing_ok=True)
    root.rmdir()


def removable_paths(root: Path) -> tuple[list[Path], list[Path]]:
    paths: list[Path] = []
    directories: list[Path] = []
    for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in filenames:
            paths.append(current_path / name)
        for name in dirnames:
            path = current_path / name
            if path.is_symlink():
                paths.append(path)
            else:
                directories.append(path)
    paths.sort(key=lambda path: path.relative_to(root).as_posix())
    directories.sort(key=lambda path: len(path.relative_to(root).parts), reverse=True)
    return paths, directories


def is_payload_path(root: Path, path: Path) -> bool:
    rel = path.relative_to(root)
    if rel.parts and rel.parts[0] in SKIP_DIRS:
        return False
    return rel.as_posix() not in SKIP_FILES and path.name != REMOVAL_RECORD


def unlink_removal_path(path: Path) -> None:
    path.unlink()


def prune_empty_removal_parents(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
