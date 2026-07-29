from dataclasses import dataclass

import pytest

from model_mirror.hub import (
    HubSnapshot,
    cached_manifest_verifies,
    read_snapshot_plan,
    write_snapshot_plan,
)
import model_mirror.repair as repair_module
from model_mirror.config import Config
from model_mirror.checksums import checksum_row_from_hashes, file_hashes, load_manifest, write_checksums
from model_mirror.repair import (
    missing_manifest_paths,
    partition_blocking_removals,
    preview_update,
    remove_obsolete_snapshot_files,
    repair,
)
from model_mirror.payload import UnsafePayloadError
from model_mirror.state import VerificationState, read_verification_state, write_verification_state


@dataclass
class FakeFile:
    path: str
    size: int
    lfs_sha256: str | None = None
    blob_id: str | None = None


class FakeHub:
    def __init__(self, metadata, metadata_by_revision=None):
        self.metadata = metadata
        self.metadata_by_revision = metadata_by_revision or {}
        self.downloads = []

    def files(self, repo_id, repo_type, revision):
        return self.metadata_by_revision.get(revision, self.metadata)

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        self.downloads.append((repo_id, repo_type, revision, local_dir, allow_patterns))
        selected_metadata = self.metadata_by_revision.get(revision, self.metadata)
        selected = set(allow_patterns or [item.path for item in selected_metadata])
        for item in selected_metadata:
            if item.path not in selected:
                continue
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}" if item.path == "config.json" else b"x" * item.size)
        return local_dir


class StreamingFakeHub(FakeHub):
    def download_snapshot(self, snapshot, root, allow_patterns=None):
        self.downloads.append((snapshot.repo_id, snapshot.repo_type, snapshot.resolved_commit, root, allow_patterns))


def test_repair_uses_local_verification_state_without_reverifying_first(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "good.bin").write_bytes(b"good")
    (archive / "bad.bin").write_bytes(b"bad")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["bad.bin", "missing.bin"],
        ),
    )
    hub = FakeHub(
        [
            FakeFile("good.bin", 4),
            FakeFile("bad.bin", 5),
            FakeFile("missing.bin", 7),
        ]
    )

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "repaired"
    assert result.paths == ["bad.bin", "missing.bin"]
    assert hub.downloads[0][4] == ["bad.bin", "missing.bin"]
    assert (archive / "good.bin").read_bytes() == b"good"
    assert read_verification_state(archive).status == "clean"


def test_repair_never_unlinks_through_symlinked_payload_parent(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "file.bin"
    victim.write_bytes(b"keep")
    (archive / "linked").symlink_to(outside, target_is_directory=True)
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["linked/file.bin"],
        ),
    )
    hub = FakeHub([FakeFile("linked/file.bin", 3)])

    with pytest.raises(UnsafePayloadError, match="symlink"):
        repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert victim.read_bytes() == b"keep"
    assert hub.downloads == []


def test_repair_replaces_expected_payload_symlink_without_touching_target(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    (archive / "file.bin").symlink_to(outside)
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["file.bin"],
        ),
    )
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "repaired"
    assert not (archive / "file.bin").is_symlink()
    assert (archive / "file.bin").read_bytes() == b"xxx"
    assert outside.read_bytes() == b"keep"


def test_repair_noops_when_verification_state_is_clean(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "complete"
    assert hub.downloads == []
    assert read_verification_state(archive).status == "clean"


def test_repair_fails_when_dirty_state_has_no_repair_paths(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="dirty", repo_id="org/model"))
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "no-repair-paths"
    assert hub.downloads == []


def test_repair_requires_verification_state(tmp_path):
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "verify-required"
    assert hub.downloads == []


def test_repair_download_snapshot_falls_back_to_snapshot_download(tmp_path):
    hub = FakeHub([FakeFile("file.bin", 3)])
    snapshot = HubSnapshot("org/model", "model", "main", "abc123", [FakeFile("file.bin", 3)])

    repair_module.download_snapshot(hub, snapshot, tmp_path, allow_patterns=["file.bin"])

    assert hub.downloads == [("org/model", "model", "abc123", tmp_path, ["file.bin"])]


