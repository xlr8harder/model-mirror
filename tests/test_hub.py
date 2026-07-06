import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_mirror.config as config_module
import model_mirror.hub as hub_module
from model_mirror.checksums import checksum_row_from_hashes, file_hashes, load_manifest, write_manifest
from model_mirror.config import Config
from model_mirror.hub import (
    DownloadIntegrityError,
    HubFile,
    HubSnapshot,
    HuggingFaceHub,
    StallTimeoutError,
    compatible_snapshot_plan,
    download_staging_dir,
    prune_incomplete_downloads,
    read_snapshot_plan,
    snapshot_plan_path,
    write_snapshot_plan,
    cached_manifest_verifies,
)
from model_mirror.progress import ProgressRecorder, progress_snapshot


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def git_blob_sha1(payload: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {len(payload)}\0".encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


def test_huggingface_hub_adapter_uses_configured_environment(tmp_path, monkeypatch, capsys):
    captured = {}
    payloads = { "a.bin": b"abc", "README.md": b"read" }
    token_path = tmp_path / "token"
    token_path.write_text("hf_example", encoding="utf-8")

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            captured["repo_info"] = (repo_id, repo_type, revision, files_metadata)
            captured["hf_home"] = os.environ["HF_HOME"]
            captured["token_path"] = os.environ["HF_TOKEN_PATH"]
            siblings = [
                SimpleNamespace(
                    rfilename="a.bin",
                    size=3,
                    lfs=SimpleNamespace(sha256=sha256(payloads["a.bin"])),
                    blob_id="pointer",
                ),
                SimpleNamespace(
                    rfilename="README.md",
                    size=4,
                    lfs=None,
                    blob_id=git_blob_sha1(payloads["README.md"]),
                ),
            ]
            return SimpleNamespace(sha="commit123", siblings=siblings)

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        captured["snapshot"] = snapshot
        captured["destination_root"] = destination.parents[len(Path(item.path).parents) - 1]
        captured["xet"] = os.environ["HF_XET_HIGH_PERFORMANCE"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payloads[item.path])
        return file_hashes(destination)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=FakeApi),
    )
    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    config = Config(directory=tmp_path, hf_xet_high_performance=True, token_path=token_path)
    hub = HuggingFaceHub(config)

    files = hub.files("org/model", "model", "main")
    snapshot = hub.snapshot("org/model", "model", "main")
    destination = tmp_path / "models" / "org" / "model"
    path = hub.snapshot_download("org/model", "model", "main", destination)

    assert captured["repo_info"] == ("org/model", "model", "main", True)
    assert captured["hf_home"] == str(tmp_path / ".cache")
    assert captured["token_path"] == str(token_path)
    assert captured["snapshot"].repo_id == "org/model"
    assert captured["snapshot"].repo_type == "model"
    assert captured["snapshot"].resolved_commit == "commit123"
    assert captured["destination_root"] == destination
    assert captured["xet"] == "1"
    assert path == destination
    assert (destination / "README.md").read_text(encoding="utf-8") == "read"
    assert set(load_manifest(destination)) == {"a.bin", "README.md"}
    assert snapshot.requested_revision == "main"
    assert snapshot.resolved_commit == "commit123"
    assert files[0].lfs_sha256 == sha256(payloads["a.bin"])
    assert files[1].lfs_sha256 is None
    assert files[1].blob_id == git_blob_sha1(payloads["README.md"])
    assert capsys.readouterr().err == ""


def test_huggingface_hub_snapshot_download_uses_ephemeral_staging_cache(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    destination = archive / "models" / "org" / "model"
    inherited_cache = tmp_path / "inherited-cache"
    captured = {}
    payload = b"payload"
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("HF_HOME", str(inherited_cache / "home"))
    monkeypatch.setenv("HF_HUB_CACHE", str(inherited_cache / "hub"))
    monkeypatch.setenv("HF_ASSETS_CACHE", str(inherited_cache / "assets"))
    monkeypatch.setenv("HF_XET_CACHE", str(inherited_cache / "xet"))
    monkeypatch.setenv("TRANSFORMERS_CACHE", str(inherited_cache / "transformers"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(inherited_cache / "xdg"))
    monkeypatch.setenv("TMPDIR", str(inherited_cache / "tmp"))

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            return SimpleNamespace(
                sha="commit123",
                siblings=[
                    SimpleNamespace(
                        rfilename="file.bin",
                        size=len(payload),
                        lfs=SimpleNamespace(sha256=sha256(payload)),
                        blob_id="pointer",
                    )
                ],
            )

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        captured["local_dir"] = download_staging_dir(
            Config(directory=archive),
            snapshot.repo_id,
            snapshot.repo_type,
            snapshot.resolved_commit,
            None,
        )
        captured["cache_dir"] = Path(os.environ["HF_HUB_CACHE"])
        captured["env"] = {
            key: os.environ[key]
            for key in (
                "HF_HOME",
                "HF_HUB_CACHE",
                "HF_ASSETS_CACHE",
                "HF_XET_CACHE",
                "TRANSFORMERS_CACHE",
                "XDG_CACHE_HOME",
                "TMPDIR",
            )
        }
        destination.write_bytes(payload)
        return file_hashes(destination)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=FakeApi),
    )
    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    hub = HuggingFaceHub(Config(directory=archive))
    result = hub.snapshot_download("org/model", "model", "main", destination)

    assert result == destination
    assert captured["local_dir"].is_relative_to(archive / ".tmp" / "downloads")
    assert captured["cache_dir"] == captured["local_dir"] / ".cache" / "hub"
    assert not captured["cache_dir"].is_relative_to(destination)
    assert captured["env"] == {
        "HF_HOME": str(captured["local_dir"] / ".cache" / "home"),
        "HF_HUB_CACHE": str(captured["local_dir"] / ".cache" / "hub"),
        "HF_ASSETS_CACHE": str(captured["local_dir"] / ".cache" / "assets"),
        "HF_XET_CACHE": str(captured["local_dir"] / ".cache" / "xet"),
        "TRANSFORMERS_CACHE": str(captured["local_dir"] / ".cache" / "transformers"),
        "XDG_CACHE_HOME": str(captured["local_dir"] / ".cache" / "xdg"),
        "TMPDIR": str(captured["local_dir"] / ".tmp"),
    }
    assert not captured["local_dir"].exists()
    assert (destination / "file.bin").read_bytes() == payload
    assert not (destination / ".cache").exists()
    assert not (destination / ".tmp").exists()


