import hashlib
from dataclasses import dataclass

from model_mirror.config import Config
from model_mirror.hub import HubSnapshot
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
    assert not result.path.joinpath(".model-mirror").exists()


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
