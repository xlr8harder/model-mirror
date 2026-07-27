from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .checksums import FileHashes, file_hashes, write_manifest
from .config import Config, archive_path, safe_repo_path
from .hub import HubFile, HubSnapshot, write_snapshot_plan
from .state import VerificationState, utc_now, write_verification_state
from .torrent import (
    DESCRIPTOR_INFO_KEY,
    DESCRIPTOR_SCHEMA,
    DESCRIPTOR_VERSION,
    PUBLICATION_PROFILE,
    SAFE_COMMIT,
    HybridMetainfo,
    PublicationDescriptor,
    TorrentPayloadFile,
    TorrentPublicationError,
    hybrid_metainfo_from_tree,
    hybrid_v1_files,
    load_libtorrent,
    require_hex,
    select_piece_length,
    validate_payload_path,
)
from .torrent_coverage import (
    TorrentCoverage,
    TorrentCoverageFile,
    TorrentCoverageRecorder,
    coverage_path,
    write_coverage,
)
from .torrent_publication import (
    FENCE_SCHEMA,
    FENCE_VERSION,
    PublicationRecord,
    fence_path,
    publication_dir,
    publication_record_path,
    recovery_value,
    write_bytes_atomic,
    write_json_atomic,
)


MAX_TORRENT_FILES = 1_000_000
MAX_TORRENT_PAYLOAD_BYTES = 1 << 60
IMPORT_SCHEMA = "model-mirror-torrent-import"
IMPORT_VERSION = 1
STAGING_DIR = ".torrent-staging"


@dataclass(frozen=True, slots=True)
class ParsedPublication:
    descriptor: PublicationDescriptor
    artifact: HybridMetainfo
    metainfo_tree: dict[bytes, object]
    root_name: str


@dataclass(frozen=True, slots=True)
class ImportResult:
    path: Path
    publication: PublicationRecord
    reread_files: int
    reread_bytes: int


def parse_publication_metainfo(
    metainfo: bytes,
    *,
    libtorrent_module=None,
) -> ParsedPublication:
    lt = libtorrent_module or load_libtorrent()
    try:
        tree = lt.bdecode(metainfo)
    except Exception as exc:
        raise TorrentPublicationError("torrent metainfo is malformed") from exc
    if not isinstance(tree, dict) or not isinstance(tree.get(b"info"), dict):
        raise TorrentPublicationError("torrent metainfo has no info dictionary")
    info = tree[b"info"]
    raw_descriptor = info.get(DESCRIPTOR_INFO_KEY)
    if not isinstance(raw_descriptor, dict):
        raise TorrentPublicationError("torrent does not contain a model-mirror publication descriptor")
    descriptor = parse_descriptor(raw_descriptor)
    if raw_descriptor != descriptor.bencode_value():
        raise TorrentPublicationError("publication descriptor is non-canonical or contains unknown fields")
    if info.get(b"piece length") != descriptor.piece_length:
        raise TorrentPublicationError("torrent piece length does not match its publication descriptor")
    if info.get(b"meta version") != 2:
        raise TorrentPublicationError("publication is not a BitTorrent v2/hybrid torrent")
    try:
        root_name = bytes_text(info[b"name"], "torrent root name")
    except KeyError as exc:
        raise TorrentPublicationError("torrent root name is missing") from exc
    expected_root = safe_repo_path(descriptor.repo_id).name
    if root_name != expected_root:
        raise TorrentPublicationError(
            f"torrent root name does not match repository identity: {root_name!r} != {expected_root!r}"
        )
    if info.get(b"files") != hybrid_v1_files(descriptor):
        raise TorrentPublicationError("torrent payload layout does not match the publication descriptor")
    validate_v2_file_tree(info.get(b"file tree"), descriptor)
    artifact = hybrid_metainfo_from_tree(tree, descriptor, libtorrent_module=lt)
    if artifact.metainfo != metainfo:
        # bencode canonicalization is part of the profile and protects deterministic handoff.
        raise TorrentPublicationError("torrent metainfo is not canonically bencoded")
    return ParsedPublication(descriptor, artifact, tree, root_name)