def test_huggingface_hub_snapshot_download_prunes_incomplete_local_dir_chunks(tmp_path, monkeypatch):
    destination = tmp_path / "models" / "org" / "model"
    config = Config(directory=tmp_path)
    staging = download_staging_dir(config, "org/model", "model", "commit123", None)
    stale = staging / ".cache" / "huggingface" / "download" / "stale.incomplete"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"partial")
    staged_stale = staging / "README.md.123.incomplete"
    staged_stale.write_bytes(b"partial")
    captured = {}
    payload = b"readme"
    monkeypatch.setenv("HF_TOKEN", "hf_secret")

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            return SimpleNamespace(
                sha="commit123",
                siblings=[
                    SimpleNamespace(
                        rfilename="README.md",
                        size=len(payload),
                        lfs=None,
                        blob_id=git_blob_sha1(payload),
                    )
                ],
            )

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        captured["stale_removed_before_download"] = not stale.exists()
        captured["staged_stale_removed_before_download"] = not staged_stale.exists()
        destination.write_bytes(payload)
        return file_hashes(destination)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=FakeApi),
    )
    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    hub = HuggingFaceHub(config)
    hub.snapshot_download("org/model", "model", "main", destination)

    assert captured["stale_removed_before_download"] is True
    assert captured["staged_stale_removed_before_download"] is False
    assert not staging.exists()
    assert (destination / "README.md").read_text(encoding="utf-8") == "readme"
    assert not (destination / ".cache").exists()


def test_prune_incomplete_downloads_returns_removed_count(tmp_path):
    download_dir = tmp_path / ".cache" / "huggingface" / "download"
    download_dir.mkdir(parents=True)
    (download_dir / "a.incomplete").write_bytes(b"a")
    (download_dir / "dir.incomplete").mkdir()
    (download_dir / "b.metadata").write_text("b", encoding="utf-8")

    assert prune_incomplete_downloads(tmp_path) == 1
    assert not (download_dir / "a.incomplete").exists()
    assert (download_dir / "dir.incomplete").is_dir()
    assert (download_dir / "b.metadata").exists()
    assert prune_incomplete_downloads(tmp_path / "missing") == 0


def test_snapshot_plan_round_trips_and_filters_compatible_resume(tmp_path):
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [HubFile("file.bin", 3, lfs_sha256="sha", blob_id="pointer")],
    )

    path = write_snapshot_plan(tmp_path, snapshot)
    loaded = read_snapshot_plan(tmp_path)

    assert path == snapshot_plan_path(tmp_path)
    assert loaded == snapshot
    assert compatible_snapshot_plan(
        tmp_path,
        repo_id="org/model",
        repo_type="model",
        requested_revision="main",
    ) == snapshot
    assert compatible_snapshot_plan(
        tmp_path,
        repo_id="org/other",
        repo_type="model",
        requested_revision="main",
    ) is None
    assert compatible_snapshot_plan(
        tmp_path,
        repo_id="org/model",
        repo_type="dataset",
        requested_revision="main",
    ) is None
    assert compatible_snapshot_plan(
        tmp_path,
        repo_id="org/model",
        repo_type="model",
        requested_revision="dev",
    ) is None


def test_snapshot_plan_missing_returns_none(tmp_path):
    assert read_snapshot_plan(tmp_path) is None
    assert compatible_snapshot_plan(
        tmp_path,
        repo_id="org/model",
        repo_type="model",
        requested_revision="main",
    ) is None


