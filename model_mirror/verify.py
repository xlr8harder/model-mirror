from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .checksums import file_hashes, load_manifest, iter_payload_files, record_is_current


@dataclass(slots=True)
class RemoteVerifyResult:
    files_checked: int = 0
    hashes_checked: int = 0
    missing: list[str] = field(default_factory=list)
    size_mismatches: list[str] = field(default_factory=list)
    hash_mismatches: list[str] = field(default_factory=list)
    cached_hash_missing: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing
            or self.size_mismatches
            or self.hash_mismatches
            or self.cached_hash_missing
            or self.extras
        )


def metadata_path(item) -> str:
    direct = getattr(item, "path", None)
    if direct is not None:
        return direct
    return getattr(item, "rfilename")


def metadata_size(item) -> int | None:
    return getattr(item, "size", None)


def metadata_lfs_sha256(item) -> str | None:
    direct = getattr(item, "lfs_sha256", None)
    if direct:
        return direct
    lfs = getattr(item, "lfs", None)
    if lfs is None:
        return None
    return getattr(lfs, "sha256", None)


def metadata_blob_id(item) -> str | None:
    return getattr(item, "blob_id", None)


def verify_remote(
    root: Path,
    metadata,
    *,
    cached: bool = False,
    from_manifest: bool = False,
    check_hashes: bool = True,
    strict: bool = False,
) -> RemoteVerifyResult:
    result = RemoteVerifyResult()
    manifest: dict[str, dict] = {}
    if cached or from_manifest:
        manifest = load_manifest(root)

    expected_paths = set()
    for item in metadata:
        rel = metadata_path(item)
        expected_paths.add(rel)
        path = root / rel
        if not path.exists():
            result.missing.append(rel)
            continue

        result.files_checked += 1
        stat = path.stat()
        expected_size = metadata_size(item)
        actual_size = stat.st_size
        if expected_size is not None and actual_size != expected_size:
            result.size_mismatches.append(rel)
            continue

        if not check_hashes:
            continue

        expected_lfs_hash = metadata_lfs_sha256(item)
        if expected_lfs_hash is not None:
            manifest_hash = current_manifest_hash(manifest, rel, stat.st_size, stat.st_mtime_ns, "sha256")
            if manifest_hash is not None:
                actual_hash = manifest_hash
            elif cached:
                result.cached_hash_missing.append(rel)
                continue
            else:
                actual_hash = file_hashes(path).sha256
            result.hashes_checked += 1
            if actual_hash != expected_lfs_hash:
                result.hash_mismatches.append(rel)
            continue

        expected_blob_id = metadata_blob_id(item)
        if expected_blob_id is None:
            continue

        manifest_hash = current_manifest_hash(manifest, rel, stat.st_size, stat.st_mtime_ns, "git_blob_sha1")
        if manifest_hash is not None:
            actual_hash = manifest_hash
        elif cached:
            result.cached_hash_missing.append(rel)
            continue
        else:
            actual_hash = file_hashes(path).git_blob_sha1
        result.hashes_checked += 1
        if actual_hash != expected_blob_id:
            result.hash_mismatches.append(rel)

    if strict:
        result.extras = [
            path.relative_to(root).as_posix()
            for path in iter_payload_files(root)
            if path.relative_to(root).as_posix() not in expected_paths
        ]

    return result


def current_manifest_hash(
    manifest: dict[str, dict],
    rel: str,
    size: int,
    mtime_ns: int,
    hash_key: str,
) -> str | None:
    row = manifest.get(rel)
    if not record_is_current(row, size, mtime_ns):
        return None
    value = row.get(hash_key)
    return str(value) if value else None


def merge_checksum_result(remote_result, checksum_result) -> None:
    for attr, values in (
        ("missing", checksum_result.missing),
        ("hash_mismatches", checksum_result.failures),
        ("extras", checksum_result.extras),
    ):
        existing = getattr(remote_result, attr)
        for value in values:
            if value not in existing:
                existing.append(value)
