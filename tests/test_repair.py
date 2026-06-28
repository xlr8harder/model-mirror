from dataclasses import dataclass

from model_mirror.config import Config
from model_mirror.checksums import load_records, write_checksums
from model_mirror.repair import repair
from model_mirror.state import VerificationState, read_verification_state, write_verification_state


@dataclass
class FakeFile:
    path: str
    size: int
    lfs_sha256: str | None = None


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


def test_repair_updates_existing_checksums_for_repaired_paths(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "bad.bin").write_bytes(b"bad")
    write_checksums(archive)
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", repair_paths=["bad.bin"]),
    )
    hub = FakeHub([FakeFile("bad.bin", 5)])

    result = repair(Config(directory=tmp_path), "org/model", hub=hub)

    checksums, manifest = load_records(archive)
    assert result.status == "repaired"
    assert checksums.keys() >= {"bad.bin"}
    assert manifest["bad.bin"]["size"] == 5


def test_repair_can_run_without_checksum_writes(tmp_path):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    result = repair(Config(directory=tmp_path, checksum=False), "org/model", hub=hub)

    assert result.status == "repaired"
    assert not (archive / ".checksums").exists()


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


def test_repair_update_applies_changed_upstream_commit(tmp_path):
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

    result = repair(Config(directory=tmp_path), "org/model", hub=hub, update=True)

    state = read_verification_state(archive)
    assert result.status == "updated"
    assert hub.downloads[0][2] == "newcommit"
    assert (archive / "file.bin").stat().st_size == 4
    assert state.resolved_commit == "newcommit"
    assert state.upstream_commit == "newcommit"
    assert state.upstream_status == "current"


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
    assert not (archive / ".checksums").exists()