def test_snapshot_plan_reader_rejects_unknown_schema_or_version(tmp_path):
    path = snapshot_plan_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": "wrong", "version": 1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported snapshot plan schema"):
        read_snapshot_plan(tmp_path)

    path.write_text('{"schema": "model-mirror-snapshot", "version": 999}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported snapshot plan version"):
        read_snapshot_plan(tmp_path)


def test_stream_file_to_path_resumes_xet_incomplete_from_hashed_prefix(tmp_path, monkeypatch):
    payload = b"abcdef"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    partial = hub_module.incomplete_path_for(destination)
    partial.write_bytes(payload[:3])
    calls = []

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload), xet_file_data=object()), {}, "unused"),
    )

    def fake_stream_xet_file(xet_file_data, headers, writer, expected_size, rel, resume_size, **kwargs):
        calls.append((expected_size, rel, resume_size))
        writer.write(payload[resume_size:])

    monkeypatch.setattr(hub_module, "stream_xet_file", fake_stream_xet_file)

    hashes = hub_module.stream_file_to_path(snapshot, item, destination)

    assert calls == [(len(payload), "file.bin", 3)]
    assert destination.read_bytes() == payload
    assert not partial.exists()
    assert hashes.sha256 == sha256(payload)
    assert hashes.git_blob_sha1 == git_blob_sha1(payload)


def test_stream_file_to_path_retries_corrupt_partial_from_zero(tmp_path, monkeypatch):
    payload = b"abcdef"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    partial = hub_module.incomplete_path_for(destination)
    partial.write_bytes(b"xxx")
    starts = []

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload), xet_file_data=object()), {}, "unused"),
    )

    def fake_stream_xet_file(xet_file_data, headers, writer, expected_size, rel, resume_size, **kwargs):
        starts.append(resume_size)
        writer.write(payload[resume_size:])

    monkeypatch.setattr(hub_module, "stream_xet_file", fake_stream_xet_file)

    hashes = hub_module.stream_file_to_path(snapshot, item, destination)

    assert starts == [3, 0]
    assert destination.read_bytes() == payload
    assert not partial.exists()
    assert hashes.sha256 == sha256(payload)


def test_stream_file_to_path_retries_stalled_download_from_partial(tmp_path, monkeypatch):
    payload = b"abcdef"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    starts = []

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload), xet_file_data=object()), {}, "unused"),
    )

    def flaky_xet(xet_file_data, headers, writer, expected_size, rel, resume_size, **kwargs):
        starts.append(resume_size)
        if len(starts) == 1:
            writer.write(payload[:2])
            raise StallTimeoutError("stalled")
        writer.write(payload[resume_size:])

    monkeypatch.setattr(hub_module, "stream_xet_file", flaky_xet)

    recorder = ProgressRecorder(tmp_path, min_interval_seconds=0, min_bytes=1)
    progress = recorder.track(item.path, total=item.size, stage="starting")
    hashes = hub_module.stream_file_to_path(snapshot, item, destination, progress=progress, stall_retries=3)

    assert starts == [0, 2]
    assert destination.read_bytes() == payload
    assert hashes.sha256 == sha256(payload)
    assert progress_snapshot(tmp_path).entries[0].stage == "verifying"


def test_stream_file_to_path_fails_after_stall_retry_limit(tmp_path, monkeypatch):
    payload = b"abcdef"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    attempts = []

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload), xet_file_data=object()), {}, "unused"),
    )

    def stalled_xet(xet_file_data, headers, writer, expected_size, rel, resume_size, **kwargs):
        attempts.append(resume_size)
        if resume_size < len(payload):
            writer.write(payload[resume_size : resume_size + 1])
        raise StallTimeoutError("stalled")

    monkeypatch.setattr(hub_module, "stream_xet_file", stalled_xet)

    with pytest.raises(StallTimeoutError):
        hub_module.stream_file_to_path(snapshot, item, destination, stall_retries=2)

    assert attempts == [0, 1, 2]
    assert hub_module.incomplete_path_for(destination).read_bytes() == payload[:3]


def test_stream_snapshot_hashes_existing_final_file_after_restart(tmp_path, monkeypatch):
    payload = b"abc"
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    local_dir.mkdir(parents=True)
    (local_dir / "file.bin").write_bytes(payload)
    staging_dir = tmp_path / ".tmp" / "downloads" / "repo"

    def fail_download(snapshot, item, destination):
        raise AssertionError("existing final file should be hashed before redownload")

    monkeypatch.setattr(hub_module, "stream_file_to_path", fail_download)

    hub_module.stream_snapshot(snapshot, local_dir, staging_dir, allow_patterns=None)

    manifest = load_manifest(local_dir)
    assert (local_dir / "file.bin").read_bytes() == payload
    assert manifest["file.bin"]["sha256"] == sha256(payload)


def test_stream_snapshot_promotes_legacy_completed_staging_file(tmp_path, monkeypatch):
    payload = b"abc"
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    staging_dir = tmp_path / ".tmp" / "downloads" / "repo"
    staged = staging_dir / "file.bin"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(payload)

    def fail_download(snapshot, item, destination):
        raise AssertionError("completed legacy staged file should be reused")

    monkeypatch.setattr(hub_module, "stream_file_to_path", fail_download)

    hub_module.stream_snapshot(snapshot, local_dir, staging_dir, allow_patterns=None)

    manifest = load_manifest(local_dir)
    assert (local_dir / "file.bin").read_bytes() == payload
    assert not staged.exists()
    assert manifest["file.bin"]["sha256"] == sha256(payload)


