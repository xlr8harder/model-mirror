from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path


CHECKSUMS = ".checksums"
MANIFEST = ".manifest"
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(16 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(root: Path, *, max_workers: int = 1) -> ChecksumWriteResult:
    checksums, manifest = load_records(root)
    payload_files = list(iter_payload_files(root))
    current_paths = {path.relative_to(root).as_posix() for path in payload_files}
    result = ChecksumWriteResult(total=len(payload_files))

    for rel in sorted((set(checksums) | set(manifest)) - current_paths):
        checksums.pop(rel, None)
        manifest.pop(rel, None)
        result.removed += 1

    work: list[Path] = []
    for path in payload_files:
        rel = path.relative_to(root).as_posix()
        stat = path.stat()
        if checksums.get(rel) and record_is_current(manifest.get(rel), stat.st_size, stat.st_mtime_ns):
            result.skipped += 1
            continue
        work.append(path)

    if result.removed and not work:
        write_records(root, checksums, manifest)
    if not work:
        return result

    workers = max(1, max_workers)
    if workers == 1:
        for path in work:
            row = checksum_row(root, path)
            checksums[row["path"]] = row["sha256"]
            manifest[row["path"]] = row
            result.hashed += 1
            write_records(root, checksums, manifest)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(checksum_row, root, path) for path in work]
        for future in as_completed(futures):
            row = future.result()
            checksums[row["path"]] = row["sha256"]
            manifest[row["path"]] = row
            result.hashed += 1
            write_records(root, checksums, manifest)
    return result


def record_is_current(row: dict | None, size: int, mtime_ns: int) -> bool:
    if row is None:
        return False
    return row.get("size") == size and row.get("mtime_ns") == mtime_ns


def checksum_row(root: Path, path: Path) -> dict:
    digest = sha256_file(path)
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": digest,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def write_records(root: Path, checksums: dict[str, str], manifest: dict[str, dict]) -> None:
    checksum_tmp = root / f"{CHECKSUMS}.tmp"
    manifest_tmp = root / f"{MANIFEST}.tmp"
    with checksum_tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for rel in sorted(checksums):
            handle.write(f"{checksums[rel]}  {rel}\n")
    with manifest_tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for rel in sorted(manifest):
            handle.write(json.dumps(manifest[rel], sort_keys=True) + "\n")
    checksum_tmp.replace(root / CHECKSUMS)
    manifest_tmp.replace(root / MANIFEST)


def update_checksums(root: Path, paths: list[str], *, max_workers: int = 1) -> ChecksumWriteResult:
    checksums, manifest = load_records(root)
    result = ChecksumWriteResult(total=len(paths))
    work: list[Path] = []
    for rel in paths:
        path = root / rel
        if not path.exists() or not path.is_file():
            removed_checksum = checksums.pop(rel, None) is not None
            removed_manifest = manifest.pop(rel, None) is not None
            if removed_checksum or removed_manifest:
                result.removed += 1
            continue
        work.append(path)
    if result.removed and not work:
        write_records(root, checksums, manifest)
    if not work:
        return result

    workers = max(1, max_workers)
    if workers == 1:
        for path in work:
            row = checksum_row(root, path)
            checksums[row["path"]] = row["sha256"]
            manifest[row["path"]] = row
            result.hashed += 1
            write_records(root, checksums, manifest)
        return result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(checksum_row, root, path) for path in work]
        for future in as_completed(futures):
            row = future.result()
            checksums[row["path"]] = row["sha256"]
            manifest[row["path"]] = row
            result.hashed += 1
            write_records(root, checksums, manifest)
    return result


def load_records(root: Path) -> tuple[dict[str, str], dict[str, dict]]:
    checksums: dict[str, str] = {}
    checksum_path = root / CHECKSUMS
    if checksum_path.exists():
        with checksum_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    digest, rel = line.split("  ", 1)
                except ValueError as exc:
                    raise ValueError(f"Malformed line {line_number} in {checksum_path}") from exc
                checksums[rel] = digest

    manifest: dict[str, dict] = {}
    manifest_path = root / MANIFEST
    if manifest_path.exists():
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    manifest[row["path"]] = row
                except Exception as exc:
                    raise ValueError(f"Malformed line {line_number} in {manifest_path}") from exc
    return checksums, manifest


def verify_checksums(root: Path, strict: bool = False) -> ChecksumResult:
    checksums, _ = load_records(root)
    if not checksums:
        return ChecksumResult()

    result = ChecksumResult()
    for rel, expected in checksums.items():
        path = root / rel
        if not path.exists():
            result.missing.append(rel)
            continue
        result.checked += 1
        if sha256_file(path) != expected:
            result.failures.append(rel)

    if strict:
        tracked = set(checksums)
        result.extras = [
            path.relative_to(root).as_posix()
            for path in iter_payload_files(root)
            if path.relative_to(root).as_posix() not in tracked
        ]

    return result