def test_repair_download_snapshot_uses_streaming_adapter(tmp_path):
    hub = StreamingFakeHub([FakeFile("file.bin", 3)])
    snapshot = HubSnapshot("org/model", "model", "main", "abc123", [FakeFile("file.bin", 3)])

    repair_module.download_snapshot(hub, snapshot, tmp_path, allow_patterns=["file.bin"])

    assert hub.downloads == [("org/model", "model", "abc123", tmp_path, ["file.bin"])]


def test_derive_state_fetches_snapshot_when_not_supplied(tmp_path):
    root = tmp_path / "datasets" / "org" / "data"
    root.mkdir(parents=True)
    (root / "file.bin").write_bytes(b"xxx")
    hub = FakeHub([FakeFile("file.bin", 3)])

    state = repair_module.derive_state(
        Config(directory=tmp_path),
        "org/data",
        hub,
        "dataset",
        "main",
        root,
    )

    assert state.status == "clean"
    assert state.resolved_commit == "main"


def test_repair_cached_manifest_verifies_rejects_missing_and_wrong_rows(tmp_path):
    metadata = [FakeFile("file.bin", 3, lfs_sha256="sha")]
    assert cached_manifest_verifies(tmp_path, metadata) is False

    path = tmp_path / "file.bin"
    path.write_bytes(b"abc")
    assert cached_manifest_verifies(tmp_path, metadata) is False

    row = checksum_row_from_hashes(tmp_path, path, file_hashes(path))
    row["sha256"] = "wrong"
    from model_mirror.checksums import write_manifest

    write_manifest(tmp_path, {"file.bin": row})
    assert cached_manifest_verifies(tmp_path, metadata) is False


def test_repair_refuses_offline_only_state(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            offline_only=True,
            repair_paths=["missing.bin"],
        ),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "offline-only"
    assert result.paths == []
    assert hub.downloads == []