def test_stream_snapshot_skips_file_with_current_manifest(tmp_path, monkeypatch):
    payload = b"abc"
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    local_dir.mkdir(parents=True)
    path = local_dir / "file.bin"
    path.write_bytes(payload)
    write_manifest(local_dir, {"file.bin": checksum_row_from_hashes(local_dir, path, file_hashes(path))})

    def fail_download(snapshot, item, destination):
        raise AssertionError("current manifest row should skip download")

    monkeypatch.setattr(hub_module, "stream_file_to_path", fail_download)

    hub_module.stream_snapshot(snapshot, local_dir, tmp_path / ".tmp", allow_patterns=None)

    assert (local_dir / "file.bin").read_bytes() == payload


def test_verified_manifest_record_rejects_wrong_size_missing_row_and_missing_hash(tmp_path):
    payload = b"abc"
    root = tmp_path
    path = root / "file.bin"
    path.write_bytes(payload)
    item = HubFile("file.bin", len(payload) + 1, lfs_sha256=sha256(payload), blob_id="pointer")

    assert hub_module.verified_manifest_record(root, path, item, {}) is False
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    assert hub_module.verified_manifest_record(root, path, item, {}) is False
    row = checksum_row_from_hashes(root, path, file_hashes(path))
    row.pop("sha256")
    assert hub_module.verified_manifest_record(root, path, item, {"file.bin": row}) is False


def test_manifest_hash_matches_handles_empty_and_blob_only_rows():
    assert hub_module.manifest_hash_matches(HubFile("file.bin", 1, blob_id="blob"), None) is False
    assert hub_module.manifest_hash_matches(HubFile("file.bin", 1, blob_id="blob"), {"git_blob_sha1": "blob"}) is True
    assert hub_module.manifest_hash_matches(HubFile("file.bin", 1), {"git_blob_sha1": "blob"}) is False


def test_cached_manifest_verifies_accepts_lfs_and_blob_rows(tmp_path):
    lfs_payload = b"abc"
    blob_payload = b"read"
    (tmp_path / "file.bin").write_bytes(lfs_payload)
    (tmp_path / "README.md").write_bytes(blob_payload)
    manifest = {}
    for rel in ("file.bin", "README.md"):
        path = tmp_path / rel
        row = checksum_row_from_hashes(tmp_path, path, file_hashes(path))
        manifest[row["path"]] = row
    write_manifest(tmp_path, manifest)

    metadata = [
        HubFile("file.bin", len(lfs_payload), lfs_sha256=sha256(lfs_payload), blob_id="pointer"),
        HubFile("README.md", len(blob_payload), blob_id=git_blob_sha1(blob_payload)),
    ]

    assert cached_manifest_verifies(tmp_path, metadata) is True


def test_stream_snapshot_redownloads_corrupt_existing_file(tmp_path, monkeypatch):
    payload = b"good"
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    local_dir.mkdir(parents=True)
    (local_dir / "file.bin").write_bytes(b"xxxx")

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        destination.write_bytes(payload)
        return file_hashes(destination)

    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    hub_module.stream_snapshot(snapshot, local_dir, tmp_path / ".tmp", allow_patterns=None)

    assert (local_dir / "file.bin").read_bytes() == payload
    assert load_manifest(local_dir)["file.bin"]["sha256"] == sha256(payload)


def test_stream_snapshot_filters_allow_patterns(tmp_path, monkeypatch):
    payload = b"abc"
    skipped = HubFile("skip.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    kept = HubFile("keep.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [skipped, kept])
    local_dir = tmp_path / "models" / "org" / "model"
    seen = []

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        seen.append(item.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return file_hashes(destination)

    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    hub_module.stream_snapshot(snapshot, local_dir, tmp_path / ".tmp", allow_patterns=["keep.*"])

    assert seen == ["keep.bin"]
    assert (local_dir / "keep.bin").exists()
    assert not (local_dir / "skip.bin").exists()


def test_stream_snapshot_can_download_multiple_files_concurrently(tmp_path, monkeypatch):
    payloads = {"a.bin": b"aaa", "b.bin": b"bbbb"}
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [
            HubFile("a.bin", len(payloads["a.bin"]), lfs_sha256=sha256(payloads["a.bin"]), blob_id="pointer"),
            HubFile("b.bin", len(payloads["b.bin"]), lfs_sha256=sha256(payloads["b.bin"]), blob_id="pointer"),
        ],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    barrier = threading.Barrier(2, timeout=5)
    seen = []
    lock = threading.Lock()

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        with lock:
            seen.append(item.path)
        barrier.wait()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payloads[item.path])
        return file_hashes(destination)

    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    hub_module.stream_snapshot(
        snapshot,
        local_dir,
        tmp_path / ".tmp",
        allow_patterns=None,
        download_workers=2,
    )

    manifest = load_manifest(local_dir)
    assert sorted(seen) == ["a.bin", "b.bin"]
    assert set(manifest) == {"a.bin", "b.bin"}
    assert manifest["a.bin"]["sha256"] == sha256(payloads["a.bin"])
    assert manifest["b.bin"]["sha256"] == sha256(payloads["b.bin"])