def parse_descriptor(value: dict[bytes, object]) -> PublicationDescriptor:
    try:
        if bytes_text(value[b"schema"], "descriptor schema") != DESCRIPTOR_SCHEMA:
            raise TorrentPublicationError("unsupported publication descriptor schema")
        if value[b"version"] != DESCRIPTOR_VERSION:
            raise TorrentPublicationError("unsupported publication descriptor version")
        if bytes_text(value[b"profile"], "publication profile") != PUBLICATION_PROFILE:
            raise TorrentPublicationError("unsupported publication profile")
        if bytes_text(value[b"provider"], "publication provider") != "huggingface":
            raise TorrentPublicationError("unsupported publication provider")
        repo_id = bytes_text(value[b"repo_id"], "repository id")
        repo_type = bytes_text(value[b"repo_type"], "repository type")
        resolved_commit = bytes_text(value[b"resolved_commit"], "resolved commit")
        piece_length = int(value[b"piece_length"])
        raw_files = value[b"files"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TorrentPublicationError("malformed publication descriptor") from exc
    safe_repo_path(repo_id)
    if repo_type not in {"model", "dataset", "space"}:
        raise TorrentPublicationError(f"unsupported repository type: {repo_type!r}")
    if SAFE_COMMIT.fullmatch(resolved_commit) is None:
        raise TorrentPublicationError(f"unsafe resolved commit: {resolved_commit!r}")
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_TORRENT_FILES:
        raise TorrentPublicationError("publication descriptor has an unreasonable file count")

    files = []
    seen = set()
    total_size = 0
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise TorrentPublicationError("malformed publication descriptor file row")
        allowed = {b"path", b"size", b"sha256", b"git_blob_sha1", b"lfs_sha256", b"blob_id"}
        if set(raw) - allowed:
            raise TorrentPublicationError("publication descriptor file row contains unknown fields")
        try:
            rel = validate_payload_path(bytes_text(raw[b"path"], "payload path"))
            size = int(raw[b"size"])
            sha256 = require_hex(bytes_text(raw[b"sha256"], "payload SHA-256"), 64, "payload SHA-256")
            git_blob = require_hex(
                bytes_text(raw[b"git_blob_sha1"], "payload Git blob SHA-1"),
                40,
                "payload Git blob SHA-1",
            )
            lfs = (
                require_hex(bytes_text(raw[b"lfs_sha256"], "LFS SHA-256"), 64, "LFS SHA-256")
                if b"lfs_sha256" in raw
                else None
            )
            blob = (
                require_hex(bytes_text(raw[b"blob_id"], "blob ID"), 40, "blob ID")
                if b"blob_id" in raw
                else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TorrentPublicationError("malformed publication descriptor file row") from exc
        if size < 0 or rel in seen or (lfs is None and blob is None):
            raise TorrentPublicationError(f"invalid or duplicate payload entry: {rel}")
        if lfs is not None and lfs != sha256:
            raise TorrentPublicationError(f"LFS identity does not match full-file SHA-256: {rel}")
        if lfs is None and blob != git_blob:
            raise TorrentPublicationError(f"blob identity does not match full-file Git blob SHA-1: {rel}")
        seen.add(rel)
        total_size += size
        if total_size > MAX_TORRENT_PAYLOAD_BYTES:
            raise TorrentPublicationError("publication descriptor payload size is unreasonable")
        files.append(TorrentPayloadFile(rel, size, sha256, git_blob, lfs, blob))
    validate_path_collisions(seen)
    if total_size < 1 or piece_length != select_piece_length(total_size):
        raise TorrentPublicationError("publication descriptor has a non-canonical piece length")
    return PublicationDescriptor(
        repo_id=repo_id,
        repo_type=repo_type,
        resolved_commit=resolved_commit,
        piece_length=piece_length,
        files=tuple(files),
    )


def validate_path_collisions(paths: set[str]) -> None:
    for rel in paths:
        parts = PurePosixPath(rel).parts
        for length in range(1, len(parts)):
            if PurePosixPath(*parts[:length]).as_posix() in paths:
                raise TorrentPublicationError(f"payload file/directory collision: {rel}")


def validate_v2_file_tree(value: object, descriptor: PublicationDescriptor) -> None:
    if not isinstance(value, dict):
        raise TorrentPublicationError("torrent v2 file tree is missing")
    found: dict[str, dict[bytes, object]] = {}

    def walk(node: dict, parts: tuple[str, ...]) -> None:
        for raw_name, child in node.items():
            if raw_name == b"" or not isinstance(child, dict):
                raise TorrentPublicationError("malformed torrent v2 file tree")
            name = bytes_text(raw_name, "v2 path component")
            if b"" in child:
                if set(child) != {b""} or not isinstance(child[b""], dict):
                    raise TorrentPublicationError("malformed torrent v2 file leaf")
                found[PurePosixPath(*parts, name).as_posix()] = child[b""]
            else:
                walk(child, (*parts, name))

    walk(value, ())
    if set(found) != {item.path for item in descriptor.files}:
        raise TorrentPublicationError("torrent v2 file tree does not match the descriptor")
    for item in descriptor.files:
        leaf = found[item.path]
        if leaf.get(b"length") != item.size or set(leaf) - {b"length", b"pieces root"}:
            raise TorrentPublicationError(f"invalid v2 file leaf: {item.path}")
        root = leaf.get(b"pieces root")
        if item.size == 0:
            if root is not None:
                raise TorrentPublicationError(f"empty file has a v2 pieces root: {item.path}")
        elif not isinstance(root, bytes) or len(root) != 32:
            raise TorrentPublicationError(f"file has no valid v2 pieces root: {item.path}")


def bytes_text(value: object, label: str) -> str:
    if not isinstance(value, bytes):
        raise TorrentPublicationError(f"{label} must be a byte string")
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TorrentPublicationError(f"{label} is not valid UTF-8") from exc


def external_handoff(config: Config, parsed: ParsedPublication, torrent_path: Path) -> tuple[Path, str]:
    stage_parent = staging_parent(config, parsed.artifact.infohash_v2)
    payload_root = stage_parent / parsed.root_name
    command = (
        f"model-mirror torrent import {shell_quote(str(torrent_path))} "
        f"{shell_quote(str(payload_root))}"
    )
    return stage_parent, command


def staging_parent(config: Config, infohash_v2: str) -> Path:
    return Path(config.directory) / STAGING_DIR / infohash_v2


def import_external_payload(
    config: Config,
    *,
    metainfo: bytes,
    payload_root: Path,
    seed: bool = False,
    backend_verified: bool = False,
    libtorrent_module=None,
) -> ImportResult:
    parsed = parse_publication_metainfo(metainfo, libtorrent_module=libtorrent_module)
    return finalize_payload(
        config,
        parsed,
        payload_root,
        seed=seed,
        backend_verified=backend_verified,
    )


def finalize_payload(
    config: Config,
    parsed: ParsedPublication,
    payload_root: Path,
    *,
    seed: bool,
    backend_verified: bool,
) -> ImportResult:
    descriptor = parsed.descriptor
    target = archive_path(config, descriptor.repo_id, descriptor.repo_type)
    if payload_root.name != parsed.root_name:
        raise TorrentPublicationError(
            f"downloaded payload root must be named {parsed.root_name!r}: {payload_root}"
        )
    if target.exists():
        raise TorrentPublicationError(f"canonical archive target already exists: {target}")
    validate_staged_payload(payload_root, descriptor)
    snapshot = descriptor_snapshot(descriptor)
    write_snapshot_plan(payload_root, snapshot)

    if backend_verified:
        coverage = coverage_from_metainfo(payload_root, parsed)
        write_coverage(coverage_path(payload_root, descriptor.resolved_commit), coverage)
        reread_files = 0
        reread_bytes = 0
    else:
        recorder = TorrentCoverageRecorder(payload_root, snapshot)
        for item in snapshot.files:
            path = payload_root / item.path
            accumulator = recorder.accumulator(item)
            hashes = file_hashes(path, accumulators=(accumulator,))
            recorder.record(item, path, hashes, accumulator.finalize())
        coverage = recorder.coverage
        reread_files = len(descriptor.files)
        reread_bytes = descriptor.total_size

    write_descriptor_manifest(payload_root, descriptor)
    target.parent.mkdir(parents=True, exist_ok=True)
    if payload_root.stat().st_dev != target.parent.stat().st_dev:
        raise TorrentPublicationError(
            f"staging and archive target are on different filesystems; download under "
            f"{Path(config.directory) / STAGING_DIR}"
        )
    payload_root.replace(target)
    state = VerificationState(
        status="torrent-verified",
        repo_id=descriptor.repo_id,
        repo_type=descriptor.repo_type,
        requested_revision=descriptor.resolved_commit,
        resolved_commit=descriptor.resolved_commit,
        upstream_status="unknown",
        issues=["torrent content verified; upstream provenance not independently verified"],
    )
    write_verification_state(target, state)
    write_json_atomic(
        target / ".model-mirror" / "torrent" / "import.json",
        {
            "schema": IMPORT_SCHEMA,
            "version": IMPORT_VERSION,
            "content_verification": "torrent-verified",
            "publication_trust": "trusted-infohash",
            "upstream_provenance": "not-upstream-verified",
            "upstream_availability": "unknown",
            "infohash_v1": parsed.artifact.infohash_v1,
            "infohash_v2": parsed.artifact.infohash_v2,
            "imported_at_utc": utc_now(),
        },
    )
    record = register_imported_publication(target, parsed, coverage, desired_seed=seed)
    return ImportResult(target, record, reread_files, reread_bytes)


def validate_staged_payload(root: Path, descriptor: PublicationDescriptor) -> None:
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise TorrentPublicationError(f"staged payload root is missing or unsafe: {root}")
    remove_padding_files(root, descriptor)
    expected = {item.path: item for item in descriptor.files}
    found = set()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TorrentPublicationError(f"staged payload contains a symlink: {path}")
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel not in expected:
            raise TorrentPublicationError(f"staged payload contains an unexpected file: {rel}")
        if path.stat().st_size != expected[rel].size:
            raise TorrentPublicationError(f"staged payload size mismatch: {rel}")
        found.add(rel)
    missing = sorted(set(expected) - found)
    if missing:
        raise TorrentPublicationError(f"staged payload is missing files: {missing!r}")


def remove_padding_files(root: Path, descriptor: PublicationDescriptor) -> None:
    pad_root = root / ".pad"
    if not pad_root.exists():
        return
    if not pad_root.is_dir() or pad_root.is_symlink():
        raise TorrentPublicationError("torrent padding path is unsafe")
    expected = {
        row[b"path"][1].decode("ascii"): row[b"length"]
        for row in hybrid_v1_files(descriptor)
        if row.get(b"attr") == b"p"
    }
    for path in pad_root.rglob("*"):
        if path.is_symlink() or (path.is_file() and (path.name not in expected or path.stat().st_size != expected[path.name])):
            raise TorrentPublicationError(f"unexpected torrent padding file: {path}")
    shutil.rmtree(pad_root)


def descriptor_snapshot(descriptor: PublicationDescriptor) -> HubSnapshot:
    return HubSnapshot(
        descriptor.repo_id,
        descriptor.repo_type,
        descriptor.resolved_commit,
        descriptor.resolved_commit,
        [
            HubFile(item.path, item.size, lfs_sha256=item.lfs_sha256, blob_id=item.blob_id)
            for item in descriptor.files
        ],
    )


def write_descriptor_manifest(root: Path, descriptor: PublicationDescriptor) -> None:
    manifest = {}
    for item in descriptor.files:
        stat = (root / item.path).stat()
        manifest[item.path] = {
            "path": item.path,
            "sha256": item.sha256,
            "git_blob_sha1": item.git_blob_sha1,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    write_manifest(root, manifest)


def coverage_from_metainfo(root: Path, parsed: ParsedPublication) -> TorrentCoverage:
    descriptor = parsed.descriptor
    info = parsed.metainfo_tree[b"info"]
    pieces = info[b"pieces"]
    if not isinstance(pieces, bytes) or len(pieces) % 20:
        raise TorrentPublicationError("torrent v1 piece hashes are malformed")
    v1_hashes = [pieces[offset : offset + 20] for offset in range(0, len(pieces), 20)]
    piece_layers = parsed.metainfo_tree.get(b"piece layers", {})
    if not isinstance(piece_layers, dict):
        raise TorrentPublicationError("torrent v2 piece layers are malformed")
    coverage = TorrentCoverage(
        repo_id=descriptor.repo_id,
        repo_type=descriptor.repo_type,
        resolved_commit=descriptor.resolved_commit,
        piece_length=descriptor.piece_length,
    )
    cursor = 0
    for item in descriptor.files:
        count = (item.size + descriptor.piece_length - 1) // descriptor.piece_length
        selected_v1 = v1_hashes[cursor : cursor + count]
        if len(selected_v1) != count:
            raise TorrentPublicationError("torrent v1 piece hashes do not cover the descriptor")
        cursor += count
        root_hash = v2_file_root(info[b"file tree"], item.path)
        if item.size == 0:
            v2_hashes = []
        elif item.size <= descriptor.piece_length:
            v2_hashes = [root_hash]
        else:
            layer = piece_layers.get(root_hash)
            if not isinstance(layer, bytes) or len(layer) != count * 32:
                raise TorrentPublicationError(f"torrent v2 piece layer is incomplete: {item.path}")
            v2_hashes = [layer[offset : offset + 32] for offset in range(0, len(layer), 32)]
        stat = (root / item.path).stat()
        coverage.files[item.path] = TorrentCoverageFile(
            path=item.path,
            size=item.size,
            mtime_ns=stat.st_mtime_ns,
            sha256=item.sha256,
            git_blob_sha1=item.git_blob_sha1,
            lfs_sha256=item.lfs_sha256,
            blob_id=item.blob_id,
            v1_piece_hashes=tuple(value.hex() for value in selected_v1),
            v2_piece_hashes=tuple(value.hex() for value in v2_hashes),
            v2_file_root=root_hash.hex() if root_hash is not None else None,
        )
    if cursor != len(v1_hashes):
        raise TorrentPublicationError("torrent has unexpected extra v1 piece hashes")
    return coverage


def v2_file_root(tree: dict, rel: str) -> bytes | None:
    node = tree
    for part in PurePosixPath(rel).parts:
        node = node[part.encode("utf-8")]
    root = node[b""].get(b"pieces root")
    return root


def register_imported_publication(
    root: Path,
    parsed: ParsedPublication,
    coverage: TorrentCoverage,
    *,
    desired_seed: bool,
) -> PublicationRecord:
    descriptor = parsed.descriptor
    target_dir = publication_dir(root, descriptor.resolved_commit, descriptor.profile)
    torrent_path = target_dir / f"{root.name}@{descriptor.resolved_commit}.torrent"
    recovery_path = target_dir / "recovery.json"
    record_path = publication_record_path(root, descriptor.resolved_commit, descriptor.profile)
    now = utc_now()
    record = PublicationRecord(
        repo_id=descriptor.repo_id,
        repo_type=descriptor.repo_type,
        resolved_commit=descriptor.resolved_commit,
        profile=descriptor.profile,
        descriptor_sha256=parsed.artifact.descriptor_sha256,
        metainfo_sha256=parsed.artifact.metainfo_sha256,
        infohash_v1=parsed.artifact.infohash_v1,
        infohash_v2=parsed.artifact.infohash_v2,
        magnet_uri=parsed.artifact.magnet_uri,
        torrent_path=torrent_path.relative_to(root).as_posix(),
        recovery_path=recovery_path.relative_to(root).as_posix(),
        payload_fingerprints={
            rel: {"size": row.size, "mtime_ns": row.mtime_ns}
            for rel, row in coverage.files.items()
        },
        desired_seed=desired_seed,
        observed_backend="pending" if desired_seed else "stopped",
        content_verification="torrent-verified",
        publication_trust="trusted-infohash",
        upstream_provenance="not-upstream-verified",
        upstream_availability="unknown",
        created_at_utc=now,
        updated_at_utc=now,
    )
    write_bytes_atomic(torrent_path, parsed.artifact.metainfo)
    write_json_atomic(recovery_path, recovery_value(record))
    write_json_atomic(record_path, record.to_dict())
    write_json_atomic(
        fence_path(root),
        {
            "schema": FENCE_SCHEMA,
            "version": FENCE_VERSION,
            "publication_id": record.publication_id,
            "publication_record": record_path.relative_to(root).as_posix(),
        },
    )
    return record


def join_torrent(
    config: Config,
    source: str,
    *,
    seed: bool = False,
    metadata_timeout_seconds: float = 120.0,
    poll_seconds: float = 0.5,
    libtorrent_module=None,
    session=None,
    on_progress=None,
) -> ImportResult:
    if metadata_timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("join timeouts must be positive")
    lt = libtorrent_module or load_libtorrent()
    session = session or lt.session()
    source_path = Path(source)
    if source_path.is_file():
        metainfo = source_path.read_bytes()
        parsed = parse_publication_metainfo(metainfo, libtorrent_module=lt)
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(metainfo)
        stage_parent = staging_parent(config, parsed.artifact.infohash_v2)
    elif source.startswith("magnet:?"):
        params = lt.parse_magnet_uri(source)
        stage_parent = Path(config.directory) / STAGING_DIR / magnet_staging_key(params, source)
        parsed = None
        metainfo = None
    else:
        raise TorrentPublicationError(f"torrent source is not a file or magnet URI: {source}")
    stage_parent.mkdir(parents=True, exist_ok=True)
    params.save_path = str(stage_parent)
    handle = session.add_torrent(params)
    started = time.monotonic()
    last_report = -1
    while parsed is None:
        status = handle.status()
        if status.has_metadata:
            metainfo = metainfo_from_handle(handle, lt)
            parsed = parse_publication_metainfo(metainfo, libtorrent_module=lt)
            break
        if status.error:
            raise TorrentPublicationError(f"torrent metadata acquisition failed: {status.error}")
        if time.monotonic() - started >= metadata_timeout_seconds:
            raise TorrentPublicationError("timed out waiting for torrent metadata")
        time.sleep(poll_seconds)

    while True:
        status = handle.status()
        progress = int(status.progress * 1000)
        if on_progress is not None and progress != last_report:
            on_progress(status)
            last_report = progress
        if status.error:
            raise TorrentPublicationError(f"torrent download failed: {status.error}")
        if status.is_seeding or status.is_finished:
            break
        time.sleep(poll_seconds)
    session.remove_torrent(handle)
    payload_root = stage_parent / parsed.root_name
    return import_external_payload(
        config,
        metainfo=metainfo,
        payload_root=payload_root,
        seed=seed,
        backend_verified=True,
        libtorrent_module=lt,
    )


def magnet_staging_key(params, source: str) -> str:
    info_hashes = params.info_hashes
    if info_hashes.has_v2():
        return str(info_hashes.v2)
    if info_hashes.has_v1():
        return str(info_hashes.v1)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def metainfo_from_handle(handle, lt) -> bytes:
    creator = lt.create_torrent(handle.torrent_file())
    tree = creator.generate()
    tree.pop(b"creation date", None)
    raw_info = tree.get(b"info")
    if isinstance(raw_info, tuple):
        raw_bytes = bytes(value % 256 for value in raw_info)
        tree[b"info"] = lt.bdecode(raw_bytes)
    if not isinstance(tree.get(b"info"), dict):
        raise TorrentPublicationError("torrent backend returned malformed metadata")
    return bytes(lt.bencode(tree))


def shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
