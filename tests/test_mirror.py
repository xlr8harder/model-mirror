import hashlib
from dataclasses import dataclass

import model_mirror.mirror as mirror_module
from model_mirror.checksums import file_hashes, load_manifest
from model_mirror.config import Config
from model_mirror.hub import HubSnapshot, cached_manifest_verifies, read_snapshot_plan, write_snapshot_plan
from model_mirror.lock import ModelBusyError, ModelLock
from model_mirror.mirror import mirror
from model_mirror.state import VerificationState, read_verification_state, write_verification_state


@dataclass
class FakeFile:
    path: str
    size: int
    lfs_sha256: str | None = None


class FakeHub:
    def __init__(self, metadata):
        self.metadata = metadata
        self.downloads = []

    def files(self, repo_id, repo_type, revision):
        return self.metadata

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        self.downloads.append((repo_id, repo_type, revision, local_dir, allow_patterns))
        for item in self.metadata:
            if allow_patterns and item.path not in allow_patterns:
                continue
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}" if item.path == "config.json" else b"x" * item.size)
        return local_dir


class CommitFakeHub(FakeHub):
    def snapshot(self, repo_id, repo_type, revision):
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit="abc123",
            files=self.metadata,
        )


class ChangingFakeHub(FakeHub):
    def __init__(self, first, second):
        super().__init__(first)
        self.first = first
        self.second = second
        self.snapshot_calls = 0

    def snapshot(self, repo_id, repo_type, revision):
        self.snapshot_calls += 1
        metadata = self.first if self.snapshot_calls == 1 else self.second
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit="abc123" if self.snapshot_calls == 1 else "def456",
            files=metadata,
        )


class StreamingFakeHub(FakeHub):
    def snapshot(self, repo_id, repo_type, revision):
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit="abc123",
            files=self.metadata,
        )

    def download_snapshot(self, snapshot, local_dir, allow_patterns=None, stall_timeout_seconds=None):
        self.downloads.append((snapshot.repo_id, snapshot.repo_type, snapshot.resolved_commit, local_dir, allow_patterns))
        for item in self.metadata:
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * item.size)
        from model_mirror.checksums import checksum_row_from_hashes, file_hashes, write_manifest

        manifest = {}
        for item in self.metadata:
            path = local_dir / item.path
            row = checksum_row_from_hashes(local_dir, path, file_hashes(path))
            manifest[row["path"]] = row
        write_manifest(local_dir, manifest)
        return local_dir


class InspectingFakeHub(FakeHub):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.state_during_download = None

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        self.state_during_download = read_verification_state(local_dir)
        return super().snapshot_download(repo_id, repo_type, revision, local_dir, allow_patterns)


def test_mirror_noops_without_verification_when_cached_verify_says_archive_is_complete(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub, verify_after=False)

    assert result.status == "complete"
    assert hub.downloads == []


def test_mirror_verifies_existing_complete_archive_without_clean_state(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "complete"
    assert hub.downloads == []
    assert read_verification_state(archive).status == "clean"


def test_mirror_noops_existing_clean_archive_at_same_commit(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    write_verification_state(
        archive,
        VerificationState(status="clean", repo_id="org/model", resolved_commit="main"),
    )
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "complete"
    assert hub.downloads == []


def test_mirror_existing_complete_archive_can_skip_checksum_writes(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path, checksum=False), "org/model", hub=hub)

    assert result.status == "complete"
    assert not (archive / ".manifest").exists()


def test_mirror_downloads_missing_archive_and_writes_checksums(tmp_path):
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "downloaded"
    assert hub.downloads
    assert result.path.joinpath("file.bin").read_bytes() == b"xxx"
    assert result.path.joinpath(".manifest").exists()
    assert result.path.joinpath(".verification").exists()
    assert read_snapshot_plan(result.path).resolved_commit == "main"


def test_mirror_does_not_rehash_after_streaming_download_manifest(tmp_path, monkeypatch):
    payload_hash = hashlib.sha256(b"xxx").hexdigest()
    hub = StreamingFakeHub([FakeFile("file.bin", 3, lfs_sha256=payload_hash)])

    def fail_write_checksums(*args, **kwargs):
        raise AssertionError("streaming download already wrote verified manifest")

    monkeypatch.setattr(mirror_module, "write_checksums", fail_write_checksums)

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub, repo_type="dataset")

    assert result.status == "downloaded"
    manifest = load_manifest(result.path)
    assert manifest["file.bin"]["sha256"] == payload_hash


def test_mirror_download_can_skip_checksum_writes(tmp_path):
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path, checksum=False), "org/model", hub=hub)

    assert result.status == "downloaded"
    assert not result.path.joinpath(".manifest").exists()


def test_mirror_writes_in_progress_verification_before_download(tmp_path):
    hub = InspectingFakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "downloaded"
    assert hub.state_during_download is not None
    assert hub.state_during_download.status == "in_progress"
    assert hub.state_during_download.resolved_commit == "main"