def test_stream_snapshot_parallel_failure_keeps_completed_manifest_rows(tmp_path, monkeypatch):
    payload = b"good"
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [
            HubFile("good.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer"),
            HubFile("bad.bin", 3, lfs_sha256="bad", blob_id="pointer"),
        ],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    release_good = threading.Event()

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        if item.path == "bad.bin":
            release_good.set()
            raise DownloadIntegrityError("bad download")
        assert release_good.wait(timeout=5)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return file_hashes(destination)

    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    with pytest.raises(DownloadIntegrityError, match="bad download"):
        hub_module.stream_snapshot(
            snapshot,
            local_dir,
            tmp_path / ".tmp",
            allow_patterns=None,
            download_workers=2,
        )

    manifest = load_manifest(local_dir)
    assert set(manifest) == {"good.bin"}
    assert manifest["good.bin"]["sha256"] == sha256(payload)


def test_stream_snapshot_deletes_corrupt_legacy_staged_file_and_redownloads(tmp_path, monkeypatch):
    payload = b"good"
    snapshot = HubSnapshot(
        "org/model",
        "model",
        "main",
        "commit123",
        [HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")],
    )
    local_dir = tmp_path / "models" / "org" / "model"
    staging_dir = tmp_path / ".tmp" / "downloads" / "repo"
    staged = staging_dir / "file.bin"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"xxxx")

    def fake_stream_file_to_path(snapshot, item, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return file_hashes(destination)

    monkeypatch.setattr(hub_module, "stream_file_to_path", fake_stream_file_to_path)

    hub_module.stream_snapshot(snapshot, local_dir, staging_dir, allow_patterns=None)

    assert not staged.exists()
    assert (local_dir / "file.bin").read_bytes() == payload


def test_stream_file_to_path_promotes_complete_incomplete_file(tmp_path, monkeypatch):
    payload = b"abcdef"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    partial = hub_module.incomplete_path_for(destination)
    partial.write_bytes(payload)

    def fail_transport(snapshot, item):
        raise AssertionError("complete partial should not fetch metadata")

    monkeypatch.setattr(hub_module, "hf_transport_metadata", fail_transport)

    recorder = ProgressRecorder(tmp_path, min_interval_seconds=0, min_bytes=1)
    progress = recorder.track(item.path, total=item.size, stage="starting")
    hashes = hub_module.stream_file_to_path(snapshot, item, destination, progress=progress)

    assert destination.read_bytes() == payload
    assert not partial.exists()
    assert hashes.sha256 == sha256(payload)
    assert progress_snapshot(tmp_path).entries[0].stage == "verifying-partial"


def test_stream_file_to_path_promotes_complete_incomplete_file_without_progress(tmp_path, monkeypatch):
    payload = b"abcdef"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    hub_module.incomplete_path_for(destination).write_bytes(payload)

    def fail_transport(snapshot, item):
        raise AssertionError("complete partial should not fetch metadata")

    monkeypatch.setattr(hub_module, "hf_transport_metadata", fail_transport)

    hashes = hub_module.stream_file_to_path(snapshot, item, destination)

    assert destination.read_bytes() == payload
    assert hashes.sha256 == sha256(payload)


def test_stream_file_to_path_rejects_head_size_mismatch_and_cleans_retry_partial(tmp_path, monkeypatch):
    payload = b"abc"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    hub_module.incomplete_path_for(destination).write_bytes(b"a")

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload) + 1, xet_file_data=object()), {}, "unused"),
    )

    with pytest.raises(DownloadIntegrityError, match="size metadata mismatch"):
        hub_module.stream_file_to_path(snapshot, item, destination)

    assert not hub_module.incomplete_path_for(destination).exists()


def test_stream_file_to_path_rejects_short_download(tmp_path, monkeypatch):
    payload = b"abc"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload), xet_file_data=object()), {}, "unused"),
    )

    def short_xet(xet_file_data, headers, writer, expected_size, rel, resume_size, **kwargs):
        writer.write(b"ab")

    monkeypatch.setattr(hub_module, "stream_xet_file", short_xet)

    with pytest.raises(DownloadIntegrityError, match="downloaded size mismatch"):
        hub_module.stream_file_to_path(snapshot, item, destination)


def test_stream_file_to_path_uses_http_when_xet_metadata_is_absent(tmp_path, monkeypatch):
    payload = b"abc"
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", len(payload), lfs_sha256=sha256(payload), blob_id="pointer")
    destination = tmp_path / "file.bin"
    calls = []

    monkeypatch.setattr(
        hub_module,
        "hf_transport_metadata",
        lambda snapshot, item: (SimpleNamespace(size=len(payload), xet_file_data=None), {"h": "v"}, "https://cdn/file"),
    )

    def fake_stream_http_file(url, headers, writer, expected_size, rel, resume_size, **kwargs):
        calls.append((url, headers, expected_size, rel, resume_size))
        writer.write(payload)

    monkeypatch.setattr(hub_module, "stream_http_file", fake_stream_http_file)

    recorder = ProgressRecorder(tmp_path, min_interval_seconds=0, min_bytes=1)
    progress = recorder.track(item.path, total=item.size, stage="starting")
    hashes = hub_module.stream_file_to_path(snapshot, item, destination, progress=progress)

    assert calls == [("https://cdn/file", {"h": "v"}, len(payload), "file.bin", 0)]
    assert destination.read_bytes() == payload
    assert hashes.sha256 == sha256(payload)
    assert progress_snapshot(tmp_path).entries[0].stage == "verifying"


