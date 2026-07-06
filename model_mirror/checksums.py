from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path


CHECKSUMS = ".checksums"
MANIFEST = ".manifest"
MANIFEST_SCHEMA = "model-mirror-manifest"
MANIFEST_VERSION = 1
SKIP_DIRS = {".model-mirror", ".cache", ".archive"}
SKIP_FILES = {
    CHECKSUMS,
    MANIFEST,
    ".checksums.tmp",
    ".manifest.tmp",
    ".verification",
    ".verification.lock",
    ".verification.tmp",
}


@dataclass(slots=True)
class ChecksumResult:
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.failures and not self.extras


@dataclass(slots=True)
class ChecksumWriteResult:
    total: int = 0
    hashed: int = 0
    skipped: int = 0
    removed: int = 0


@dataclass(frozen=True, slots=True)
class FileHashes:
    sha256: str
    git_blob_sha1: str


@dataclass(slots=True)
class FileHashState:
    sha256: object
    git_blob_sha1: object
    bytes_hashed: int = 0

    @property
    def hashes(self) -> FileHashes:
        return FileHashes(sha256=self.sha256.hexdigest(), git_blob_sha1=self.git_blob_sha1.hexdigest())


class HashingWriter:
    def __init__(self, handle, *, expected_size: int, hash_state: FileHashState | None = None):
        self._handle = handle
        self._expected_size = expected_size
        self._hash_state = hash_state or new_hash_state(expected_size)

    def write(self, data: bytes) -> int:
        written = self._handle.write(data)
        chunk = data if written == len(data) else data[:written]
        self._hash_state.sha256.update(chunk)
        self._hash_state.git_blob_sha1.update(chunk)
        self._hash_state.bytes_hashed += written
        return written

    def tell(self) -> int:
        return self._handle.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)

    def truncate(self, size: int | None = None) -> int:
        truncated = self._handle.truncate(size)
        if truncated == 0:
            self._hash_state = new_hash_state(self._expected_size)
        elif truncated != self._hash_state.bytes_hashed:
            raise OSError("cannot preserve streaming hash state after non-zero truncate")
        return truncated

    def flush(self) -> None:
        self._handle.flush()

    def fileno(self) -> int:
        return self._handle.fileno()

    @property
    def hashes(self) -> FileHashes:
        return self._hash_state.hashes


def new_hash_state(expected_size: int) -> FileHashState:
    sha256 = hashlib.sha256()
    git_blob_sha1 = hashlib.sha1()
    git_blob_sha1.update(f"blob {expected_size}\0".encode("ascii"))
    return FileHashState(sha256=sha256, git_blob_sha1=git_blob_sha1)


def hash_file_prefix(path: Path, *, total_size: int, prefix_size: int) -> FileHashState:
    state = new_hash_state(total_size)
    remaining = prefix_size
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(16 * 1024 * 1024, remaining))
            if not chunk:
                raise OSError(f"short read while hashing prefix: {path}")
            state.sha256.update(chunk)
            state.git_blob_sha1.update(chunk)
            state.bytes_hashed += len(chunk)
            remaining -= len(chunk)
    return state


def iter_payload_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        if rel.as_posix() in SKIP_FILES:
            continue
        yield path


def file_hashes(path: Path) -> FileHashes:
    stat = path.stat()
    digest = hashlib.sha256()
    blob_digest = hashlib.sha1()
    blob_digest.update(f"blob {stat.st_size}\0".encode("ascii"))
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
            blob_digest.update(chunk)
    return FileHashes(sha256=digest.hexdigest(), git_blob_sha1=blob_digest.hexdigest())


def write_checksums(root: Path, *, max_workers: int = 1) -> ChecksumWriteResult:
    manifest = load_manifest(root)
    payload_files = list(iter_payload_files(root))
    current_paths = {path.relative_to(root).as_posix() for path in payload_files}
    result = ChecksumWriteResult(total=len(payload_files))

    for rel in sorted(set(manifest) - current_paths):
        manifest.pop(rel, None)
        result.removed += 1

    work: list[Path] = []
    for path in payload_files:
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        if record_is_current(manifest.get(rel), stat.st_size, stat.st_mtime_ns):
            result.skipped += 1
            continue
        work.append(path)

    if result.removed and not work:
        write_manifest(root, manifest)
    if not work:
        return result

    workers = max(1, max_workers)
    if workers == 1:
        for path in work:
            row = checksum_row(root, path)
            manifest[row["path"]] = row
            result.hashed += 1
            write_manifest(root, manifest)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(checksum_row, root, path) for path in work]
        for future in as_completed(futures):
            row = future.result()
            manifest[row["path"]] = row
            result.hashed += 1
            write_manifest(root, manifest)
    return result


