import hashlib
import json
import os
import threading
import time

import pytest

from model_mirror.checksums import write_checksums
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.state import VerificationState, write_verification_state
from model_mirror.torrent import PUBLICATION_PROFILE, TorrentPublicationError
from model_mirror.torrent_publication import (
    FENCE_SCHEMA,
    FENCE_VERSION,
    PublicationRecord,
    assert_commit_update_allowed,
    begin_maintenance,
    create_publication,
    fence_path,
    finish_maintenance,
    load_fenced_publication,
    load_publication,
    payload_fingerprints_match,
    publication_record_path,
    recovery_value,
    require_digest,
    retire_publication,
    set_seed_desired,
    update_observed_backend,
    wait_for_maintenance_detach,
    write_json_atomic,
)


COMMIT = "a" * 40


def git_blob(payload):
    return hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()


def prepared_archive(tmp_path, *, commit=COMMIT):
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    payloads = {"README.md": b"hello", "weights.bin": b"w" * 20000}
    files = []
    for rel, payload in payloads.items():
        (root / rel).write_bytes(payload)
        if rel.endswith(".md"):
            files.append(HubFile(rel, len(payload), blob_id=git_blob(payload)))
        else:
            files.append(HubFile(rel, len(payload), lfs_sha256=hashlib.sha256(payload).hexdigest()))
    write_checksums(root)
    write_snapshot_plan(root, HubSnapshot("org/model", "model", "main", commit, files))
    write_verification_state(
        root,
        VerificationState(
            "clean",
            "org/model",
            resolved_commit=commit,
            upstream_commit=commit,
            upstream_status="current",
        ),
    )
    return root


def test_publication_lifecycle_is_idempotent_and_fences_updates(tmp_path):
    root = prepared_archive(tmp_path)
    first = create_publication(root, repo_id="org/model", repo_type="model")
    assert first.created and first.coverage_hashed_files == 2
    assert first.record.publication_id == f"huggingface:model:org/model@{COMMIT}"
    assert first.record.active
    assert json.loads(first.recovery_path.read_text()) == recovery_value(first.record)
    fenced, path = load_fenced_publication(root)
    assert path == first.record_path and fenced.infohash_v2 == first.record.infohash_v2
    assert load_publication(first.record_path).to_dict() == first.record.to_dict()

    second = create_publication(root, repo_id="org/model", repo_type="model")
    assert not second.created and second.coverage_hashed_files == 0
    assert second.torrent_path.read_bytes() == first.torrent_path.read_bytes()
    assert_commit_update_allowed(root, COMMIT)
    with pytest.raises(TorrentPublicationError, match="update blocked"):
        assert_commit_update_allowed(root, "b" * 40)

    record = set_seed_desired(root, desired=True)
    assert record.desired_seed and record.observed_backend == "pending"
    record = update_observed_backend(root, state="seeding", detail="ok")
    assert record.observed_backend == "seeding" and record.observed_detail == "ok"
    with pytest.raises(TorrentPublicationError, match="cannot retire"):
        retire_publication(root)
    record = begin_maintenance(root)
    assert record.lifecycle == "maintenance" and record.maintenance_resume_seed
    assert record.observed_backend == "stopping"
    with pytest.raises(TorrentPublicationError, match="did not detach"):
        wait_for_maintenance_detach(root, timeout_seconds=0)
    update_observed_backend(root, state="stopped")
    wait_for_maintenance_detach(root, timeout_seconds=0)
    assert begin_maintenance(root).maintenance_resume_seed
    record = finish_maintenance(root, healthy=True)
    assert record.lifecycle == "published" and record.desired_seed and record.observed_backend == "pending"

    record = begin_maintenance(root)
    update_observed_backend(root, state="stopped")
    record = finish_maintenance(root, healthy=False)
    assert record.lifecycle == "unhealthy" and not record.desired_seed
    assert "did not finish cleanly" in record.observed_detail

    record = retire_publication(root)
    assert not record.active and not fence_path(root).exists()
    assert load_fenced_publication(root) is None
    assert_commit_update_allowed(root, "b" * 40)
    with pytest.raises(TorrentPublicationError, match="no active"):
        retire_publication(root)


def test_external_mode_stop_fingerprints_and_reactivation(tmp_path):
    root = prepared_archive(tmp_path)
    result = create_publication(
        root,
        repo_id="org/model",
        repo_type="model",
        desired_seed=False,
        client_mode="external",
    )
    assert result.record.client_mode == "external"
    assert result.record.observed_backend == "external"
    with pytest.raises(TorrentPublicationError, match="external torrent client"):
        begin_maintenance(root)

    stopped = set_seed_desired(root, desired=False)
    assert stopped.observed_backend == "stopped"
    assert begin_maintenance(root).lifecycle == "maintenance"
    assert finish_maintenance(root, healthy=True).observed_backend == "stopped"

    record = set_seed_desired(root, desired=True, client_mode="external")
    assert record.observed_backend == "external"
    record = set_seed_desired(root, desired=True, client_mode="managed")
    assert record.observed_backend == "pending"
    record = set_seed_desired(root, desired=False)
    assert record.observed_backend == "stopped"
    with pytest.raises(ValueError, match="unsupported"):
        set_seed_desired(root, desired=True, client_mode="bad")

    record = load_fenced_publication(root)[0]
    assert payload_fingerprints_match(root, record) == (True, "")
    os.utime(root / "README.md", ns=(1_000_000_000, 1_000_000_000))
    assert payload_fingerprints_match(root, record)[0] is False
    (root / "README.md").unlink()
    assert payload_fingerprints_match(root, record)[0] is False

    retire_publication(root)
    (root / "README.md").write_bytes(b"hello")
    write_checksums(root)
    revived = create_publication(
        root,
        repo_id="org/model",
        repo_type="model",
        desired_seed=False,
    )
    assert revived.created