def test_resumable_prefix_size_handles_missing_and_oversized_partials(tmp_path):
    missing = tmp_path / "missing.incomplete"
    oversized = tmp_path / "file.bin.incomplete"
    oversized.write_bytes(b"abcd")

    assert hub_module.resumable_prefix_size(missing, 3) == 0
    assert hub_module.resumable_prefix_size(oversized, 3) == 0
    assert not oversized.exists()


def test_hf_transport_metadata_rejects_commit_mismatch(monkeypatch):
    import huggingface_hub
    import huggingface_hub.file_download as file_download_module
    import huggingface_hub.utils as utils_module

    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", 3, lfs_sha256="sha", blob_id="pointer")
    monkeypatch.setattr(huggingface_hub, "hf_hub_url", lambda *args, **kwargs: "https://huggingface.co/org/model/file")
    monkeypatch.setattr(utils_module, "build_hf_headers", lambda: {"authorization": "Bearer token"})
    monkeypatch.setattr(
        file_download_module,
        "get_hf_file_metadata",
        lambda **kwargs: SimpleNamespace(
            commit_hash="other",
            location="https://huggingface.co/org/model/file",
            size=3,
            xet_file_data=None,
        ),
    )

    with pytest.raises(DownloadIntegrityError, match="commit metadata mismatch"):
        hub_module.hf_transport_metadata(snapshot, item)


def test_hf_transport_metadata_strips_auth_for_external_http_redirect(monkeypatch):
    import huggingface_hub
    import huggingface_hub.file_download as file_download_module
    import huggingface_hub.utils as utils_module

    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", 3, lfs_sha256="sha", blob_id="pointer")
    monkeypatch.setattr(huggingface_hub, "hf_hub_url", lambda *args, **kwargs: "https://huggingface.co/org/model/file")
    monkeypatch.setattr(utils_module, "build_hf_headers", lambda: {"authorization": "Bearer token", "x": "y"})
    monkeypatch.setattr(
        file_download_module,
        "get_hf_file_metadata",
        lambda **kwargs: SimpleNamespace(
            commit_hash="commit123",
            location="https://cdn.example/file",
            size=3,
            xet_file_data=None,
        ),
    )

    metadata, headers, url = hub_module.hf_transport_metadata(snapshot, item)

    assert metadata.commit_hash == "commit123"
    assert headers == {"x": "y"}
    assert url == "https://cdn.example/file"


def test_hf_transport_metadata_keeps_auth_for_same_host_redirect(monkeypatch):
    import huggingface_hub
    import huggingface_hub.file_download as file_download_module
    import huggingface_hub.utils as utils_module

    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", 3, lfs_sha256="sha", blob_id="pointer")
    monkeypatch.setattr(huggingface_hub, "hf_hub_url", lambda *args, **kwargs: "https://huggingface.co/org/model/file")
    monkeypatch.setattr(utils_module, "build_hf_headers", lambda: {"authorization": "Bearer token", "x": "y"})
    monkeypatch.setattr(
        file_download_module,
        "get_hf_file_metadata",
        lambda **kwargs: SimpleNamespace(
            commit_hash="commit123",
            location="https://huggingface.co/renamed/model/file",
            size=3,
            xet_file_data=None,
        ),
    )

    metadata, headers, url = hub_module.hf_transport_metadata(snapshot, item)

    assert metadata.commit_hash == "commit123"
    assert headers == {"authorization": "Bearer token", "x": "y"}
    assert url == "https://huggingface.co/renamed/model/file"


def test_hf_transport_metadata_keeps_auth_for_xet_metadata(monkeypatch):
    import huggingface_hub
    import huggingface_hub.file_download as file_download_module
    import huggingface_hub.utils as utils_module

    xet = object()
    snapshot = HubSnapshot("org/model", "model", "main", "commit123", [])
    item = HubFile("file.bin", 3, lfs_sha256="sha", blob_id="pointer")
    monkeypatch.setattr(huggingface_hub, "hf_hub_url", lambda *args, **kwargs: "https://huggingface.co/org/model/file")
    monkeypatch.setattr(utils_module, "build_hf_headers", lambda: {"authorization": "Bearer token"})
    monkeypatch.setattr(
        file_download_module,
        "get_hf_file_metadata",
        lambda **kwargs: SimpleNamespace(
            commit_hash=None,
            location="https://cdn.example/file",
            size=3,
            xet_file_data=xet,
        ),
    )

    metadata, headers, url = hub_module.hf_transport_metadata(snapshot, item)

    assert metadata.xet_file_data is xet
    assert headers == {"authorization": "Bearer token"}
    assert url == "https://cdn.example/file"


