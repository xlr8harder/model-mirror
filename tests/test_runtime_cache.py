import errno
import json
from pathlib import Path

import pytest

import model_mirror.runtime_cache as runtime_cache
from model_mirror.config import Config, archive_path
from model_mirror.lock import ModelLock
from model_mirror.runtime_cache import (
    DOWNLOAD_RECORD_FILE,
    CacheIssue,
    RuntimeCacheLock,
    active_repository_locks,
    inspect_runtime_cache,
    inspect_staging_dir,
    path_has_entries,
    read_download_record,
    write_download_record,
)
from model_mirror.state import VerificationState, write_verification_state


def write_record(staging, config, *, repo_id="org/model", repo_type="model", commit="commit123"):
    destination = archive_path(config, repo_id, repo_type) if repo_type in {"model", "dataset", "space"} else staging
    return write_download_record(
        staging,
        repo_id=repo_id,
        repo_type=repo_type,
        requested_revision="main",
        resolved_commit=commit,
        destination=destination,
        allow_patterns=["z.bin", "a.bin"],
    )


def test_download_record_round_trip_preserves_creation_for_same_operation(tmp_path):
    config = Config(directory=tmp_path)
    staging = tmp_path / "staging"

    first = write_record(staging, config)
    second = write_record(staging, config)
    loaded = read_download_record(staging)

    assert first.created_at_utc == second.created_at_utc
    assert loaded == second
    assert loaded.allow_patterns == ["a.bin", "z.bin"]

    changed = write_download_record(
        staging,
        repo_id="org/model",
        repo_type="model",
        requested_revision="dev",
        resolved_commit="different",
        destination=archive_path(config, "org/model", "model"),
        allow_patterns=None,
    )
    assert changed.resolved_commit == "different"
    assert changed.allow_patterns is None


def test_write_download_record_replaces_unreadable_existing_record(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / DOWNLOAD_RECORD_FILE).write_text("{", encoding="utf-8")

    record = write_record(staging, Config(directory=tmp_path))

    assert record.repo_id == "org/model"
    assert read_download_record(staging) == record


@pytest.mark.parametrize(
    "payload,match",
    [
        ([], "JSON object"),
        ({"schema": "wrong", "version": 1}, "unsupported"),
        ({"schema": "model-mirror-download", "version": 1}, "missing or invalid"),
        (
            {
                "schema": "model-mirror-download",
                "version": 1,
                "repo_id": "org/model",
                "repo_type": "model",
                "requested_revision": "main",
                "resolved_commit": "commit123",
                "destination": "/tmp/model",
                "created_at_utc": "now",
                "last_started_at_utc": "now",
                "allow_patterns": [1],
            },
            "invalid allow_patterns",
        ),
    ],
)
def test_read_download_record_rejects_invalid_metadata(tmp_path, payload, match):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / DOWNLOAD_RECORD_FILE).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=match):
        read_download_record(staging)


def test_runtime_cache_lock_handles_noop_exit_and_unexpected_flock_error(tmp_path, monkeypatch):
    lock = RuntimeCacheLock(Config(directory=tmp_path), exclusive=True)
    assert lock.__exit__(None, None, None) is False

    def unexpected(*args):
        raise OSError(errno.EINVAL, "unexpected")

    monkeypatch.setattr(runtime_cache.fcntl, "flock", unexpected)
    with pytest.raises(OSError, match="unexpected"):
        lock.__enter__()
    assert lock.handle is None


def test_inspect_runtime_cache_reports_global_legacy_temporary_and_mirror_state(tmp_path):
    config = Config(directory=tmp_path)
    (tmp_path / ".model-mirror" / "cache").mkdir(parents=True)
    (tmp_path / ".model-mirror" / "cache" / "blob").write_bytes(b"x")
    (tmp_path / ".model-mirror" / "tmp").mkdir()
    (tmp_path / ".model-mirror" / "tmp" / "other").write_bytes(b"x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "legacy").write_bytes(b"x")
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "legacy").write_bytes(b"x")
    mirror_cache = tmp_path / "models" / "org" / "model" / ".cache"
    mirror_cache.mkdir(parents=True)
    (mirror_cache / "legacy").write_bytes(b"x")

    issues = inspect_runtime_cache(config)

    assert {
        (issue.tag, issue.reason)
        for issue in issues
    } == {
        ("untracked-cache", "runtime-cache-without-active-operation"),
        ("untracked-cache", "untracked-temporary-data"),
        ("stale-cache", "legacy-cache-layout"),
        ("stale-cache", "legacy-mirror-cache"),
    }
    assert all(issue.actions[-1] == "remove: model-mirror clean-cache --force" for issue in issues)