def test_publication_refuses_conflicts_and_bad_modes(tmp_path):
    root = prepared_archive(tmp_path / "bad-mode")
    with pytest.raises(ValueError, match="unsupported"):
        create_publication(root, repo_id="org/model", repo_type="model", client_mode="bad")

    root = prepared_archive(tmp_path / "torrent-conflict")
    result = create_publication(root, repo_id="org/model", repo_type="model")
    result.torrent_path.write_bytes(b"corrupt")
    with pytest.raises(TorrentPublicationError, match="torrent artifact"):
        create_publication(root, repo_id="org/model", repo_type="model")

    root = prepared_archive(tmp_path / "record-conflict")
    result = create_publication(root, repo_id="org/model", repo_type="model")
    value = json.loads(result.record_path.read_text())
    value["metainfo_sha256"] = "0" * 64
    write_json_atomic(result.record_path, value)
    with pytest.raises(TorrentPublicationError, match="conflicts"):
        create_publication(root, repo_id="org/model", repo_type="model")

    root = prepared_archive(tmp_path / "commit-conflict")
    create_publication(root, repo_id="org/model", repo_type="model")
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "b" * 40,
        [
            HubFile("README.md", 5, blob_id=git_blob(b"hello")),
            HubFile("weights.bin", 20000, lfs_sha256=hashlib.sha256(b"w" * 20000).hexdigest()),
        ],
    )
    write_snapshot_plan(root, snapshot)
    write_verification_state(
        root,
        VerificationState("clean", "org/model", resolved_commit="b" * 40, upstream_commit="b" * 40),
    )
    with pytest.raises(TorrentPublicationError, match="fenced"):
        create_publication(root, repo_id="org/model", repo_type="model")


def test_publication_record_and_fence_reject_malformed_state(tmp_path):
    missing = tmp_path / "missing.json"
    assert load_publication(missing) is None
    with pytest.raises(ValueError, match="must contain"):
        PublicationRecord.from_dict([], source=missing)
    with pytest.raises(ValueError, match="Unsupported"):
        PublicationRecord.from_dict({}, source=missing)

    root = prepared_archive(tmp_path / "base")
    result = create_publication(root, repo_id="org/model", repo_type="model")
    base = result.record.to_dict()
    malformed = [
        "{",
        json.dumps({**base, "payload_fingerprints": []}),
        json.dumps({**base, "descriptor_sha256": "bad"}),
        json.dumps({**base, "publication_id": "wrong"}),
        json.dumps({**base, "lifecycle": "bad"}),
        json.dumps({**base, "client_mode": "bad"}),
    ]
    for index, value in enumerate(malformed):
        path = tmp_path / f"bad-{index}.json"
        path.write_text(value)
        with pytest.raises(ValueError):
            load_publication(path)

    # Old records load with the documented defaults.
    old = dict(base)
    for key in (
        "lifecycle",
        "desired_seed",
        "client_mode",
        "observed_backend",
        "observed_detail",
        "maintenance_resume_seed",
        "content_verification",
        "publication_trust",
        "upstream_provenance",
        "upstream_availability",
        "created_at_utc",
        "updated_at_utc",
    ):
        old.pop(key)
    loaded = PublicationRecord.from_dict(old, source=missing)
    assert loaded.lifecycle == "published" and loaded.content_verification == "upstream-verified"

    good_fence = json.loads(fence_path(root).read_text())
    fence_cases = [
        "{",
        json.dumps([]),
        json.dumps({**good_fence, "version": 99}),
        json.dumps({**good_fence, "publication_record": "../escape"}),
        json.dumps({**good_fence, "publication_record": "missing.json"}),
        json.dumps({**good_fence, "publication_id": "wrong"}),
    ]
    for value in fence_cases:
        fence_path(root).write_text(value)
        with pytest.raises(ValueError):
            load_fenced_publication(root)
    write_json_atomic(fence_path(root), good_fence)

    retired = load_publication(result.record_path)
    retired.lifecycle = "retired"
    write_json_atomic(result.record_path, retired.to_dict())
    with pytest.raises(ValueError, match="retired"):
        load_fenced_publication(root)

    assert require_digest("A" * 40, 40, missing) == "a" * 40
    with pytest.raises(ValueError, match="Malformed digest"):
        require_digest("z", 40, missing)


def test_publication_operations_without_fence_and_wait_without_fence(tmp_path):
    root = tmp_path / "none"
    assert begin_maintenance(root) is None
    assert finish_maintenance(root, healthy=True) is None
    wait_for_maintenance_detach(root, timeout_seconds=0)
    for operation in (
        lambda: set_seed_desired(root, desired=True),
        lambda: update_observed_backend(root, state="x"),
    ):
        with pytest.raises(TorrentPublicationError, match="no active"):
            operation()


def test_maintenance_wait_observes_async_daemon_detach(tmp_path):
    root = prepared_archive(tmp_path)
    create_publication(
        root,
        repo_id="org/model",
        repo_type="model",
        desired_seed=True,
    )
    update_observed_backend(root, state="seeding")
    begin_maintenance(root)

    def detach():
        time.sleep(0.02)
        update_observed_backend(root, state="stopped")

    thread = threading.Thread(target=detach)
    thread.start()
    wait_for_maintenance_detach(root, timeout_seconds=1)
    thread.join()
