from __future__ import annotations

import errno
import fcntl
import json
import os
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import (
    Config,
    REPO_TYPE_DIRS,
    archive_control_path,
    archive_path,
    archive_runtime_cache_path,
    archive_runtime_tmp_path,
)
from .lock import read_active_lock
from .state import read_verification_state, utc_now


DOWNLOAD_RECORD_FILE = ".model-mirror-download.json"
DOWNLOAD_RECORD_SCHEMA = "model-mirror-download"
DOWNLOAD_RECORD_VERSION = 1
RUNTIME_CACHE_LOCK_FILE = "runtime-cache.lock"


@dataclass(frozen=True, slots=True)
class DownloadRecord:
    repo_id: str
    repo_type: str
    requested_revision: str
    resolved_commit: str
    destination: str
    allow_patterns: list[str] | None
    created_at_utc: str
    last_started_at_utc: str
    schema: str = DOWNLOAD_RECORD_SCHEMA
    version: int = DOWNLOAD_RECORD_VERSION


@dataclass(frozen=True, slots=True)
class CacheIssue:
    kind: str
    reason: str
    label: str
    path: Path
    repo_id: str | None = None
    repo_type: str | None = None
    resolved_commit: str | None = None
    actions: tuple[str, ...] = ()

    @property
    def tag(self) -> str:
        return "untracked-cache" if self.kind == "untracked" else "stale-cache"


class RuntimeCacheBusyError(RuntimeError):
    pass


class RuntimeCacheLock:
    def __init__(self, config: Config, *, exclusive: bool):
        self.path = archive_control_path(config) / RUNTIME_CACHE_LOCK_FILE
        self.exclusive = exclusive
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | fcntl.LOCK_NB if self.exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(self.handle.fileno(), flags)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeCacheBusyError("runtime cache is in use by an active operation") from exc
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return False
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None
        return False


