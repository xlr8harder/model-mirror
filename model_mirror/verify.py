from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .checksums import load_records, sha256_file, iter_payload_files


@dataclass(slots=True)
class RemoteVerifyResult:
    files_checked: int = 0
    hashes_checked: int = 0
    missing: list[str] = field(default_factory=list)
    size_mismatches: list[str] = field(default_factory=list)
    hash_mismatches: list[str] = field(default_factory=list)
    hash_missing: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing
            or self.size_mismatches
            or self.hash_mismatches
            or self.hash_missing
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


def git_blob_sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    size = path.stat().st_size
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_remote(
    root: Path,
    metadata,
    *,
    quick: bool = False,
    from_checksums: bool = False,
    strict: bool = False,
) -> RemoteVerifyResult:
    result = RemoteVerifyResult()
    checksums: dict[str, str] = {}
    manifest: dict[str, dict] = {}
    if from_checksums:
        checksums, manifest = load_records(root)

    expected_paths = set()
    for item in metadata:
        rel = metadata_path(item)
        expected_paths.add(rel)
        path = root / rel
        if not path.exists():
            result.missing.append(rel)
            continue

        result.files_checked += 1
        expected_size = metadata_size(item)
        if from_checksums and rel in manifest:
            actual_size = manifest[rel].get("size")
        else:
            actual_size = path.stat().st_size
        if expected_size is not None and actual_size != expected_size:
            result.size_mismatches.append(rel)
            continue

        if quick:
            continue

        expected_lfs_hash = metadata_lfs_sha256(item)
        if expected_lfs_hash is not None:
            if from_checksums:
                actual_hash = checksums.get(rel)
                if actual_hash is None:
                    result.hash_missing.append(rel)
                    continue
            else:
                actual_hash = sha256_file(path)
            result.hashes_checked += 1
            if actual_hash != expected_lfs_hash:
                result.hash_mismatches.append(rel)
            continue

        expected_blob_id = metadata_blob_id(item)
        if expected_blob_id is None:
            continue

        actual_hash = git_blob_sha1_file(path)
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