def test_stream_xet_file_writes_ordered_chunks(monkeypatch, tmp_path):
    import huggingface_hub.utils._xet as xet_utils
    from model_mirror.checksums import HashingWriter

    captured = {}

    class FakeGroup:
        def download_stream(self, file_info, start=None):
            captured["start"] = start
            captured["file_size"] = file_info.file_size
            return [b"a", b"", b"bc"]

    class FakeSession:
        def new_download_stream_group(self, **kwargs):
            captured["group_kwargs"] = kwargs
            return FakeGroup()

    monkeypatch.setattr(xet_utils, "get_xet_session", lambda: FakeSession())
    monkeypatch.setattr(xet_utils, "xet_headers_without_auth", lambda headers: {"xet": headers["h"]})
    path = tmp_path / "out.bin"
    with path.open("wb") as raw:
        writer = HashingWriter(raw, expected_size=6)
        hub_module.stream_xet_file(
            SimpleNamespace(refresh_route="refresh", file_hash="hash"),
            {"h": "v"},
            writer,
            6,
            "file.bin",
            3,
            stall_timeout_seconds=0,
        )

    assert path.read_bytes() == b"abc"
    assert captured["start"] == 3
    assert captured["file_size"] == 6
    assert captured["group_kwargs"]["token_refresh_url"] == "refresh"
    assert captured["group_kwargs"]["custom_headers"] == {"xet": "v"}


def test_stream_xet_file_aborts_on_keyboard_interrupt(monkeypatch, tmp_path):
    import huggingface_hub.utils._xet as xet_utils
    from model_mirror.checksums import HashingWriter

    aborted = []

    class InterruptingStream:
        def __iter__(self):
            raise KeyboardInterrupt

    class FakeGroup:
        def download_stream(self, file_info, start=None):
            return InterruptingStream()

    class FakeSession:
        def new_download_stream_group(self, **kwargs):
            return FakeGroup()

    monkeypatch.setattr(xet_utils, "get_xet_session", lambda: FakeSession())
    monkeypatch.setattr(xet_utils, "xet_headers_without_auth", lambda headers: {})
    monkeypatch.setattr(xet_utils, "abort_xet_session", lambda: aborted.append(True))
    with (tmp_path / "out.bin").open("wb") as raw:
        writer = HashingWriter(raw, expected_size=1)
        with pytest.raises(KeyboardInterrupt):
            hub_module.stream_xet_file(
                SimpleNamespace(refresh_route="refresh", file_hash="hash"),
                {},
                writer,
                1,
                "file.bin",
                0,
                stall_timeout_seconds=0,
            )

    assert aborted == [True]


def test_stream_xet_file_converts_watchdog_abort_to_stall(monkeypatch, tmp_path):
    import huggingface_hub.utils._xet as xet_utils
    from model_mirror.checksums import HashingWriter

    class RaisingStream:
        def __iter__(self):
            raise RuntimeError("aborted")

    class FakeGroup:
        def download_stream(self, file_info, start=None):
            return RaisingStream()

    class FakeSession:
        def new_download_stream_group(self, **kwargs):
            return FakeGroup()

    class FakeWatchdog:
        stalled = True

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def progress(self):
            pass

        def raise_if_stalled(self):
            pass

    monkeypatch.setattr(xet_utils, "get_xet_session", lambda: FakeSession())
    monkeypatch.setattr(xet_utils, "xet_headers_without_auth", lambda headers: {})
    monkeypatch.setattr(hub_module, "StallWatchdog", FakeWatchdog)
    with (tmp_path / "out.bin").open("wb") as raw:
        writer = HashingWriter(raw, expected_size=1)
        with pytest.raises(StallTimeoutError, match="stalled download"):
            hub_module.stream_xet_file(
                SimpleNamespace(refresh_route="refresh", file_hash="hash"),
                {},
                writer,
                1,
                "file.bin",
                0,
                stall_timeout_seconds=1,
            )


def test_stream_xet_file_reraises_non_stall_errors(monkeypatch, tmp_path):
    import huggingface_hub.utils._xet as xet_utils
    from model_mirror.checksums import HashingWriter

    class RaisingStream:
        def __iter__(self):
            raise RuntimeError("not stalled")

    class FakeGroup:
        def download_stream(self, file_info, start=None):
            return RaisingStream()

    class FakeSession:
        def new_download_stream_group(self, **kwargs):
            return FakeGroup()

    monkeypatch.setattr(xet_utils, "get_xet_session", lambda: FakeSession())
    monkeypatch.setattr(xet_utils, "xet_headers_without_auth", lambda headers: {})
    with (tmp_path / "out.bin").open("wb") as raw:
        writer = HashingWriter(raw, expected_size=1)
        with pytest.raises(RuntimeError, match="not stalled"):
            hub_module.stream_xet_file(
                SimpleNamespace(refresh_route="refresh", file_hash="hash"),
                {},
                writer,
                1,
                "file.bin",
                0,
                stall_timeout_seconds=0,
            )


def test_stall_watchdog_calls_abort_callback():
    aborted = []

    with hub_module.StallWatchdog(0.01, "file.bin", abort_callback=lambda: aborted.append(True)) as watchdog:
        time.sleep(0.05)

    assert aborted == [True]
    assert watchdog.stalled is True
    with pytest.raises(StallTimeoutError, match="stalled download"):
        watchdog.raise_if_stalled()


def test_stall_watchdog_can_be_disabled_and_refreshed():
    with hub_module.StallWatchdog(0, "file.bin") as disabled:
        disabled.progress()

    assert disabled.stalled is False

    with hub_module.StallWatchdog(0.05, "file.bin") as watchdog:
        time.sleep(0.01)
        watchdog.progress()
        time.sleep(0.01)

    assert watchdog.stalled is False


def test_stall_watchdog_stalls_without_abort_callback():
    with hub_module.StallWatchdog(0.01, "file.bin") as watchdog:
        time.sleep(0.05)

    assert watchdog.stalled is True


