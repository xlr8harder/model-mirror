import dataclasses
import hashlib
import json
import os

import pytest

from model_mirror.checksums import FileHashes, file_hashes
from model_mirror.hub import HubFile, HubSnapshot
from model_mirror.torrent import PUBLICATION_PROFILE, TorrentPublicationError
from model_mirror.torrent_coverage import (
    COVERAGE_SCHEMA,
    COVERAGE_VERSION,
    TorrentCoverage,
    TorrentCoverageFile,
    TorrentCoverageRecorder,
    coverage_hex,
    coverage_path,
    coverage_row_is_current,
    ensure_upstream_hashes,
    load_coverage,
    optional_coverage_hex,
    require_metadata_size,
    snapshot_files,
    upgrade_coverage,
    write_coverage,
)


COMMIT = "a" * 40


def git_blob(payload):
    return hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()


def coverage_fixture(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    payloads = {"a.bin": b"a" * 17, "empty.txt": b""}
    files = []
    for rel, payload in payloads.items():
        (root / rel).write_bytes(payload)
        if rel.endswith(".bin"):
            files.append(HubFile(rel, len(payload), lfs_sha256=hashlib.sha256(payload).hexdigest()))
        else:
            files.append(HubFile(rel, len(payload), blob_id=git_blob(payload)))
    snapshot = HubSnapshot("org/model", "model", "main", COMMIT, files)
    return root, snapshot


def test_upgrade_is_complete_resumable_and_invalidates_changed_fingerprints(tmp_path):
    root, snapshot = coverage_fixture(tmp_path)
    progress = []

    dry = upgrade_coverage(root, snapshot, dry_run=True)
    assert dry.dry_run and dry.hashed_files == 2 and dry.hashed_bytes == 17 and not dry.complete

    first = upgrade_coverage(
        root,
        snapshot,
        on_progress=lambda rel, done, total: progress.append((rel, done, total)),
    )
    assert first.complete and first.hashed_files == 2 and first.covered_files == 2
    assert progress[-1] == ("empty.txt", 0, 0) or progress[-1][0] == "a.bin"
    coverage = load_coverage(first.path)
    assert coverage.to_dict()["schema"] == COVERAGE_SCHEMA
    assert TorrentCoverageFile.from_dict(
        coverage.files["a.bin"].to_dict(),
        source=first.path,
    ) == coverage.files["a.bin"]

    second = upgrade_coverage(root, snapshot)
    assert second.complete and second.hashed_files == 0

    os.utime(root / "a.bin", ns=(2_000_000_000, 2_000_000_000))
    stale = TorrentCoverageRecorder(root, snapshot)
    assert stale.missing_paths() == ["a.bin"]
    refreshed = upgrade_coverage(root, snapshot)
    assert refreshed.hashed_files == 1 and refreshed.complete


def test_recorder_validates_identity_sizes_and_upstream_hashes(tmp_path):
    root, snapshot = coverage_fixture(tmp_path)
    recorder = TorrentCoverageRecorder(root, snapshot)
    item = snapshot.files[0]
    accumulator = recorder.accumulator(item)
    hashes = file_hashes(root / item.path, accumulators=(accumulator,))
    row = recorder.record(item, root / item.path, hashes, accumulator.finalize())
    assert coverage_row_is_current(row, item, root / item.path, recorder.piece_length)

    bad_size = HubFile(item.path, item.size + 1, lfs_sha256=item.lfs_sha256)
    with pytest.raises(TorrentPublicationError, match="expected"):
        recorder.record(bad_size, root / item.path, hashes, accumulator.finalize())
    with pytest.raises(TorrentPublicationError, match="SHA-256 mismatch"):
        ensure_upstream_hashes(
            item,
            FileHashes("0" * 64, hashes.git_blob_sha1),
        )
    blob_item = snapshot.files[1]
    with pytest.raises(TorrentPublicationError, match="Git blob mismatch"):
        ensure_upstream_hashes(blob_item, FileHashes("0" * 64, "0" * 40))
    with pytest.raises(TorrentPublicationError, match="identity is missing"):
        ensure_upstream_hashes(HubFile("x", 1), hashes)

    wrong = TorrentCoverage("other/model", "model", COMMIT, recorder.piece_length)
    write_coverage(recorder.path, wrong)
    with pytest.raises(ValueError, match="identity mismatch"):
        TorrentCoverageRecorder(root, snapshot)


def test_coverage_metadata_and_snapshot_validation_errors(tmp_path):
    root, snapshot = coverage_fixture(tmp_path)
    with pytest.raises(TorrentPublicationError, match="non-empty"):
        TorrentCoverageRecorder(root, HubSnapshot("x/y", "model", "main", COMMIT, []))
    with pytest.raises(TorrentPublicationError, match="non-empty"):
        TorrentCoverageRecorder(
            root,
            HubSnapshot("x/y", "model", "main", COMMIT, [HubFile("empty.txt", 0, blob_id=git_blob(b""))]),
        )
    with pytest.raises(TorrentPublicationError, match="duplicate"):
        snapshot_files(HubSnapshot("x/y", "model", "main", COMMIT, [snapshot.files[0], snapshot.files[0]]))
    for size in (None, -1):
        with pytest.raises(TorrentPublicationError, match="invalid expected size"):
            require_metadata_size(HubFile("x", size, blob_id="a" * 40))
    with pytest.raises(TorrentPublicationError, match="invalid resolved commit"):
        coverage_path(root, "../bad")

    path = coverage_path(root, COMMIT)
    malformed_values = [
        "{",
        "[]",
        json.dumps({"schema": "wrong", "version": 1}),
        json.dumps({"schema": COVERAGE_SCHEMA, "version": 99}),
        json.dumps({"schema": COVERAGE_SCHEMA, "version": 1, "files": []}),
        json.dumps({"schema": COVERAGE_SCHEMA, "version": 1, "files": {}, "repo_id": "x"}),
    ]
    path.parent.mkdir(parents=True)
    for value in malformed_values:
        path.write_text(value)
        with pytest.raises(ValueError):
            load_coverage(path)

    base = {
        "schema": COVERAGE_SCHEMA,
        "version": COVERAGE_VERSION,
        "profile": PUBLICATION_PROFILE,
        "repo_id": "org/model",
        "repo_type": "model",
        "resolved_commit": COMMIT,
        "piece_length": 1024 * 1024,
        "files": {},
    }
    valid_row = TorrentCoverageFile(
        "a.bin",
        1,
        1,
        "a" * 64,
        "b" * 40,
        "a" * 64,
        None,
        ("c" * 40,),
        ("d" * 64,),
        "e" * 64,
    ).to_dict()
    for row in (None, {}, {**valid_row, "sha256": "bad"}, {**valid_row, "size": -1}):
        value = dict(base)
        value["files"] = {"a.bin": row}
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            load_coverage(path)
    mismatch = dict(base)
    mismatch["files"] = {"wrong": valid_row}
    path.write_text(json.dumps(mismatch))
    with pytest.raises(ValueError, match="file map"):
        load_coverage(path)


def test_coverage_currentness_branches_and_helpers(tmp_path):
    root, snapshot = coverage_fixture(tmp_path)
    result = upgrade_coverage(root, snapshot)
    recorder = TorrentCoverageRecorder(root, snapshot)
    row = recorder.coverage.files["a.bin"]
    item = snapshot.files[0]
    path = root / "a.bin"

    assert coverage_hex("A" * 40, 40, "hash", result.path) == "a" * 40
    assert optional_coverage_hex(None, 40, "hash", result.path) is None
    assert optional_coverage_hex("", 40, "hash", result.path) is None
    with pytest.raises(ValueError, match="Invalid"):
        coverage_hex("x", 40, "hash", result.path)

    assert not coverage_row_is_current(None, item, path, recorder.piece_length)
    for changed in (
        dataclasses.replace(row, path="other"),
        dataclasses.replace(row, size=99),
        dataclasses.replace(row, mtime_ns=0),
        dataclasses.replace(row, v1_piece_hashes=()),
        dataclasses.replace(row, v2_piece_hashes=()),
        dataclasses.replace(row, v2_file_root=None),
    ):
        assert not coverage_row_is_current(changed, item, path, recorder.piece_length)
    blob_item = snapshot.files[1]
    blob_row = recorder.coverage.files["empty.txt"]
    assert coverage_row_is_current(blob_row, blob_item, root / "empty.txt", recorder.piece_length)
    assert not coverage_row_is_current(
        dataclasses.replace(blob_row, blob_id="0" * 40),
        blob_item,
        root / "empty.txt",
        recorder.piece_length,
    )
    assert not coverage_row_is_current(
        row,
        HubFile("a.bin", item.size),
        path,
        recorder.piece_length,
    )

    (root / "a.bin").unlink()
    with pytest.raises(TorrentPublicationError, match="payload file is missing"):
        upgrade_coverage(root, snapshot)
