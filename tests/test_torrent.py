import builtins
import dataclasses
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import libtorrent as lt
import pytest

from model_mirror.checksums import load_manifest, write_checksums, write_manifest
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.state import VerificationState, write_verification_state
from model_mirror.torrent import (
    DESCRIPTOR_INFO_KEY,
    MAX_PIECE_LENGTH,
    MIN_PIECE_LENGTH,
    TARGET_PIECES,
    TorrentBackendUnavailable,
    TorrentPublicationError,
    build_publication_descriptor,
    create_hybrid_metainfo,
    create_hybrid_metainfo_from_coverage,
    insert_file_tree_leaf,
    load_libtorrent,
    optional_hex,
    require_hex,
    select_piece_length,
    validate_payload_path,
    verified_seed_params,
    validate_coverage_for_descriptor,
)
from model_mirror.torrent_coverage import load_coverage, upgrade_coverage


def git_blob_sha1(payload: bytes) -> str:
    return hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()


def prepared_archive(
    base: Path,
    *,
    commit: str = "a" * 40,
    payloads: dict[str, bytes] | None = None,
) -> Path:
    root = base / "models" / "org" / "model"
    root.mkdir(parents=True)
    selected = payloads or {
        "README.md": b"readme",
        "sub/weights.bin": b"weights" * 40000,
    }
    files = []
    for rel, payload in selected.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        if rel.endswith(".md"):
            files.append(HubFile(rel, len(payload), blob_id=git_blob_sha1(payload)))
        else:
            files.append(
                HubFile(
                    rel,
                    len(payload),
                    lfs_sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
    write_checksums(root)
    write_snapshot_plan(root, HubSnapshot("org/model", "model", "main", commit, files))
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id="org/model",
            repo_type="model",
            resolved_commit=commit,
            upstream_commit=commit,
            upstream_status="current",
        ),
    )
    return root


def test_descriptor_and_hybrid_metainfo_are_deterministic_and_client_compatible(tmp_path):
    first = prepared_archive(tmp_path / "first")
    second = prepared_archive(tmp_path / "second")
    os.utime(second / "README.md", ns=(1_000_000_000, 1_000_000_000))
    write_checksums(second)

    first_descriptor = build_publication_descriptor(first, repo_id="org/model", repo_type="model")
    second_descriptor = build_publication_descriptor(second, repo_id="org/model", repo_type="model")
    first_artifact = create_hybrid_metainfo(first, first_descriptor)
    second_artifact = create_hybrid_metainfo(second, second_descriptor)

    assert [item.path for item in first_descriptor.files] == ["README.md", "sub/weights.bin"]
    assert first_descriptor.total_size == sum(item.size for item in first_descriptor.files)
    assert first_descriptor.bencode_value() == second_descriptor.bencode_value()
    assert first_artifact == second_artifact
    assert len(first_artifact.infohash_v1) == 40
    assert len(first_artifact.infohash_v2) == 64
    assert first_artifact.metainfo_sha256 == hashlib.sha256(first_artifact.metainfo).hexdigest()
    assert "urn:btih:" in first_artifact.magnet_uri
    assert "urn:btmh:" in first_artifact.magnet_uri

    decoded = lt.bdecode(first_artifact.metainfo)
    assert b"creation date" not in decoded
    descriptor = decoded[b"info"][DESCRIPTOR_INFO_KEY]
    assert descriptor[b"repo_id"] == b"org/model"
    assert descriptor[b"resolved_commit"] == b"a" * 40
    assert descriptor[b"files"][0][b"path"] == b"README.md"
    torrent_info = lt.torrent_info(first_artifact.metainfo)
    assert torrent_info.info_hashes().has_v1()
    assert torrent_info.info_hashes().has_v2()

    params = verified_seed_params(first_artifact.metainfo, first)
    assert params.save_path == str(first.parent)
    assert params.ti.name() == "model"
    assert params.have_pieces == [True] * params.ti.num_pieces()
    assert params.verified_pieces == [True] * params.ti.num_pieces()
    assert not params.flags & lt.torrent_flags.paused
    assert not params.flags & lt.torrent_flags.auto_managed

    wrong_root = first.parent / "different-name"
    with pytest.raises(TorrentPublicationError, match="payload root mismatch"):
        verified_seed_params(first_artifact.metainfo, wrong_root)