def test_mirror_reuses_frozen_snapshot_plan_for_interrupted_resume(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    frozen = HubSnapshot(
        repo_id="org/model",
        repo_type="model",
        requested_revision="main",
        resolved_commit="abc123",
        files=[FakeFile("config.json", 2), FakeFile("file.bin", 3)],
    )
    write_snapshot_plan(archive, frozen)
    write_verification_state(
        archive,
        VerificationState(status="in_progress", repo_id="org/model", requested_revision="main", resolved_commit="abc123"),
    )
    hub = ChangingFakeHub(
        [FakeFile("config.json", 2), FakeFile("file.bin", 3)],
        [FakeFile("other.bin", 5)],
    )

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "downloaded"
    assert hub.snapshot_calls == 0
    assert hub.downloads[0][2] == "abc123"
    assert (archive / "file.bin").exists()
    assert not (archive / "other.bin").exists()


def test_mirror_force_ignores_frozen_snapshot_plan(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    write_snapshot_plan(
        archive,
        HubSnapshot(
            repo_id="org/model",
            repo_type="model",
            requested_revision="main",
            resolved_commit="abc123",
            files=[FakeFile("old.bin", 3)],
        ),
    )
    write_verification_state(
        archive,
        VerificationState(status="in_progress", repo_id="org/model", requested_revision="main", resolved_commit="abc123"),
    )
    hub = CommitFakeHub([FakeFile("config.json", 2), FakeFile("new.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub, force=True)

    assert result.status == "downloaded"
    assert hub.downloads[0][2] == "abc123"
    assert read_snapshot_plan(archive).files[-1].path == "new.bin"


def test_select_mirror_snapshot_ignores_incompatible_plan(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    write_snapshot_plan(
        archive,
        HubSnapshot(
            repo_id="org/model",
            repo_type="model",
            requested_revision="other",
            resolved_commit="old",
            files=[FakeFile("old.bin", 3)],
        ),
    )
    hub = CommitFakeHub([FakeFile("new.bin", 3)])

    snapshot = mirror_module.select_mirror_snapshot(
        hub,
        "org/model",
        "model",
        "main",
        archive,
        existing_state=VerificationState(status="in_progress", repo_id="org/model"),
        force=False,
    )

    assert snapshot.resolved_commit == "abc123"
    assert snapshot.files[0].path == "new.bin"


def test_mirror_download_snapshot_falls_back_to_snapshot_download(tmp_path):
    hub = FakeHub([FakeFile("file.bin", 3)])
    snapshot = HubSnapshot("org/model", "model", "main", "abc123", [FakeFile("file.bin", 3)])

    mirror_module.download_snapshot(hub, snapshot, tmp_path)

    assert hub.downloads == [("org/model", "model", "abc123", tmp_path, None)]


def test_mirror_cached_manifest_verifies_rejects_missing_stale_and_wrong_rows(tmp_path):
    metadata = [FakeFile("file.bin", 3, lfs_sha256=hashlib.sha256(b"abc").hexdigest())]
    assert cached_manifest_verifies(tmp_path, metadata) is False

    path = tmp_path / "file.bin"
    path.write_bytes(b"abc")
    assert cached_manifest_verifies(tmp_path, metadata) is False

    from model_mirror.checksums import checksum_row_from_hashes, write_manifest

    row = checksum_row_from_hashes(tmp_path, path, file_hashes(path))
    row["sha256"] = "wrong"
    write_manifest(tmp_path, {"file.bin": row})
    assert cached_manifest_verifies(tmp_path, metadata) is False


def test_mirror_fails_fast_when_model_lock_is_held(tmp_path):
    config = Config(directory=tmp_path)
    archive = tmp_path / "models" / "org" / "model"
    hub = FakeHub([FakeFile("config.json", 2)])

    with ModelLock(archive, "test", "org/model"):
        try:
            mirror(config, "org/model", hub=hub)
        except ModelBusyError as exc:
            assert "busy" in str(exc)
        else:
            raise AssertionError("mirror should fail when lock is held")


def test_mirror_verifies_generated_checksums_against_lfs_hashes(tmp_path):
    actual_hash = hashlib.sha256(b"xxx").hexdigest()
    wrong_hash = hashlib.sha256(b"yyy").hexdigest()
    assert actual_hash != wrong_hash
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3, lfs_sha256=wrong_hash)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub)

    assert result.status == "downloaded-unverified"
    state = read_verification_state(result.path)
    assert state.status == "dirty"
    assert "hash_mismatches: file.bin" in state.issues
    assert state.repair_paths == ["file.bin"]


def test_mirror_can_skip_verification_after_download(tmp_path):
    hub = FakeHub([FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub, verify_after=False)

    assert result.status == "downloaded"
    state = read_verification_state(result.path)
    assert state.status == "dirty"
    assert state.issues == ["verification skipped"]


def test_mirror_downloads_resolved_commit_and_records_it(tmp_path):
    hub = CommitFakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    result = mirror(Config(directory=tmp_path), "org/model", hub=hub, revision="main")

    assert hub.downloads[0][2] == "abc123"
    state = read_verification_state(result.path)
    assert state.requested_revision == "main"
    assert state.resolved_commit == "abc123"
    assert state.upstream_status == "current"