def test_stream_http_file_delegates_resume_to_huggingface(monkeypatch, tmp_path):
    import huggingface_hub.file_download as file_download_module
    from model_mirror.checksums import HashingWriter

    calls = []

    def fake_http_get(**kwargs):
        calls.append(kwargs)
        kwargs["temp_file"].write(b"bc")

    monkeypatch.setattr(file_download_module, "http_get", fake_http_get)
    with (tmp_path / "out.bin").open("wb") as raw:
        writer = HashingWriter(raw, expected_size=3)
        writer.write(b"a")
        hub_module.stream_http_file(
            "https://cdn/file",
            {"h": "v"},
            writer,
            3,
            "file.bin",
            1,
            stall_timeout_seconds=0,
        )

    assert calls[0]["url"] == "https://cdn/file"
    assert calls[0]["headers"] == {"h": "v"}
    assert calls[0]["resume_size"] == 1
    assert calls[0]["displayed_filename"] == "file.bin"
    assert (tmp_path / "out.bin").read_bytes() == b"abc"


def test_stream_http_file_converts_download_timeout(monkeypatch, tmp_path):
    import huggingface_hub.file_download as file_download_module
    from huggingface_hub import constants
    from model_mirror.checksums import HashingWriter

    class ReadTimeout(Exception):
        pass

    previous_timeout = constants.HF_HUB_DOWNLOAD_TIMEOUT

    def fake_http_get(**kwargs):
        raise ReadTimeout("timed out")

    monkeypatch.setattr(file_download_module, "http_get", fake_http_get)
    with (tmp_path / "out.bin").open("wb") as raw:
        writer = HashingWriter(raw, expected_size=3)
        with pytest.raises(StallTimeoutError, match="stalled HTTP download"):
            hub_module.stream_http_file(
                "https://cdn/file",
                {"h": "v"},
                writer,
                3,
                "file.bin",
                1,
                stall_timeout_seconds=2,
            )

    assert constants.HF_HUB_DOWNLOAD_TIMEOUT == previous_timeout


def test_stream_http_file_reraises_non_timeout_errors(monkeypatch, tmp_path):
    import huggingface_hub.file_download as file_download_module
    from model_mirror.checksums import HashingWriter

    def fake_http_get(**kwargs):
        raise ValueError("not a timeout")

    monkeypatch.setattr(file_download_module, "http_get", fake_http_get)
    with (tmp_path / "out.bin").open("wb") as raw:
        writer = HashingWriter(raw, expected_size=3)
        with pytest.raises(ValueError, match="not a timeout"):
            hub_module.stream_http_file(
                "https://cdn/file",
                {"h": "v"},
                writer,
                3,
                "file.bin",
                1,
                stall_timeout_seconds=2,
            )


def test_ensure_hashes_match_rejects_size_blob_and_missing_metadata(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"abc")
    hashes = file_hashes(path)

    with pytest.raises(DownloadIntegrityError, match="size mismatch"):
        hub_module.ensure_hashes_match(HubFile("file.bin", 4, lfs_sha256=hashes.sha256), path, hashes)
    with pytest.raises(DownloadIntegrityError, match="sha256 mismatch"):
        hub_module.ensure_hashes_match(HubFile("file.bin", 3, lfs_sha256="wrong"), path, hashes)
    with pytest.raises(DownloadIntegrityError, match="git blob hash mismatch"):
        hub_module.ensure_hashes_match(HubFile("file.bin", 3, blob_id="wrong"), path, hashes)
    with pytest.raises(DownloadIntegrityError, match="missing remote hash metadata"):
        hub_module.ensure_hashes_match(HubFile("file.bin", 3), path, hashes)
    hub_module.ensure_hashes_match(HubFile("file.bin", 3, blob_id=hashes.git_blob_sha1), path, hashes)


def test_cached_manifest_verifies_rejects_missing_path_and_wrong_blob(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"read")
    row = checksum_row_from_hashes(tmp_path, path, file_hashes(path))
    write_manifest(tmp_path, {"README.md": row})

    assert cached_manifest_verifies(tmp_path, [HubFile("missing.md", 4, blob_id=row["git_blob_sha1"])]) is False

    wrong_blob = HubFile("README.md", 4, blob_id="wrong")
    assert cached_manifest_verifies(tmp_path, [wrong_blob]) is False


def test_require_expected_size_rejects_missing_size():
    with pytest.raises(DownloadIntegrityError, match="missing size metadata"):
        hub_module.require_expected_size(HubFile("file.bin", None, lfs_sha256="sha"))


def test_huggingface_hub_warns_once_when_no_token_is_available(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN_PATH", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODEL_MIRROR_TOKEN_PATH", raising=False)
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path / "home")

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            return SimpleNamespace(sha="commit123", siblings=[])

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=FakeApi),
    )

    hub = HuggingFaceHub(Config(directory=tmp_path))

    hub.files("org/model", "model", "main")
    hub.snapshot_download("org/model", "model", "main", tmp_path / "models" / "org" / "model")

    err = capsys.readouterr().err
    assert err.count("no Hugging Face token found") == 1
    assert "model-mirror config set token-path /path/to/huggingface/token" in err
    assert "hf_secret" not in err