def test_repair_updates_existing_checksums_for_repaired_paths(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "bad.bin").write_bytes(b"bad")
    write_checksums(archive)
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", resolved_commit="main", repair_paths=["bad.bin"]),
    )
    hub = FakeHub([FakeFile("bad.bin", 5)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    manifest = load_manifest(archive)
    assert result.status == "repaired"
    assert manifest["bad.bin"]["size"] == 5


def test_repair_can_run_without_checksum_writes(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", resolved_commit="main", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    result = repair(Config(directory=tmp_path, checksum=False), "org/model", hub=hub)

    assert result.status == "repaired"
    assert not (archive / ".manifest").exists()


def test_repair_does_not_discover_paths_without_verification_state(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "wrong-size.bin").write_bytes(b"x")
    hub = FakeHub([FakeFile("wrong-size.bin", 3), FakeFile("missing.bin", 2)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "verify-required"
    assert hub.downloads == []


def test_repair_does_not_discover_checksum_failures_without_verification_state(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    write_checksums(archive)
    (archive / "file.bin").write_bytes(b"abd")
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "verify-required"
    assert hub.downloads == []


def test_repair_defaults_to_recorded_commit_when_upstream_changed(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
            repair_paths=["missing.bin"],
        ),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    state = read_verification_state(archive)
    assert result.status == "repaired"
    assert result.upstream_status == "changed"
    assert hub.downloads[0][2] == "oldcommit"
    assert state.resolved_commit == "oldcommit"
    assert state.upstream_commit == "newcommit"
    assert state.upstream_status == "changed"


def test_repair_requires_recorded_commit_for_dirty_state(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "verification-incomplete"
    assert hub.downloads == []


def test_repair_stops_when_untouched_file_lacks_cached_verification_data(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "good.bin").write_bytes(b"good")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["missing.bin"],
        ),
    )
    hub = FakeHub(
        [
            FakeFile("config.json", 2),
            FakeFile("good.bin", 4, lfs_sha256="sha"),
            FakeFile("missing.bin", 3),
        ]
    )

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "verification-incomplete"
    assert hub.downloads == []


def test_repair_force_partial_attempts_repair_with_incomplete_cached_data(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "good.bin").write_bytes(b"good")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["missing.bin"],
        ),
    )
    hub = FakeHub(
        [
            FakeFile("config.json", 2),
            FakeFile("good.bin", 4, lfs_sha256="sha"),
            FakeFile("missing.bin", 3),
        ]
    )

    result = repair(Config(directory=tmp_path), "org/model", hub=hub, force_partial=True)

    assert result.status == "verification-incomplete"
    assert hub.downloads


def test_missing_manifest_paths_skips_unrepairable_or_unavailable_files(tmp_path):
    (tmp_path / "wrong-size.bin").write_bytes(b"x")
    (tmp_path / "git-file.txt").write_bytes(b"abc")

    missing = missing_manifest_paths(
        tmp_path,
        [
            FakeFile("ignored.bin", 3, lfs_sha256="sha"),
            FakeFile("missing.bin", 3, lfs_sha256="sha"),
            FakeFile("wrong-size.bin", 3, lfs_sha256="sha"),
            FakeFile("git-file.txt", 3, blob_id="blob"),
            FakeFile("no-hash.txt", 3),
        ],
        ignored_paths={"ignored.bin"},
    )

    assert missing == ["git-file.txt"]


def test_repair_update_applies_changed_upstream_commit(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_snapshot_plan(
        archive,
        HubSnapshot("org/model", "model", "main", "oldcommit", [FakeFile("file.bin", 3)]),
    )
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
        ),
    )
    hub = FakeHub(
        [FakeFile("file.bin", 3)],
        metadata_by_revision={"newcommit": [FakeFile("config.json", 2), FakeFile("file.bin", 4)]},
    )

    result = repair(Config(directory=tmp_path), "org/model", hub=hub, update=True)

    state = read_verification_state(archive)
    assert result.status == "updated"
    assert hub.downloads[0][2] == "newcommit"
    assert (archive / "file.bin").stat().st_size == 4
    assert state.resolved_commit == "newcommit"
    assert state.upstream_commit == "newcommit"
    assert state.upstream_status == "current"
    snapshot = read_snapshot_plan(archive)
    assert snapshot.resolved_commit == "newcommit"
    assert snapshot.requested_revision == "main"
    assert [item.path for item in snapshot.files] == ["config.json", "file.bin"]


def test_preview_update_reports_add_change_remove_and_reuse_without_writes(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    current = HubSnapshot(
        "org/model",
        "model",
        "main",
        "oldcommit",
        [
            FakeFile("changed.bin", 3, lfs_sha256="old"),
            FakeFile("removed.bin", 4, blob_id="removed"),
            FakeFile("same.bin", 5, lfs_sha256="same"),
        ],
    )
    write_snapshot_plan(archive, current)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
        ),
    )
    before = (archive / ".model-mirror" / "snapshot.json").read_bytes()
    hub = FakeHub(
        [],
        metadata_by_revision={
            "newcommit": [
                FakeFile("added.bin", 2, blob_id="added"),
                FakeFile("changed.bin", 6, lfs_sha256="new"),
                FakeFile("same.bin", 5, lfs_sha256="same"),
            ]
        },
    )

    plan = preview_update(Config(directory=tmp_path), "org/model", hub=hub)

    assert [item.path for item in plan.added] == ["added.bin"]
    assert [item.new.path for item in plan.changed] == ["changed.bin"]
    assert [item.path for item in plan.removed] == ["removed.bin"]
    assert [item.path for item in plan.unchanged] == ["same.bin"]
    assert plan.current_bytes == 12
    assert plan.target_bytes == 13
    assert plan.candidate_download_bytes == 8
    assert (archive / ".model-mirror" / "snapshot.json").read_bytes() == before
    assert hub.downloads == []


def test_preview_update_requires_usable_changed_upstream_state(tmp_path):
    config = Config(directory=tmp_path)

    with pytest.raises(ValueError, match="verification state unavailable"):
        preview_update(config, "org/missing", hub=FakeHub([]))

    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(status="clean", repo_id="org/model", offline_only=True),
    )
    with pytest.raises(ValueError, match="offline-only"):
        preview_update(config, "org/model", hub=FakeHub([]))

    write_verification_state(
        archive,
        VerificationState(status="clean", repo_id="org/model", resolved_commit="old"),
    )
    with pytest.raises(ValueError, match="no changed upstream commit"):
        preview_update(config, "org/model", hub=FakeHub([]))