def test_commit_is_part_of_torrent_identity(tmp_path):
    first = prepared_archive(tmp_path / "first", commit="a" * 40)
    second = prepared_archive(tmp_path / "second", commit="c" * 40)

    first_artifact = create_hybrid_metainfo(
        first,
        build_publication_descriptor(first, repo_id="org/model", repo_type="model"),
    )
    second_artifact = create_hybrid_metainfo(
        second,
        build_publication_descriptor(second, repo_id="org/model", repo_type="model"),
    )

    assert first_artifact.infohash_v1 != second_artifact.infohash_v1
    assert first_artifact.infohash_v2 != second_artifact.infohash_v2


@pytest.mark.parametrize(
    "payloads",
    [
        {"one.bin": b"x"},
        {
            "a-empty.bin": b"",
            "b-small.bin": b"small",
            "nested/large.bin": bytes(range(251)) * 9000,
        },
    ],
)
def test_coverage_builder_exactly_matches_independent_libtorrent_reread(tmp_path, payloads):
    root = prepared_archive(tmp_path, payloads=payloads)
    descriptor = build_publication_descriptor(root, repo_id="org/model", repo_type="model")
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        descriptor.resolved_commit,
        [
            HubFile(
                item.path,
                item.size,
                lfs_sha256=item.lfs_sha256,
                blob_id=item.blob_id,
            )
            for item in descriptor.files
        ],
    )
    result = upgrade_coverage(root, snapshot)

    assert result.complete
    assert result.hashed_files == len(payloads)
    assert create_hybrid_metainfo_from_coverage(
        root,
        descriptor,
        __import__(
            "model_mirror.torrent_coverage",
            fromlist=["load_coverage"],
        ).load_coverage(result.path),
    ) == create_hybrid_metainfo(root, descriptor)