def write_download_record(
    staging_dir: Path,
    *,
    repo_id: str,
    repo_type: str,
    requested_revision: str,
    resolved_commit: str,
    destination: Path,
    allow_patterns: list[str] | None,
) -> DownloadRecord:
    now = utc_now()
    existing = None
    try:
        existing = read_download_record(staging_dir)
    except (OSError, ValueError):
        pass
    same_operation = bool(
        existing is not None
        and existing.repo_id == repo_id
        and existing.repo_type == repo_type
        and existing.resolved_commit == resolved_commit
        and existing.destination == str(destination)
    )
    record = DownloadRecord(
        repo_id=repo_id,
        repo_type=repo_type,
        requested_revision=requested_revision,
        resolved_commit=resolved_commit,
        destination=str(destination),
        allow_patterns=sorted(allow_patterns) if allow_patterns is not None else None,
        created_at_utc=existing.created_at_utc if same_operation else now,
        last_started_at_utc=now,
    )
    path = download_record_path(staging_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return record


def read_download_record(staging_dir: Path) -> DownloadRecord | None:
    path = download_record_path(staging_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"download record must contain a JSON object: {path}")
    if data.get("schema") != DOWNLOAD_RECORD_SCHEMA or data.get("version") != DOWNLOAD_RECORD_VERSION:
        raise ValueError(f"unsupported download record schema: {path}")
    required = (
        "repo_id",
        "repo_type",
        "requested_revision",
        "resolved_commit",
        "destination",
        "created_at_utc",
        "last_started_at_utc",
    )
    if any(not isinstance(data.get(key), str) or not data[key] for key in required):
        raise ValueError(f"download record has missing or invalid fields: {path}")
    allow_patterns = data.get("allow_patterns")
    if allow_patterns is not None and (
        not isinstance(allow_patterns, list) or not all(isinstance(item, str) for item in allow_patterns)
    ):
        raise ValueError(f"download record has invalid allow_patterns: {path}")
    return DownloadRecord(
        repo_id=data["repo_id"],
        repo_type=data["repo_type"],
        requested_revision=data["requested_revision"],
        resolved_commit=data["resolved_commit"],
        destination=data["destination"],
        allow_patterns=allow_patterns,
        created_at_utc=data["created_at_utc"],
        last_started_at_utc=data["last_started_at_utc"],
    )


def download_record_path(staging_dir: Path) -> Path:
    return staging_dir / DOWNLOAD_RECORD_FILE


def inspect_runtime_cache(config: Config) -> list[CacheIssue]:
    archive_root = Path(config.directory)
    active_locks = active_repository_locks(config)
    issues: list[CacheIssue] = []

    cache_root = archive_runtime_cache_path(config)
    if path_has_entries(cache_root) and not active_locks:
        issues.append(
            untracked_issue(
                cache_root,
                "archive-cache",
                "runtime-cache-without-active-operation",
            )
        )

    tmp_root = archive_runtime_tmp_path(config)
    downloads_root = tmp_root / "downloads"
    if downloads_root.is_dir() and not downloads_root.is_symlink():
        for staging_dir in sorted(downloads_root.iterdir()):
            issues.extend(inspect_staging_dir(config, staging_dir, active_locks))
    elif downloads_root.exists() or downloads_root.is_symlink():
        issues.append(untracked_issue(downloads_root, "download-staging", "invalid-downloads-root"))

    if tmp_root.is_dir() and not tmp_root.is_symlink():
        for child in sorted(tmp_root.iterdir()):
            if child == downloads_root:
                continue
            issues.append(untracked_issue(child, "archive-tmp", "untracked-temporary-data"))
    elif tmp_root.exists() or tmp_root.is_symlink():
        issues.append(untracked_issue(tmp_root, "archive-tmp", "invalid-temporary-root"))

    selected = {
        cache_root.resolve(strict=False),
        tmp_root.resolve(strict=False),
    }
    for path, label in (
        (archive_root / ".cache", "legacy-archive-cache"),
        (archive_root / ".tmp", "legacy-archive-tmp"),
    ):
        if path.resolve(strict=False) not in selected and path_has_entries(path):
            issues.append(
                CacheIssue(
                    kind="stale",
                    reason="legacy-cache-layout",
                    label=label,
                    path=path,
                    actions=cleanup_actions(),
                )
            )

    for repo_type, type_dir in REPO_TYPE_DIRS.items():
        repo_root = archive_root / type_dir
        if not repo_root.is_dir():
            continue
        for owner in sorted(path for path in repo_root.iterdir() if path.is_dir()):
            for repo in sorted(path for path in owner.iterdir() if path.is_dir()):
                cache = repo / ".cache"
                repo_id = f"{owner.name}/{repo.name}"
                if path_has_entries(cache) and (repo_type, repo_id) not in active_locks:
                    issues.append(
                        CacheIssue(
                            kind="stale",
                            reason="legacy-mirror-cache",
                            label="mirror-cache",
                            path=cache,
                            repo_id=repo_id,
                            repo_type=repo_type,
                            actions=cleanup_actions(),
                        )
                    )
    return issues


def inspect_staging_dir(
    config: Config,
    staging_dir: Path,
    active_locks: dict[tuple[str, str], dict],
) -> list[CacheIssue]:
    if staging_dir.is_symlink() or not staging_dir.is_dir():
        return [untracked_issue(staging_dir, "download-staging", "invalid-staging-entry")]
    try:
        record = read_download_record(staging_dir)
    except (OSError, ValueError):
        return [untracked_issue(staging_dir, "download-staging", "unreadable-download-record")]
    if record is None:
        return [untracked_issue(staging_dir, "download-staging", "missing-download-record")]
    if record.repo_type not in REPO_TYPE_DIRS:
        return [untracked_issue(staging_dir, "download-staging", "unknown-repository-type")]
    if (record.repo_type, record.repo_id) in active_locks:
        return []

    root = archive_path(config, record.repo_id, record.repo_type)
    expected_destination = root.resolve(strict=False)
    try:
        recorded_destination = Path(record.destination).resolve(strict=False)
    except OSError:
        recorded_destination = Path(record.destination)
    if recorded_destination != expected_destination:
        return [untracked_issue(staging_dir, "download-staging", "destination-mismatch")]

    try:
        state = read_verification_state(root)
    except (OSError, ValueError):
        state = None
    completed = bool(
        state is not None
        and state.clean
        and state.resolved_commit == record.resolved_commit
    )
    if completed:
        reason = "completed-download-residue"
        actions = cleanup_actions()
    elif root.is_dir():
        reason = "interrupted-download"
        actions = (resume_command(record), *cleanup_actions())
    else:
        reason = "orphaned-download"
        actions = cleanup_actions()
    return [
        CacheIssue(
            kind="stale",
            reason=reason,
            label="download-staging",
            path=staging_dir,
            repo_id=record.repo_id,
            repo_type=record.repo_type,
            resolved_commit=record.resolved_commit,
            actions=actions,
        )
    ]


def active_repository_locks(config: Config) -> dict[tuple[str, str], dict]:
    archive_root = Path(config.directory)
    active: dict[tuple[str, str], dict] = {}
    for repo_type, type_dir in REPO_TYPE_DIRS.items():
        repo_root = archive_root / type_dir
        if not repo_root.is_dir():
            continue
        for owner in sorted(path for path in repo_root.iterdir() if path.is_dir()):
            for repo in sorted(path for path in owner.iterdir() if path.is_dir()):
                info = read_active_lock(repo)
                if info is not None:
                    active[(repo_type, f"{owner.name}/{repo.name}")] = info
    return active


def untracked_issue(path: Path, label: str, reason: str) -> CacheIssue:
    return CacheIssue(
        kind="untracked",
        reason=reason,
        label=label,
        path=path,
        actions=cleanup_actions(),
    )


def cleanup_actions() -> tuple[str, ...]:
    return (
        "inspect: model-mirror clean-cache",
        "remove: model-mirror clean-cache --force",
    )


def resume_command(record: DownloadRecord) -> str:
    repo = shlex.quote(record.repo_id)
    repo_type = shlex.quote(record.repo_type)
    revision = shlex.quote(record.requested_revision)
    return f"resume: model-mirror mirror --repo-type {repo_type} --revision {revision} {repo}"


def path_has_entries(path: Path) -> bool:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        return True
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True
