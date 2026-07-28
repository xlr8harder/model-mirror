from __future__ import annotations

from pathlib import Path, PurePosixPath


RESERVED_TOP_LEVEL = {
    ".archive",
    ".cache",
    ".checksums",
    ".manifest",
    ".model-mirror",
    ".pad",
    ".verification",
    ".verification.lock",
}


class PayloadPathError(ValueError):
    pass


class PayloadMissingError(PayloadPathError):
    pass


class UnsafePayloadError(PayloadPathError):
    pass


def validate_payload_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise UnsafePayloadError(f"unsafe payload path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UnsafePayloadError(f"unsafe payload path: {value!r}")
    normalized = path.as_posix()
    if path.parts[0] in RESERVED_TOP_LEVEL:
        raise UnsafePayloadError(f"payload path conflicts with model-mirror metadata: {value!r}")
    return normalized


def validate_payload_file(root: Path, path: Path, rel: str) -> None:
    validate_payload_destination(root, path, rel)
    if not path.exists():
        raise PayloadMissingError(f"payload file is missing: {rel}")


def validate_payload_destination(root: Path, path: Path, rel: str) -> None:
    normalized = validate_payload_parent(root, path, rel)
    if path.is_symlink():
        raise UnsafePayloadError(f"payload path contains a symlink: {rel}")
    if path.exists() and not path.is_file():
        raise UnsafePayloadError(f"payload path is not a regular file: {rel}")


def validate_payload_parent(root: Path, path: Path, rel: str) -> str:
    normalized = validate_payload_path(rel)
    expected = root / PurePosixPath(normalized)
    if path != expected:
        raise UnsafePayloadError(f"payload path resolves outside its expected location: {rel}")
    current = root
    for part in PurePosixPath(normalized).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafePayloadError(f"payload path contains a symlink: {rel}")
        if current.exists() and not current.is_dir():
            raise UnsafePayloadError(f"payload path has a non-directory parent: {rel}")
    return normalized