def test_preview_update_fetches_old_commit_when_snapshot_plan_is_missing(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="old",
            upstream_commit="new",
            upstream_status="changed",
        ),
    )
    hub = FakeHub(
        [],
        metadata_by_revision={
            "old": [FakeFile("old.bin", 1, blob_id="old")],
            "new": [FakeFile("new.bin", 2, blob_id="new")],
        },
    )

    plan = preview_update(Config(directory=tmp_path), "org/model", hub=hub)

    assert [item.path for item in plan.removed] == ["old.bin"]
    assert [item.path for item in plan.added] == ["new.bin"]


def test_obsolete_snapshot_removal_handles_symlink_missing_parent_and_directory(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    (tmp_path / "link.bin").symlink_to(outside)
    empty_parent = tmp_path / "empty"
    empty_parent.mkdir()

    remove_obsolete_snapshot_files(
        tmp_path,
        [FakeFile("link.bin", 1), FakeFile("empty/missing.bin", 1)],
    )

    assert outside.read_bytes() == b"keep"
    assert not (tmp_path / "link.bin").exists()
    assert not empty_parent.exists()

    nonempty_parent = tmp_path / "nonempty"
    nonempty_parent.mkdir()
    (nonempty_parent / "remove.bin").write_bytes(b"x")
    (nonempty_parent / "keep.bin").write_bytes(b"x")
    remove_obsolete_snapshot_files(
        tmp_path,
        [FakeFile("nonempty/remove.bin", 1)],
    )
    assert (nonempty_parent / "keep.bin").exists()

    (tmp_path / "directory.bin").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        remove_obsolete_snapshot_files(
            tmp_path,
            [FakeFile("directory.bin", 1)],
        )


def test_partition_blocking_removals_detects_file_directory_shape_changes():
    removed = [
        FakeFile("old.bin", 1),
        FakeFile("parent", 1),
        FakeFile("nested/file.bin", 1),
    ]
    target = [
        FakeFile("parent/file.bin", 1),
        FakeFile("nested", 1),
        FakeFile("new.bin", 1),
    ]

    blocking, deferred = partition_blocking_removals(removed, target)

    assert [item.path for item in blocking] == ["parent", "nested/file.bin"]
    assert [item.path for item in deferred] == ["old.bin"]


def test_repair_update_removes_only_paths_obsolete_in_pinned_snapshot(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "removed.bin").write_bytes(b"old")
    (archive / "untracked.bin").write_bytes(b"keep")
    write_snapshot_plan(
        archive,
        HubSnapshot(
            "org/model",
            "model",
            "main",
            "oldcommit",
            [FakeFile("removed.bin", 3), FakeFile("kept.bin", 3)],
        ),
    )
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
        ),
    )
    hub = FakeHub([], metadata_by_revision={"newcommit": [FakeFile("kept.bin", 3)]})

    result = repair(
        Config(directory=tmp_path, checksum=False),
        "org/model",
        hub=hub,
        update=True,
    )

    assert result.status == "updated"
    assert not (archive / "removed.bin").exists()
    assert (archive / "untracked.bin").read_bytes() == b"keep"