def record_is_current(row: dict | None, size: int, mtime_ns: int) -> bool:
    if row is None:
        return False
    return (
        row.get("size") == size
        and row.get("mtime_ns") == mtime_ns
        and bool(row.get("sha256"))
        and bool(row.get("git_blob_sha1"))
    )


def checksum_row(root: Path, path: Path) -> dict:
    hashes = file_hashes(path)
    return checksum_row_from_hashes(root, path, hashes)


def checksum_row_from_hashes(root: Path, path: Path, hashes: FileHashes) -> dict:
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashes.sha256,
        "git_blob_sha1": hashes.git_blob_sha1,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def write_manifest(root: Path, manifest: dict[str, dict]) -> None:
    manifest_tmp = root / f"{MANIFEST}.tmp"
    with manifest_tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION}, sort_keys=True) + "\n")
        for rel in sorted(manifest):
            handle.write(json.dumps(manifest[rel], sort_keys=True) + "\n")
    manifest_tmp.replace(root / MANIFEST)
    remove_obsolete_checksum_files(root)


def remove_obsolete_checksum_files(root: Path) -> None:
    for path in (root / CHECKSUMS, root / f"{CHECKSUMS}.tmp"):
        path.unlink(missing_ok=True)


def update_checksums(root: Path, paths: list[str], *, max_workers: int = 1) -> ChecksumWriteResult:
    manifest = load_manifest(root)
    result = ChecksumWriteResult(total=len(paths))
    work: list[Path] = []
    for rel in paths:
        path = root / rel
        if not path.exists() or not path.is_file():
            if manifest.pop(rel, None) is not None:
                result.removed += 1
            continue
        work.append(path)
    if result.removed and not work:
        write_manifest(root, manifest)
    if not work:
        return result

    workers = max(1, max_workers)
    if workers == 1:
        for path in work:
            row = checksum_row(root, path)
            manifest[row["path"]] = row
            result.hashed += 1
            write_manifest(root, manifest)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(checksum_row, root, path) for path in work]
        for future in as_completed(futures):
            row = future.result()
            manifest[row["path"]] = row
            result.hashed += 1
            write_manifest(root, manifest)
    return result


def load_manifest(root: Path) -> dict[str, dict]:
    manifest: dict[str, dict] = {}
    manifest_path = root / MANIFEST
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as handle:
            saw_record = False
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception as exc:
                    raise ValueError(f"Malformed line {line_number} in {manifest_path}") from exc
                if not saw_record:
                    if not row.get("schema"):
                        raise ValueError(f"Manifest missing header: {manifest_path}")
                    validate_manifest_header(row, manifest_path)
                    saw_record = True
                    continue
                saw_record = True
                try:
                    manifest[row["path"]] = row
                except KeyError as exc:
                    raise ValueError(f"Malformed line {line_number} in {manifest_path}") from exc
    return manifest


def validate_manifest_header(row: dict, path: Path) -> None:
    if row.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"Unsupported manifest schema in {path}: {row.get('schema')}")
    if row.get("version") != MANIFEST_VERSION:
        raise ValueError(f"Unsupported manifest version in {path}: {row.get('version')}")


def verify_checksums(root: Path, strict: bool = False) -> ChecksumResult:
    manifest = load_manifest(root)
    if not manifest:
        return ChecksumResult()

    result = ChecksumResult()
    for rel, row in manifest.items():
        path = root / rel
        if not path.exists():
            result.missing.append(rel)
            continue
        result.checked += 1
        hashes = file_hashes(path)
        if hashes.sha256 != row.get("sha256") or hashes.git_blob_sha1 != row.get("git_blob_sha1"):
            result.failures.append(rel)

    if strict:
        tracked = set(manifest)
        result.extras = [
            path.relative_to(root).as_posix()
            for path in iter_payload_files(root)
            if path.relative_to(root).as_posix() not in tracked
        ]

    return result