def test_descriptor_requires_matching_clean_pinned_state(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(TorrentPublicationError, match="verification state is missing"):
        build_publication_descriptor(missing, repo_id="org/model", repo_type="model")

    dirty = prepared_archive(tmp_path / "dirty")
    write_verification_state(dirty, VerificationState(status="dirty", repo_id="org/model"))
    with pytest.raises(TorrentPublicationError, match="mirror is not clean"):
        build_publication_descriptor(dirty, repo_id="org/model", repo_type="model")

    wrong_identity = prepared_archive(tmp_path / "identity")
    with pytest.raises(TorrentPublicationError, match="verification identity mismatch"):
        build_publication_descriptor(wrong_identity, repo_id="other/model", repo_type="model")

    invalid_commit = prepared_archive(tmp_path / "commit")
    write_verification_state(
        invalid_commit,
        VerificationState(status="clean", repo_id="org/model", resolved_commit="../bad"),
    )
    with pytest.raises(TorrentPublicationError, match="invalid resolved commit"):
        build_publication_descriptor(invalid_commit, repo_id="org/model", repo_type="model")

    no_snapshot = prepared_archive(tmp_path / "no-snapshot")
    (no_snapshot / ".model-mirror" / "snapshot.json").unlink()
    with pytest.raises(TorrentPublicationError, match="pinned snapshot is missing"):
        build_publication_descriptor(no_snapshot, repo_id="org/model", repo_type="model")

    wrong_snapshot = prepared_archive(tmp_path / "wrong-snapshot")
    write_snapshot_plan(wrong_snapshot, HubSnapshot("org/model", "model", "main", "d" * 40, []))
    with pytest.raises(TorrentPublicationError, match="does not match"):
        build_publication_descriptor(wrong_snapshot, repo_id="org/model", repo_type="model")


def test_descriptor_rejects_ambiguous_or_unverified_payload_rows(tmp_path):
    duplicate = prepared_archive(tmp_path / "duplicate", payloads={"file.bin": b"x"})
    file = HubFile("file.bin", 1, lfs_sha256=hashlib.sha256(b"x").hexdigest())
    write_snapshot_plan(duplicate, HubSnapshot("org/model", "model", "main", "a" * 40, [file, file]))
    with pytest.raises(TorrentPublicationError, match="duplicate payload"):
        build_publication_descriptor(duplicate, repo_id="org/model", repo_type="model")

    invalid_size = prepared_archive(tmp_path / "invalid-size", payloads={"file.bin": b"x"})
    write_snapshot_plan(
        invalid_size,
        HubSnapshot("org/model", "model", "main", "a" * 40, [HubFile("file.bin", None, blob_id="a" * 40)]),
    )
    with pytest.raises(TorrentPublicationError, match="invalid expected size"):
        build_publication_descriptor(invalid_size, repo_id="org/model", repo_type="model")

    missing_file = prepared_archive(tmp_path / "missing-file", payloads={"file.bin": b"x"})
    (missing_file / "file.bin").unlink()
    with pytest.raises(TorrentPublicationError, match="payload file is missing"):
        build_publication_descriptor(missing_file, repo_id="org/model", repo_type="model")

    stale = prepared_archive(tmp_path / "stale", payloads={"file.bin": b"x"})
    os.utime(stale / "file.bin", ns=(2_000_000_000, 2_000_000_000))
    with pytest.raises(TorrentPublicationError, match="manifest record is missing or stale"):
        build_publication_descriptor(stale, repo_id="org/model", repo_type="model")

    wrong_size = prepared_archive(tmp_path / "wrong-size", payloads={"file.bin": b"x"})
    write_snapshot_plan(
        wrong_size,
        HubSnapshot(
            "org/model",
            "model",
            "main",
            "a" * 40,
            [HubFile("file.bin", 2, lfs_sha256=hashlib.sha256(b"x").hexdigest())],
        ),
    )
    with pytest.raises(TorrentPublicationError, match="size mismatch"):
        build_publication_descriptor(wrong_size, repo_id="org/model", repo_type="model")

    malformed_hash = prepared_archive(tmp_path / "malformed", payloads={"file.bin": b"x"})
    manifest = load_manifest(malformed_hash)
    manifest["file.bin"]["sha256"] = "not-a-hash"
    write_manifest(malformed_hash, manifest)
    with pytest.raises(TorrentPublicationError, match="invalid manifest SHA-256"):
        build_publication_descriptor(malformed_hash, repo_id="org/model", repo_type="model")


def test_descriptor_rejects_missing_or_mismatched_upstream_identity(tmp_path):
    missing_identity = prepared_archive(tmp_path / "missing-identity", payloads={"file.bin": b"x"})
    write_snapshot_plan(
        missing_identity,
        HubSnapshot("org/model", "model", "main", "a" * 40, [HubFile("file.bin", 1)]),
    )
    with pytest.raises(TorrentPublicationError, match="content identity is missing"):
        build_publication_descriptor(missing_identity, repo_id="org/model", repo_type="model")

    bad_lfs = prepared_archive(tmp_path / "bad-lfs", payloads={"file.bin": b"x"})
    write_snapshot_plan(
        bad_lfs,
        HubSnapshot(
            "org/model",
            "model",
            "main",
            "a" * 40,
            [HubFile("file.bin", 1, lfs_sha256="0" * 64)],
        ),
    )
    with pytest.raises(TorrentPublicationError, match="does not match upstream LFS"):
        build_publication_descriptor(bad_lfs, repo_id="org/model", repo_type="model")

    bad_blob = prepared_archive(tmp_path / "bad-blob", payloads={"README.md": b"x"})
    write_snapshot_plan(
        bad_blob,
        HubSnapshot(
            "org/model",
            "model",
            "main",
            "a" * 40,
            [HubFile("README.md", 1, blob_id="0" * 40)],
        ),
    )
    with pytest.raises(TorrentPublicationError, match="does not match upstream identity"):
        build_publication_descriptor(bad_blob, repo_id="org/model", repo_type="model")

    malformed_upstream = prepared_archive(tmp_path / "malformed-upstream", payloads={"file.bin": b"x"})
    write_snapshot_plan(
        malformed_upstream,
        HubSnapshot(
            "org/model",
            "model",
            "main",
            "a" * 40,
            [HubFile("file.bin", 1, lfs_sha256="invalid")],
        ),
    )
    with pytest.raises(TorrentPublicationError, match="invalid LFS SHA-256"):
        build_publication_descriptor(malformed_upstream, repo_id="org/model", repo_type="model")


def test_payload_path_and_empty_archive_validation(tmp_path):
    assert validate_payload_path("sub/file.bin") == "sub/file.bin"
    for unsafe in ("", "a\\b", "a\x00b", "/absolute", "../escape", "a/./b", "a//b", ".model-mirror/x"):
        with pytest.raises(TorrentPublicationError, match="unsafe|conflicts"):
            validate_payload_path(unsafe)

    symlinked = prepared_archive(tmp_path / "symlink", payloads={"target.bin": b"x"})
    (symlinked / "target.bin").rename(symlinked / "real.bin")
    (symlinked / "target.bin").symlink_to("real.bin")
    with pytest.raises(TorrentPublicationError, match="contains a symlink"):
        build_publication_descriptor(symlinked, repo_id="org/model", repo_type="model")

    empty = prepared_archive(tmp_path / "empty", payloads={"empty.bin": b""})
    with pytest.raises(TorrentPublicationError, match="non-empty payload"):
        build_publication_descriptor(empty, repo_id="org/model", repo_type="model")


def test_piece_selection_and_hash_parsing_helpers():
    assert select_piece_length(1) == MIN_PIECE_LENGTH
    assert select_piece_length(TARGET_PIECES * MIN_PIECE_LENGTH) == MIN_PIECE_LENGTH
    assert select_piece_length(TARGET_PIECES * MIN_PIECE_LENGTH + 1) == 2 * MIN_PIECE_LENGTH
    assert select_piece_length(TARGET_PIECES * MAX_PIECE_LENGTH * 2) == MAX_PIECE_LENGTH
    with pytest.raises(ValueError, match="positive"):
        select_piece_length(0)

    assert require_hex("A" * 40, 40, "test hash") == "a" * 40
    assert optional_hex(None, 40, "test hash") is None
    assert optional_hex("", 40, "test hash") is None
    for invalid in ("g" * 40, "a" * 39):
        with pytest.raises(TorrentPublicationError, match="invalid test hash"):
            require_hex(invalid, 40, "test hash")


def test_backend_errors_are_actionable(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def missing_libtorrent(name, *args, **kwargs):
        if name == "libtorrent":
            raise ModuleNotFoundError("missing", name="libtorrent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_libtorrent)
    with pytest.raises(TorrentBackendUnavailable, match="torrent.*extra"):
        load_libtorrent()
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert load_libtorrent() is lt

    root = prepared_archive(tmp_path / "not-hybrid")
    descriptor = build_publication_descriptor(root, repo_id="org/model", repo_type="model")

    class NonHybridTorrentInfo:
        def __init__(self, metainfo):
            self.inner = lt.torrent_info(metainfo)

        def info_hashes(self):
            return SimpleNamespace(has_v1=lambda: True, has_v2=lambda: False)

    fake_libtorrent = SimpleNamespace(
        file_storage=lt.file_storage,
        create_torrent=lt.create_torrent,
        set_piece_hashes=lt.set_piece_hashes,
        bencode=lt.bencode,
        torrent_info=NonHybridTorrentInfo,
    )
    with pytest.raises(TorrentPublicationError, match="did not produce a hybrid"):
        create_hybrid_metainfo(root, descriptor, libtorrent_module=fake_libtorrent)


def test_coverage_builder_rejects_identity_file_set_stale_rows_and_tree_collisions(tmp_path):
    root = prepared_archive(tmp_path)
    descriptor = build_publication_descriptor(root, repo_id="org/model", repo_type="model")
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        descriptor.resolved_commit,
        [
            HubFile(item.path, item.size, lfs_sha256=item.lfs_sha256, blob_id=item.blob_id)
            for item in descriptor.files
        ],
    )
    result = upgrade_coverage(root, snapshot)
    coverage = load_coverage(result.path)

    coverage.repo_id = "other/model"
    with pytest.raises(TorrentPublicationError, match="does not match"):
        validate_coverage_for_descriptor(root, descriptor, coverage)
    coverage.repo_id = "org/model"

    removed = coverage.files.pop("README.md")
    with pytest.raises(TorrentPublicationError, match="file set"):
        validate_coverage_for_descriptor(root, descriptor, coverage)
    coverage.files["README.md"] = removed

    coverage.files["README.md"] = dataclasses.replace(removed, mtime_ns=0)
    with pytest.raises(TorrentPublicationError, match="stale"):
        validate_coverage_for_descriptor(root, descriptor, coverage)

    tree = {}
    insert_file_tree_leaf(tree, [b"a"], {})
    with pytest.raises(TorrentPublicationError, match="collide"):
        insert_file_tree_leaf(tree, [b"a"], {})
    with pytest.raises(TorrentPublicationError, match="collide"):
        insert_file_tree_leaf(tree, [b"a", b"child"], {})