def test_repair_update_does_not_publish_clean_state_before_snapshot_plan(tmp_path, monkeypatch):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_snapshot_plan(
        archive,
        HubSnapshot("org/model", "model", "main", "oldcommit", [FakeFile("file.bin", 3)]),
    )
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
        ),
    )
    hub = FakeHub(
        [FakeFile("file.bin", 3)],
        metadata_by_revision={"newcommit": [FakeFile("config.json", 2), FakeFile("file.bin", 4)]},
    )

    def fail_snapshot_write(root, snapshot):
        raise RuntimeError("snapshot write failed")

    monkeypatch.setattr(repair_module, "write_snapshot_plan", fail_snapshot_write)

    with pytest.raises(RuntimeError, match="snapshot write failed"):
        repair(Config(directory=tmp_path), "org/model", hub=hub, update=True)

    state = read_verification_state(archive)
    assert state.resolved_commit == "oldcommit"
    assert state.upstream_commit == "newcommit"
    assert state.upstream_status == "changed"


def test_repair_reconciles_stale_snapshot_from_manifest_without_payload_reread(tmp_path, monkeypatch):
    archive = tmp_path / "datasets" / "org" / "data"
    archive.mkdir(parents=True)
    payload = archive / "file.bin"
    payload.write_bytes(b"abc")
    hashes = file_hashes(payload)
    write_checksums(archive)
    write_snapshot_plan(
        archive,
        HubSnapshot("org/data", "dataset", "main", "oldcommit", [FakeFile("old.bin", 1)]),
    )
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/data",
            repo_type="dataset",
            requested_revision="main",
            resolved_commit="newcommit",
            upstream_commit="newcommit",
            upstream_status="current",
        ),
    )
    hub = FakeHub(
        [FakeFile("file.bin", 3, lfs_sha256=hashes.sha256)],
        metadata_by_revision={"newcommit": [FakeFile("file.bin", 3, lfs_sha256=hashes.sha256)]},
    )

    def fail_payload_reread(path):
        raise AssertionError(f"unexpected payload reread: {path}")

    monkeypatch.setattr("model_mirror.verify.file_hashes", fail_payload_reread)

    result = repair(Config(directory=tmp_path), "org/data", repo_type="dataset", hub=hub)

    assert result.status == "repaired"
    assert hub.downloads == []
    snapshot = read_snapshot_plan(archive)
    assert snapshot.resolved_commit == "newcommit"
    assert snapshot.requested_revision == "main"
    assert [item.path for item in snapshot.files] == ["file.bin"]
    assert read_verification_state(archive).status == "clean"


def test_repair_keeps_stale_snapshot_when_reconciliation_fails_verification(tmp_path):
    archive = tmp_path / "datasets" / "org" / "data"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"x")
    write_snapshot_plan(
        archive,
        HubSnapshot("org/data", "dataset", "main", "oldcommit", [FakeFile("old.bin", 1)]),
    )
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/data",
            repo_type="dataset",
            requested_revision="main",
            resolved_commit="newcommit",
            upstream_commit="newcommit",
            upstream_status="current",
        ),
    )
    hub = FakeHub(
        [FakeFile("file.bin", 3)],
        metadata_by_revision={"newcommit": [FakeFile("file.bin", 3)]},
    )

    result = repair(Config(directory=tmp_path), "org/data", repo_type="dataset", hub=hub)

    assert result.status == "incomplete"
    assert read_snapshot_plan(archive).resolved_commit == "oldcommit"
    state = read_verification_state(archive)
    assert state.status == "dirty"
    assert state.repair_paths == ["file.bin"]


def test_repair_update_can_skip_checksum_writes(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
        ),
    )
    hub = FakeHub(
        [FakeFile("file.bin", 3)],
        metadata_by_revision={"newcommit": [FakeFile("config.json", 2), FakeFile("file.bin", 4)]},
    )

    result = repair(Config(directory=tmp_path, checksum=False), "org/model", hub=hub, update=True)

    assert result.status == "updated"
    assert not (archive / ".manifest").exists()