def test_inspect_runtime_cache_reports_invalid_runtime_roots(tmp_path):
    config = Config(directory=tmp_path)
    tmp_root = tmp_path / ".model-mirror" / "tmp"
    tmp_root.mkdir(parents=True)
    (tmp_root / "downloads").write_bytes(b"x")

    issues = inspect_runtime_cache(config)
    assert [issue.reason for issue in issues] == ["invalid-downloads-root"]

    other = tmp_path / "other"
    other.write_bytes(b"x")
    issues = inspect_runtime_cache(Config(directory=tmp_path, tmp_dir=other))
    assert [issue.reason for issue in issues] == ["invalid-temporary-root"]


def test_inspect_staging_classifies_invalid_untracked_and_unknown_entries(tmp_path):
    config = Config(directory=tmp_path)
    invalid = tmp_path / "invalid"
    invalid.write_bytes(b"x")
    assert inspect_staging_dir(config, invalid, {})[0].reason == "invalid-staging-entry"

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / DOWNLOAD_RECORD_FILE).write_text("{", encoding="utf-8")
    assert inspect_staging_dir(config, unreadable, {})[0].reason == "unreadable-download-record"

    missing = tmp_path / "missing"
    missing.mkdir()
    assert inspect_staging_dir(config, missing, {})[0].reason == "missing-download-record"

    unknown = tmp_path / "unknown"
    write_record(unknown, config, repo_type="unknown")
    assert inspect_staging_dir(config, unknown, {})[0].reason == "unknown-repository-type"


def test_inspect_staging_classifies_destination_metadata_and_lifecycle(tmp_path, monkeypatch):
    config = Config(directory=tmp_path)

    mismatch = tmp_path / "mismatch"
    write_record(mismatch, config)
    data_path = mismatch / DOWNLOAD_RECORD_FILE
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["destination"] = str(tmp_path / "elsewhere")
    data_path.write_text(json.dumps(data), encoding="utf-8")
    assert inspect_staging_dir(config, mismatch, {})[0].reason == "destination-mismatch"

    malformed_state = tmp_path / "malformed-state"
    write_record(malformed_state, config)
    root = archive_path(config, "org/model", "model")
    root.mkdir(parents=True)
    (root / ".verification").write_text("- invalid", encoding="utf-8")
    assert inspect_staging_dir(config, malformed_state, {})[0].reason == "interrupted-download"

    completed = tmp_path / "completed"
    write_record(completed, config)
    write_verification_state(
        root,
        VerificationState(status="clean", repo_id="org/model", resolved_commit="commit123"),
    )
    assert inspect_staging_dir(config, completed, {})[0].reason == "completed-download-residue"

    orphan = tmp_path / "orphan"
    write_record(orphan, config, repo_id="gone/model")
    assert inspect_staging_dir(config, orphan, {})[0].reason == "orphaned-download"

    original_resolve = Path.resolve

    def fail_recorded_destination(self, *args, **kwargs):
        if self == Path(str(tmp_path / "elsewhere")):
            raise OSError("cannot resolve")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_recorded_destination)
    assert inspect_staging_dir(config, mismatch, {})[0].reason == "destination-mismatch"


def test_active_repository_locks_are_detected_and_hide_owned_cache(tmp_path):
    config = Config(directory=tmp_path)
    root = archive_path(config, "org/model", "model")
    cache = root / ".cache"
    cache.mkdir(parents=True)
    (cache / "blob").write_bytes(b"x")

    with ModelLock(root, "mirror", "org/model"):
        active = active_repository_locks(config)
        assert active[("model", "org/model")]["command"] == "mirror"
        assert inspect_runtime_cache(config) == []


def test_path_has_entries_handles_missing_empty_file_and_symlink(tmp_path):
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.mkdir()
    file_path = tmp_path / "file"
    file_path.write_bytes(b"x")
    symlink = tmp_path / "link"
    symlink.symlink_to(file_path)

    assert path_has_entries(missing) is False
    assert path_has_entries(empty) is False
    assert path_has_entries(file_path) is True
    assert path_has_entries(symlink) is True


def test_cache_issue_tag_distinguishes_stale_and_untracked(tmp_path):
    assert CacheIssue("stale", "reason", "label", tmp_path).tag == "stale-cache"
    assert CacheIssue("untracked", "reason", "label", tmp_path).tag == "untracked-cache"
