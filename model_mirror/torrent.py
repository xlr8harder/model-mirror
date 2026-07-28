from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .checksums import load_manifest, record_is_current
from .hub import read_snapshot_plan
from .state import read_verification_state


PUBLICATION_PROFILE = "hybrid-v1-v2-1"
DESCRIPTOR_SCHEMA = "model-mirror-publication"
DESCRIPTOR_VERSION = 1
DESCRIPTOR_INFO_KEY = b"x-model-mirror"
MIN_PIECE_LENGTH = 1024 * 1024
MAX_PIECE_LENGTH = 16 * 1024 * 1024
TARGET_PIECES = 128 * 1024
SAFE_COMMIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
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


class TorrentPublicationError(RuntimeError):
    pass


class TorrentBackendUnavailable(TorrentPublicationError):
    pass


@dataclass(frozen=True, slots=True)
class TorrentPayloadFile:
    path: str
    size: int
    sha256: str
    git_blob_sha1: str
    lfs_sha256: str | None
    blob_id: str | None

    def bencode_value(self) -> dict[bytes, bytes | int]:
        value: dict[bytes, bytes | int] = {
            b"path": self.path.encode("utf-8"),
            b"size": self.size,
            b"sha256": self.sha256.encode("ascii"),
            b"git_blob_sha1": self.git_blob_sha1.encode("ascii"),
        }
        if self.lfs_sha256 is not None:
            value[b"lfs_sha256"] = self.lfs_sha256.encode("ascii")
        if self.blob_id is not None:
            value[b"blob_id"] = self.blob_id.encode("ascii")
        return value


