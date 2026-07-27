from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .checksums import FileHashes, file_hashes
from .torrent import PUBLICATION_PROFILE, SAFE_COMMIT, TorrentPublicationError, select_piece_length
from .torrent_hashes import TorrentFileHasher, TorrentFileHashes
from .verify import metadata_blob_id, metadata_lfs_sha256, metadata_path, metadata_size


COVERAGE_SCHEMA = "model-mirror-torrent-coverage"
COVERAGE_VERSION = 1
COVERAGE_DIR = Path(".model-mirror") / "torrent" / "coverage"


@dataclass(frozen=True, slots=True)
class TorrentCoverageFile:
    path: str
    size: int
    mtime_ns: int
    sha256: str
    git_blob_sha1: str
    lfs_sha256: str | None
    blob_id: str | None
    v1_piece_hashes: tuple[str, ...]
    v2_piece_hashes: tuple[str, ...]
    v2_file_root: str | None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "git_blob_sha1": self.git_blob_sha1,
            "lfs_sha256": self.lfs_sha256,
            "blob_id": self.blob_id,
            "v1_piece_hashes": list(self.v1_piece_hashes),
            "v2_piece_hashes": list(self.v2_piece_hashes),
            "v2_file_root": self.v2_file_root,
        }

    @classmethod
    def from_dict(cls, value: object, *, source: Path) -> TorrentCoverageFile:
        if not isinstance(value, dict):
            raise ValueError(f"Malformed torrent coverage file row in {source}")
        try:
            path = str(value["path"])
            size = int(value["size"])
            mtime_ns = int(value["mtime_ns"])
            sha256 = coverage_hex(value["sha256"], 64, "SHA-256", source)
            git_blob_sha1 = coverage_hex(value["git_blob_sha1"], 40, "Git blob SHA-1", source)
            lfs_sha256 = optional_coverage_hex(value.get("lfs_sha256"), 64, "LFS SHA-256", source)
            blob_id = optional_coverage_hex(value.get("blob_id"), 40, "blob ID", source)
            v1_piece_hashes = tuple(
                coverage_hex(item, 40, "v1 piece hash", source)
                for item in value["v1_piece_hashes"]
            )
            v2_piece_hashes = tuple(
                coverage_hex(item, 64, "v2 piece hash", source)
                for item in value["v2_piece_hashes"]
            )
            v2_file_root = optional_coverage_hex(
                value.get("v2_file_root"),
                64,
                "v2 file root",
                source,
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("Invalid "):
                raise
            raise ValueError(f"Malformed torrent coverage file row in {source}") from exc
        if not path or size < 0 or mtime_ns < 0:
            raise ValueError(f"Malformed torrent coverage file row in {source}")
        return cls(
            path=path,
            size=size,
            mtime_ns=mtime_ns,
            sha256=sha256,
            git_blob_sha1=git_blob_sha1,
            lfs_sha256=lfs_sha256,
            blob_id=blob_id,
            v1_piece_hashes=v1_piece_hashes,
            v2_piece_hashes=v2_piece_hashes,
            v2_file_root=v2_file_root,
        )


@dataclass(slots=True)
class TorrentCoverage:
    repo_id: str
    repo_type: str
    resolved_commit: str
    piece_length: int
    files: dict[str, TorrentCoverageFile] = field(default_factory=dict)
    profile: str = PUBLICATION_PROFILE

    def to_dict(self) -> dict:
        return {
            "schema": COVERAGE_SCHEMA,
            "version": COVERAGE_VERSION,
            "profile": self.profile,
            "repo_id": self.repo_id,
            "repo_type": self.repo_type,
            "resolved_commit": self.resolved_commit,
            "piece_length": self.piece_length,
            "files": {path: self.files[path].to_dict() for path in sorted(self.files)},
        }


@dataclass(frozen=True, slots=True)
class CoverageUpgradeResult:
    path: Path
    total_files: int
    covered_files: int
    hashed_files: int
    hashed_bytes: int
    complete: bool
    dry_run: bool = False


class TorrentCoverageRecorder:
    def __init__(self, root: Path, snapshot, *, profile: str = PUBLICATION_PROFILE):
        self.root = root
        self.snapshot = snapshot
        self.profile = profile
        self.expected = snapshot_files(snapshot)
        total_size = sum(require_metadata_size(item) for item in self.expected.values())
        if not self.expected or total_size == 0:
            raise TorrentPublicationError("torrent coverage requires at least one non-empty payload file")
        self.piece_length = select_piece_length(total_size)
        self.path = coverage_path(root, snapshot.resolved_commit, profile)
        self.coverage = load_coverage(self.path)
        if self.coverage is None:
            self.coverage = TorrentCoverage(
                repo_id=snapshot.repo_id,
                repo_type=snapshot.repo_type,
                resolved_commit=snapshot.resolved_commit,
                piece_length=self.piece_length,
                profile=profile,
            )
        self._validate_identity()
        self._lock = threading.Lock()

    def accumulator(self, item) -> TorrentFileHasher:
        return TorrentFileHasher(
            expected_size=require_metadata_size(item),
            piece_length=self.piece_length,
            pad_v1_tail=len(self.expected) > 1,
        )

    def record(
        self,
        item,
        path: Path,
        full_hashes: FileHashes,
        torrent_hashes: TorrentFileHashes,
    ) -> TorrentCoverageFile:
        rel = metadata_path(item)
        expected_size = require_metadata_size(item)
        stat = path.stat()
        if stat.st_size != expected_size:
            raise TorrentPublicationError(
                f"cannot record torrent coverage for {rel}: expected {expected_size} bytes, got {stat.st_size}"
            )
        ensure_upstream_hashes(item, full_hashes)
        row = TorrentCoverageFile(
            path=rel,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=full_hashes.sha256,
            git_blob_sha1=full_hashes.git_blob_sha1,
            lfs_sha256=metadata_lfs_sha256(item),
            blob_id=metadata_blob_id(item),
            v1_piece_hashes=tuple(value.hex() for value in torrent_hashes.v1_piece_hashes),
            v2_piece_hashes=tuple(value.hex() for value in torrent_hashes.v2_piece_hashes),
            v2_file_root=(
                torrent_hashes.v2_file_root.hex()
                if torrent_hashes.v2_file_root is not None
                else None
            ),
        )
        with self._lock:
            self.coverage.files[rel] = row
            write_coverage(self.path, self.coverage)
        return row

    def missing_paths(self) -> list[str]:
        missing = []
        for rel, item in self.expected.items():
            path = self.root / rel
            row = self.coverage.files.get(rel)
            if not coverage_row_is_current(row, item, path, self.piece_length):
                missing.append(rel)
        return missing

    @property
    def complete(self) -> bool:
        return not self.missing_paths()

    def _validate_identity(self) -> None:
        expected = (
            self.snapshot.repo_id,
            self.snapshot.repo_type,
            self.snapshot.resolved_commit,
            self.profile,
            self.piece_length,
        )
        actual = (
            self.coverage.repo_id,
            self.coverage.repo_type,
            self.coverage.resolved_commit,
            self.coverage.profile,
            self.coverage.piece_length,
        )
        if actual != expected:
            raise ValueError(
                f"Torrent coverage identity mismatch in {self.path}: expected {expected!r}, found {actual!r}"
            )


def snapshot_files(snapshot) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in sorted(snapshot.files, key=lambda candidate: metadata_path(candidate).encode("utf-8")):
        rel = metadata_path(item)
        if rel in result:
            raise TorrentPublicationError(f"duplicate payload path in pinned snapshot: {rel}")
        result[rel] = item
    return result


def require_metadata_size(item) -> int:
    size = metadata_size(item)
    if size is None or size < 0:
        raise TorrentPublicationError(f"missing or invalid expected size for {metadata_path(item)}")
    return size


def coverage_path(root: Path, resolved_commit: str, profile: str = PUBLICATION_PROFILE) -> Path:
    if SAFE_COMMIT.fullmatch(resolved_commit) is None:
        raise TorrentPublicationError(f"invalid resolved commit for torrent coverage: {resolved_commit!r}")
    return root / COVERAGE_DIR / f"{profile}--{resolved_commit}.json"


def load_coverage(path: Path) -> TorrentCoverage | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed torrent coverage metadata: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Torrent coverage metadata must contain an object: {path}")
    if value.get("schema") != COVERAGE_SCHEMA:
        raise ValueError(f"Unsupported torrent coverage schema in {path}: {value.get('schema')}")
    if value.get("version") != COVERAGE_VERSION:
        raise ValueError(f"Unsupported torrent coverage version in {path}: {value.get('version')}")
    files_value = value.get("files", {})
    if not isinstance(files_value, dict):
        raise ValueError(f"Malformed torrent coverage file map in {path}")
    try:
        coverage = TorrentCoverage(
            repo_id=str(value["repo_id"]),
            repo_type=str(value["repo_type"]),
            resolved_commit=str(value["resolved_commit"]),
            piece_length=int(value["piece_length"]),
            profile=str(value["profile"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Malformed torrent coverage metadata: {path}") from exc
    for rel, row_value in files_value.items():
        row = TorrentCoverageFile.from_dict(row_value, source=path)
        if rel != row.path or rel in coverage.files:
            raise ValueError(f"Malformed torrent coverage file map in {path}")
        coverage.files[rel] = row
    return coverage


def write_coverage(path: Path, coverage: TorrentCoverage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(coverage.to_dict(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def coverage_row_is_current(
    row: TorrentCoverageFile | None,
    item,
    path: Path,
    piece_length: int,
) -> bool:
    if row is None or not path.exists() or not path.is_file() or path.is_symlink():
        return False
    stat = path.stat()
    expected_size = require_metadata_size(item)
    expected_pieces = (expected_size + piece_length - 1) // piece_length
    if (
        row.path != metadata_path(item)
        or row.size != expected_size
        or stat.st_size != expected_size
        or row.mtime_ns != stat.st_mtime_ns
        or len(row.v1_piece_hashes) != expected_pieces
        or len(row.v2_piece_hashes) != expected_pieces
        or (expected_size > 0 and row.v2_file_root is None)
        or (expected_size == 0 and row.v2_file_root is not None)
    ):
        return False
    lfs_sha256 = metadata_lfs_sha256(item)
    blob_id = metadata_blob_id(item)
    if lfs_sha256 is not None:
        return row.sha256 == lfs_sha256 and row.lfs_sha256 == lfs_sha256
    if blob_id is not None:
        return row.git_blob_sha1 == blob_id and row.blob_id == blob_id
    return False


def ensure_upstream_hashes(item, hashes: FileHashes) -> None:
    rel = metadata_path(item)
    lfs_sha256 = metadata_lfs_sha256(item)
    if lfs_sha256 is not None:
        if hashes.sha256 != lfs_sha256:
            raise TorrentPublicationError(f"SHA-256 mismatch while recording torrent coverage for {rel}")
        return
    blob_id = metadata_blob_id(item)
    if blob_id is not None:
        if hashes.git_blob_sha1 != blob_id:
            raise TorrentPublicationError(f"Git blob mismatch while recording torrent coverage for {rel}")
        return
    raise TorrentPublicationError(f"upstream content identity is missing for {rel}")


def upgrade_coverage(
    root: Path,
    snapshot,
    *,
    dry_run: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> CoverageUpgradeResult:
    recorder = TorrentCoverageRecorder(root, snapshot)
    missing = recorder.missing_paths()
    if dry_run:
        return CoverageUpgradeResult(
            path=recorder.path,
            total_files=len(recorder.expected),
            covered_files=len(recorder.expected) - len(missing),
            hashed_files=len(missing),
            hashed_bytes=sum(require_metadata_size(recorder.expected[rel]) for rel in missing),
            complete=not missing,
            dry_run=True,
        )

    hashed_files = 0
    hashed_bytes = 0
    for rel in missing:
        item = recorder.expected[rel]
        path = root / rel
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise TorrentPublicationError(f"cannot upgrade torrent coverage; payload file is missing: {rel}")
        accumulator = recorder.accumulator(item)
        total = require_metadata_size(item)
        hashes = file_hashes(
            path,
            accumulators=(accumulator,),
            on_progress=(
                (lambda done, rel=rel, total=total: on_progress(rel, done, total))
                if on_progress is not None
                else None
            ),
        )
        recorder.record(item, path, hashes, accumulator.finalize())
        hashed_files += 1
        hashed_bytes += total
    return CoverageUpgradeResult(
        path=recorder.path,
        total_files=len(recorder.expected),
        covered_files=len(recorder.expected) - len(recorder.missing_paths()),
        hashed_files=hashed_files,
        hashed_bytes=hashed_bytes,
        complete=recorder.complete,
    )


def coverage_hex(value: object, length: int, label: str, source: Path) -> str:
    text = str(value).lower()
    if len(text) != length or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"Invalid {label} in {source}")
    return text


def optional_coverage_hex(value: object, length: int, label: str, source: Path) -> str | None:
    if value in {None, ""}:
        return None
    return coverage_hex(value, length, label, source)
