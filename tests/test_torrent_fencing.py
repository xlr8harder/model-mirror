import hashlib

import pytest

import model_mirror.repair as repair_module
from model_mirror.checksums import write_checksums
from model_mirror.config import Config
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.mirror import mirror
from model_mirror.repair import repair
from model_mirror.state import VerificationState, write_verification_state
from model_mirror.torrent import TorrentPublicationError
from model_mirror.torrent_publication import (
    create_publication,
    load_fenced_publication,
    update_observed_backend,
)
from model_mirror.lock import ModelBusyError


COMMIT = "a" * 40


class FakeHub:
    def __init__(self, snapshot, payload):
        self._snapshot = snapshot
        self.payload = payload

    def snapshot(self, repo_id, repo_type, revision):
        return self._snapshot

    def download_snapshot(self, snapshot, root, allow_patterns=None, stall_timeout_seconds=None):
        selected = snapshot.files
        if allow_patterns:
            selected = [item for item in selected if item.path in allow_patterns]
        for item in selected:
            path = root / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.payload)


def published_archive(tmp_path, *, repo_type="dataset"):
    type_dir = "datasets" if repo_type == "dataset" else "models"
    root = tmp_path / type_dir / "org" / "model"
    root.mkdir(parents=True)
    payload = b"correct"
    (root / "file.bin").write_bytes(payload)
    item = HubFile("file.bin", len(payload), lfs_sha256=hashlib.sha256(payload).hexdigest())
    snapshot = HubSnapshot("org/model", repo_type, "main", COMMIT, [item])
    write_checksums(root)
    write_snapshot_plan(root, snapshot)
    write_verification_state(
        root,
        VerificationState(
            "clean",
            "org/model",
            repo_type=repo_type,
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
            upstream_status="current",
        ),
    )
    create_publication(root, repo_id="org/model", repo_type=repo_type)
    return root, snapshot, payload


def test_mirror_cannot_replace_published_payload_even_at_same_commit(tmp_path):
    root, snapshot, payload = published_archive(tmp_path, repo_type="model")
    config = Config(directory=tmp_path, repo_type="model")
    hub = FakeHub(snapshot, payload)

    with pytest.raises(RuntimeError, match="published payload cannot be replaced"):
        mirror(config, "org/model", hub=hub, force=True)

    moved = HubSnapshot("org/model", "model", "main", "b" * 40, snapshot.files)
    with pytest.raises(TorrentPublicationError, match="update blocked"):
        mirror(config, "org/model", hub=FakeHub(moved, payload), force=True)


def test_same_commit_repair_enters_maintenance_and_resumes_publication(tmp_path):
    root, snapshot, payload = published_archive(tmp_path)
    (root / "file.bin").write_bytes(b"broken!")
    write_verification_state(
        root,
        VerificationState(
            "dirty",
            "org/model",
            repo_type="dataset",
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
            upstream_status="current",
            repair_paths=["file.bin"],
        ),
    )
    result = repair(
        Config(directory=tmp_path, repo_type="dataset"),
        "org/model",
        repo_type="dataset",
        hub=FakeHub(snapshot, payload),
    )
    assert result.status == "repaired"
    record = load_fenced_publication(root)[0]
    assert record.lifecycle == "published" and record.observed_backend == "stopped"


def test_repair_update_is_fenced_and_failed_maintenance_stays_unhealthy(tmp_path, monkeypatch):
    root, snapshot, payload = published_archive(tmp_path / "update")
    write_verification_state(
        root,
        VerificationState(
            "dirty",
            "org/model",
            repo_type="dataset",
            resolved_commit=COMMIT,
            upstream_commit="b" * 40,
            upstream_status="changed",
            repair_paths=["file.bin"],
        ),
    )
    with pytest.raises(TorrentPublicationError, match="update blocked"):
        repair(
            Config(directory=tmp_path / "update", repo_type="dataset"),
            "org/model",
            repo_type="dataset",
            hub=FakeHub(snapshot, payload),
            update=True,
        )

    root, snapshot, payload = published_archive(tmp_path / "failure")
    write_verification_state(
        root,
        VerificationState(
            "dirty",
            "org/model",
            repo_type="dataset",
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
            repair_paths=["file.bin"],
        ),
    )
    monkeypatch.setattr(
        repair_module,
        "repair_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("repair crashed")),
    )
    with pytest.raises(RuntimeError, match="repair crashed"):
        repair(
            Config(directory=tmp_path / "failure", repo_type="dataset"),
            "org/model",
            repo_type="dataset",
            hub=FakeHub(snapshot, payload),
        )
    record = load_fenced_publication(root)[0]
    assert record.lifecycle == "unhealthy" and not record.desired_seed


def test_failed_repair_tolerates_busy_failure_cleanup(tmp_path, monkeypatch):
    root, snapshot, payload = published_archive(tmp_path)
    write_verification_state(
        root,
        VerificationState(
            "dirty",
            "org/model",
            repo_type="dataset",
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
            repair_paths=["file.bin"],
        ),
    )
    real_lock = repair_module.ModelLock

    class BusyCleanupLock:
        def __init__(self, selected_root, command, repo_id, repo_type):
            self.root = selected_root
            self.command = command
            self.inner = real_lock(selected_root, command, repo_id, repo_type)

        def __enter__(self):
            if self.command == "torrent-maintenance-failed":
                raise ModelBusyError(self.root, {"command": "other"})
            return self.inner.__enter__()

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

    monkeypatch.setattr(repair_module, "ModelLock", BusyCleanupLock)
    monkeypatch.setattr(
        repair_module,
        "repair_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("repair crashed")),
    )
    with pytest.raises(RuntimeError, match="repair crashed"):
        repair(
            Config(directory=tmp_path, repo_type="dataset"),
            "org/model",
            repo_type="dataset",
            hub=FakeHub(snapshot, payload),
        )
    assert load_fenced_publication(root)[0].lifecycle == "maintenance"


def test_unpublished_repair_exception_has_no_maintenance_cleanup(tmp_path, monkeypatch):
    config = Config(directory=tmp_path, repo_type="dataset")
    root = tmp_path / "datasets" / "org" / "model"
    write_verification_state(
        root,
        VerificationState("clean", "org/model", repo_type="dataset", resolved_commit=COMMIT),
    )
    monkeypatch.setattr(
        repair_module,
        "repair_locked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("plain failure")),
    )
    with pytest.raises(RuntimeError, match="plain failure"):
        repair(config, "org/model", repo_type="dataset", hub=object())