@dataclass(frozen=True, slots=True)
class PublicationDescriptor:
    repo_id: str
    repo_type: str
    resolved_commit: str
    piece_length: int
    files: tuple[TorrentPayloadFile, ...]
    profile: str = PUBLICATION_PROFILE

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def bencode_value(self) -> dict[bytes, object]:
        return {
            b"schema": DESCRIPTOR_SCHEMA.encode("ascii"),
            b"version": DESCRIPTOR_VERSION,
            b"profile": self.profile.encode("ascii"),
            b"provider": b"huggingface",
            b"repo_id": self.repo_id.encode("utf-8"),
            b"repo_type": self.repo_type.encode("ascii"),
            b"resolved_commit": self.resolved_commit.encode("ascii"),
            b"piece_length": self.piece_length,
            b"files": [item.bencode_value() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class HybridMetainfo:
    metainfo: bytes
    descriptor_sha256: str
    metainfo_sha256: str
    infohash_v1: str
    infohash_v2: str
    magnet_uri: str
    piece_length: int


def build_publication_descriptor(
    root: Path,
    *,
    repo_id: str,
    repo_type: str,
) -> PublicationDescriptor:
    state = read_verification_state(root)
    if state is None:
        raise TorrentPublicationError(f"verification state is missing: {root / '.verification'}")
    if not state.clean:
        raise TorrentPublicationError(f"mirror is not clean: {repo_id} state={state.status}")
    if state.repo_id != repo_id or state.repo_type != repo_type:
        raise TorrentPublicationError(
            f"verification identity mismatch: expected {repo_type}:{repo_id}, "
            f"found {state.repo_type}:{state.repo_id}"
        )
    if not state.resolved_commit or SAFE_COMMIT.fullmatch(state.resolved_commit) is None:
        raise TorrentPublicationError(f"invalid resolved commit in verification state: {state.resolved_commit!r}")

    snapshot = read_snapshot_plan(root)
    if snapshot is None:
        raise TorrentPublicationError(f"pinned snapshot is missing: {root / '.model-mirror' / 'snapshot.json'}")
    if (
        snapshot.repo_id != repo_id
        or snapshot.repo_type != repo_type
        or snapshot.resolved_commit != state.resolved_commit
    ):
        raise TorrentPublicationError("pinned snapshot does not match the clean verification state")

    manifest = load_manifest(root)
    payload_files: list[TorrentPayloadFile] = []
    seen: set[str] = set()
    for item in sorted(snapshot.files, key=lambda candidate: candidate.path.encode("utf-8")):
        rel = validate_payload_path(item.path)
        if rel in seen:
            raise TorrentPublicationError(f"duplicate payload path in pinned snapshot: {rel}")
        seen.add(rel)
        if item.size is None or item.size < 0:
            raise TorrentPublicationError(f"missing or invalid expected size for {rel}")

        path = root / PurePosixPath(rel)
        validate_payload_file(root, path, rel)
        stat = path.stat()
        row = manifest.get(rel)
        if not record_is_current(row, stat.st_size, stat.st_mtime_ns):
            raise TorrentPublicationError(f"manifest record is missing or stale for {rel}")
        if stat.st_size != item.size:
            raise TorrentPublicationError(f"size mismatch for {rel}: expected {item.size}, got {stat.st_size}")

        sha256 = require_hex(row.get("sha256"), 64, f"manifest SHA-256 for {rel}")
        git_blob_sha1 = require_hex(row.get("git_blob_sha1"), 40, f"manifest Git blob SHA-1 for {rel}")
        lfs_sha256 = optional_hex(item.lfs_sha256, 64, f"LFS SHA-256 for {rel}")
        blob_id = optional_hex(item.blob_id, 40, f"upstream blob ID for {rel}")
        if lfs_sha256 is None and blob_id is None:
            raise TorrentPublicationError(f"upstream content identity is missing for {rel}")
        if lfs_sha256 is not None and sha256 != lfs_sha256:
            raise TorrentPublicationError(f"manifest SHA-256 does not match upstream LFS identity for {rel}")
        if lfs_sha256 is None and git_blob_sha1 != blob_id:
            raise TorrentPublicationError(f"manifest Git blob SHA-1 does not match upstream identity for {rel}")

        payload_files.append(
            TorrentPayloadFile(
                path=rel,
                size=item.size,
                sha256=sha256,
                git_blob_sha1=git_blob_sha1,
                lfs_sha256=lfs_sha256,
                blob_id=blob_id,
            )
        )

    total_size = sum(item.size for item in payload_files)
    if not payload_files or total_size == 0:
        raise TorrentPublicationError("a torrent publication requires at least one non-empty payload file")
    return PublicationDescriptor(
        repo_id=repo_id,
        repo_type=repo_type,
        resolved_commit=state.resolved_commit,
        piece_length=select_piece_length(total_size),
        files=tuple(payload_files),
    )


def select_piece_length(total_size: int) -> int:
    if total_size < 1:
        raise ValueError("total size must be positive")
    required = math.ceil(total_size / TARGET_PIECES)
    selected = 1 << (required - 1).bit_length()
    return min(MAX_PIECE_LENGTH, max(MIN_PIECE_LENGTH, selected))


def validate_payload_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise TorrentPublicationError(f"unsafe payload path: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise TorrentPublicationError(f"unsafe payload path: {value!r}")
    normalized = path.as_posix()
    if path.parts[0] in RESERVED_TOP_LEVEL:
        raise TorrentPublicationError(f"payload path conflicts with model-mirror metadata: {value!r}")
    return normalized


def validate_payload_file(root: Path, path: Path, rel: str) -> None:
    if not path.exists() or not path.is_file():
        raise TorrentPublicationError(f"payload file is missing: {rel}")
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            raise TorrentPublicationError(f"payload path contains a symlink: {rel}")


def require_hex(value: object, length: int, label: str) -> str:
    text = str(value or "").lower()
    if len(text) != length or any(character not in "0123456789abcdef" for character in text):
        raise TorrentPublicationError(f"invalid {label}")
    return text


def optional_hex(value: object, length: int, label: str) -> str | None:
    if value in {None, ""}:
        return None
    return require_hex(value, length, label)


def create_hybrid_metainfo(
    root: Path,
    descriptor: PublicationDescriptor,
    *,
    libtorrent_module=None,
) -> HybridMetainfo:
    lt = libtorrent_module or load_libtorrent()
    storage = lt.file_storage()
    for item in descriptor.files:
        storage.add_file(f"{root.name}/{item.path}", item.size)
    creator = lt.create_torrent(storage, descriptor.piece_length, 0)
    lt.set_piece_hashes(creator, str(root.parent))
    metainfo_tree = creator.generate()
    metainfo_tree.pop(b"creation date", None)
    metainfo_tree[b"info"][DESCRIPTOR_INFO_KEY] = descriptor.bencode_value()
    return hybrid_metainfo_from_tree(metainfo_tree, descriptor, libtorrent_module=lt)


def create_hybrid_metainfo_from_coverage(
    root: Path,
    descriptor: PublicationDescriptor,
    coverage,
    *,
    libtorrent_module=None,
) -> HybridMetainfo:
    """Build hybrid metainfo from verified coverage without reading payload bytes."""
    lt = libtorrent_module or load_libtorrent()
    validate_coverage_for_descriptor(root, descriptor, coverage)

    v1_pieces = bytearray()
    file_tree: dict[bytes, object] = {}
    piece_layers: dict[bytes, bytes] = {}

    for item in descriptor.files:
        row = coverage.files[item.path]
        path_parts = [part.encode("utf-8") for part in PurePosixPath(item.path).parts]
        v1_pieces.extend(b"".join(bytes.fromhex(value) for value in row.v1_piece_hashes))

        leaf: dict[bytes, object] = {b"length": item.size}
        if row.v2_file_root is not None:
            file_root = bytes.fromhex(row.v2_file_root)
            leaf[b"pieces root"] = file_root
            if item.size > descriptor.piece_length:
                piece_layers[file_root] = b"".join(
                    bytes.fromhex(value) for value in row.v2_piece_hashes
                )
        insert_file_tree_leaf(file_tree, path_parts, leaf)

    metainfo_tree = {
        b"info": {
            b"file tree": file_tree,
            b"files": hybrid_v1_files(descriptor),
            b"meta version": 2,
            b"name": root.name.encode("utf-8"),
            b"piece length": descriptor.piece_length,
            b"pieces": bytes(v1_pieces),
            DESCRIPTOR_INFO_KEY: descriptor.bencode_value(),
        },
        b"piece layers": piece_layers,
    }
    return hybrid_metainfo_from_tree(metainfo_tree, descriptor, libtorrent_module=lt)


def hybrid_v1_files(descriptor: PublicationDescriptor) -> list[dict[bytes, object]]:
    result: list[dict[bytes, object]] = []
    multifile = len(descriptor.files) > 1
    for item in descriptor.files:
        result.append(
            {
                b"length": item.size,
                b"path": [
                    part.encode("utf-8")
                    for part in PurePosixPath(item.path).parts
                ],
            }
        )
        if multifile and item.size and item.size % descriptor.piece_length:
            padding = descriptor.piece_length - (item.size % descriptor.piece_length)
            result.append(
                {
                    b"attr": b"p",
                    b"length": padding,
                    b"path": [b".pad", str(padding).encode("ascii")],
                }
            )
    return result


def validate_coverage_for_descriptor(
    root: Path,
    descriptor: PublicationDescriptor,
    coverage,
) -> None:
    identity = (
        coverage.repo_id,
        coverage.repo_type,
        coverage.resolved_commit,
        coverage.profile,
        coverage.piece_length,
    )
    expected_identity = (
        descriptor.repo_id,
        descriptor.repo_type,
        descriptor.resolved_commit,
        descriptor.profile,
        descriptor.piece_length,
    )
    if identity != expected_identity:
        raise TorrentPublicationError(
            f"torrent coverage does not match publication descriptor: "
            f"expected {expected_identity!r}, found {identity!r}"
        )
    expected_paths = {item.path for item in descriptor.files}
    if set(coverage.files) != expected_paths:
        missing = sorted(expected_paths - set(coverage.files))
        extra = sorted(set(coverage.files) - expected_paths)
        raise TorrentPublicationError(
            f"torrent coverage file set does not match publication descriptor: "
            f"missing={missing!r} extra={extra!r}"
        )

    for item in descriptor.files:
        row = coverage.files[item.path]
        path = root / PurePosixPath(item.path)
        validate_payload_file(root, path, item.path)
        stat = path.stat()
        expected_pieces = (item.size + descriptor.piece_length - 1) // descriptor.piece_length
        if (
            row.path != item.path
            or row.size != item.size
            or stat.st_size != item.size
            or row.mtime_ns != stat.st_mtime_ns
            or row.sha256 != item.sha256
            or row.git_blob_sha1 != item.git_blob_sha1
            or row.lfs_sha256 != item.lfs_sha256
            or row.blob_id != item.blob_id
            or len(row.v1_piece_hashes) != expected_pieces
            or len(row.v2_piece_hashes) != expected_pieces
            or (item.size > 0) != (row.v2_file_root is not None)
        ):
            raise TorrentPublicationError(f"torrent coverage is incomplete or stale for {item.path}")
        for value in row.v1_piece_hashes:
            require_hex(value, 40, f"v1 piece hash for {item.path}")
        for value in row.v2_piece_hashes:
            require_hex(value, 64, f"v2 piece hash for {item.path}")
        if row.v2_file_root is not None:
            require_hex(row.v2_file_root, 64, f"v2 file root for {item.path}")


def insert_file_tree_leaf(
    tree: dict[bytes, object],
    path_parts: list[bytes],
    leaf: dict[bytes, object],
) -> None:
    current = tree
    for part in path_parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict) or b"" in child:
            raise TorrentPublicationError("payload paths collide in the v2 file tree")
        current = child
    final = path_parts[-1]
    if final in current:
        raise TorrentPublicationError("payload paths collide in the v2 file tree")
    current[final] = {b"": leaf}


def hybrid_metainfo_from_tree(
    metainfo_tree: dict[bytes, object],
    descriptor: PublicationDescriptor,
    *,
    libtorrent_module,
) -> HybridMetainfo:
    lt = libtorrent_module
    metainfo = bytes(lt.bencode(metainfo_tree))
    torrent_info = lt.torrent_info(metainfo)
    info_hashes = torrent_info.info_hashes()
    if not info_hashes.has_v1() or not info_hashes.has_v2():
        raise TorrentPublicationError("publication profile did not produce a hybrid v1/v2 torrent")
    descriptor_bytes = bytes(lt.bencode(descriptor.bencode_value()))
    return HybridMetainfo(
        metainfo=metainfo,
        descriptor_sha256=hashlib.sha256(descriptor_bytes).hexdigest(),
        metainfo_sha256=hashlib.sha256(metainfo).hexdigest(),
        infohash_v1=str(info_hashes.v1),
        infohash_v2=str(info_hashes.v2),
        magnet_uri=lt.make_magnet_uri(torrent_info),
        piece_length=descriptor.piece_length,
    )


def verified_seed_params(
    metainfo: bytes,
    payload_root: Path,
    *,
    libtorrent_module=None,
):
    lt = libtorrent_module or load_libtorrent()
    torrent_info = lt.torrent_info(metainfo)
    if torrent_info.name() != payload_root.name:
        raise TorrentPublicationError(
            f"torrent payload root mismatch: expected {payload_root.name!r}, found {torrent_info.name()!r}"
        )
    params = lt.add_torrent_params()
    params.ti = torrent_info
    params.save_path = str(payload_root.parent)
    params.have_pieces = [True] * torrent_info.num_pieces()
    params.verified_pieces = [True] * torrent_info.num_pieces()
    params.flags &= ~lt.torrent_flags.paused
    params.flags &= ~lt.torrent_flags.auto_managed
    return params


def load_libtorrent():
    try:
        import libtorrent
    except ModuleNotFoundError as exc:
        raise TorrentBackendUnavailable(
            "libtorrent is required for managed torrent support; "
            "install the model-mirror-cli distribution with the 'torrent' extra"
        ) from exc
    return libtorrent
