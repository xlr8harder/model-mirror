import hashlib
import json

import pytest

import model_mirror.checksums as checksums_module
from model_mirror.checksums import (
    FileHashes,
    HashingWriter,
    MANIFEST_SCHEMA,
    MANIFEST_VERSION,
    file_hashes,
    hash_file_prefix,
    load_manifest,
    record_is_current,
    update_checksums,
    verify_checksums,
    write_checksums,
)


def git_blob_sha1(payload: bytes) -> str:
    return hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()


def test_write_and_verify_checksums_excludes_metadata_and_writes_versioned_manifest(tmp_path):
    payload = b"alpha"
    (tmp_path / "a.txt").write_bytes(payload)
    (tmp_path / ".model-mirror").mkdir()
    (tmp_path / ".model-mirror" / "meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "partial").write_text("partial", encoding="utf-8")
    (tmp_path / ".verification.lock").write_text("", encoding="utf-8")
    (tmp_path / ".verification.tmp").write_text("partial", encoding="utf-8")
    (tmp_path / ".checksums").write_text("obsolete", encoding="utf-8")
    (tmp_path / ".checksums.tmp").write_text("obsolete", encoding="utf-8")
    (tmp_path / ".manifest.tmp").write_text("partial", encoding="utf-8")

    write_checksums(tmp_path)
    manifest = load_manifest(tmp_path)

    header = json.loads((tmp_path / ".manifest").read_text(encoding="utf-8").splitlines()[0])
    assert header == {"schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION}
    assert set(manifest) == {"a.txt"}
    assert manifest["a.txt"]["size"] == 5
    assert manifest["a.txt"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["a.txt"]["git_blob_sha1"] == git_blob_sha1(payload)
    assert not (tmp_path / ".checksums").exists()
    assert not (tmp_path / ".checksums.tmp").exists()

    result = verify_checksums(tmp_path)
    assert result.ok is True
    assert result.checked == 1


def test_file_hashes_computes_sha256_and_git_blob_sha1_in_one_call(tmp_path):
    payload = b"hello\n"
    path = tmp_path / "README.md"
    path.write_bytes(payload)

    hashes = file_hashes(path)

    assert hashes.sha256 == hashlib.sha256(payload).hexdigest()
    assert hashes.git_blob_sha1 == git_blob_sha1(payload)


def test_hashing_writer_resets_hashes_when_http_retry_truncates(tmp_path):
    path = tmp_path / "file.bin"
    with path.open("wb") as raw:
        writer = HashingWriter(raw, expected_size=3)
        writer.write(b"bad")
        writer.seek(0)
        writer.truncate()
        writer.write(b"abc")
        hashes = writer.hashes

    assert path.read_bytes() == b"abc"
    assert hashes.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert hashes.git_blob_sha1 == git_blob_sha1(b"abc")


def test_hashing_writer_delegates_file_methods_and_rejects_nonzero_truncate(tmp_path):
    path = tmp_path / "file.bin"
    with path.open("wb") as raw:
        writer = HashingWriter(raw, expected_size=4)
        writer.write(b"abcd")
        assert writer.tell() == 4
        writer.flush()
        assert writer.fileno() == raw.fileno()
        assert writer.truncate(4) == 4
        with pytest.raises(OSError, match="non-zero truncate"):
            writer.truncate(2)


def test_hash_file_prefix_rejects_short_read(tmp_path):
    path = tmp_path / "file.bin"
    path.write_bytes(b"abc")

    with pytest.raises(OSError, match="short read"):
        hash_file_prefix(path, total_size=4, prefix_size=4)


def test_write_checksums_skips_current_manifest_records(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    first = write_checksums(tmp_path)
    assert first.hashed == 1

    calls = []

    def fail_if_called(path):
        calls.append(path)
        raise AssertionError("current file should not be rehashed")

    monkeypatch.setattr(checksums_module, "file_hashes", fail_if_called)
    second = write_checksums(tmp_path)

    assert second.hashed == 0
    assert second.skipped == 1
    assert calls == []


def test_write_checksums_checkpoints_completed_files_before_failure(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    def fake_hashes(path):
        if path.name == "b.txt":
            raise RuntimeError("boom")
        return FileHashes(sha256="sha-a", git_blob_sha1="blob-a")

    monkeypatch.setattr(checksums_module, "file_hashes", fake_hashes)
    with pytest.raises(RuntimeError):
        write_checksums(tmp_path)

    manifest = load_manifest(tmp_path)
    assert set(manifest) == {"a.txt"}
    assert manifest["a.txt"]["sha256"] == "sha-a"
    assert manifest["a.txt"]["git_blob_sha1"] == "blob-a"


def test_write_checksums_supports_multiple_workers(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    result = write_checksums(tmp_path, max_workers=2)

    manifest = load_manifest(tmp_path)
    assert result.hashed == 2
    assert set(manifest) == {"a.txt", "b.txt"}


def test_write_checksums_removes_records_for_deleted_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "a.txt").unlink()

    result = write_checksums(tmp_path)

    manifest = load_manifest(tmp_path)
    assert result.removed == 1
    assert manifest == {}


def test_verify_checksums_reports_tampering(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")

    result = verify_checksums(tmp_path)

    assert result.ok is False
    assert result.failures == ["a.txt"]


def test_verify_checksums_strict_reports_extra_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    result = verify_checksums(tmp_path, strict=True)

    assert result.ok is False
    assert result.extras == ["b.txt"]


def test_verify_checksums_reports_missing_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "a.txt").unlink()

    result = verify_checksums(tmp_path)

    assert result.ok is False
    assert result.missing == ["a.txt"]


def test_update_checksums_rewrites_changed_paths_and_removes_missing_paths(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    write_checksums(tmp_path)

    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    (tmp_path / "b.txt").unlink()
    update_checksums(tmp_path, ["a.txt", "b.txt"])

    manifest = load_manifest(tmp_path)
    assert set(manifest) == {"a.txt"}
    assert manifest["a.txt"]["size"] == 7
    assert verify_checksums(tmp_path).ok is True


def test_update_checksums_removes_missing_record_without_work(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "a.txt").unlink()

    result = update_checksums(tmp_path, ["a.txt"])

    manifest = load_manifest(tmp_path)
    assert result.removed == 1
    assert manifest == {}


def test_update_checksums_ignores_missing_untracked_path(tmp_path):
    result = update_checksums(tmp_path, ["not-tracked.bin"])

    assert result.removed == 0
    assert result.hashed == 0


def test_record_is_current_rejects_missing_or_incomplete_manifest_rows():
    assert record_is_current(None, 1, 2) is False
    assert record_is_current({"size": 1, "mtime_ns": 2, "sha256": "sha"}, 1, 2) is False
    assert record_is_current({"size": 1, "mtime_ns": 2, "git_blob_sha1": "blob"}, 1, 2) is False


def test_update_checksums_supports_multiple_workers(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    result = update_checksums(tmp_path, ["a.txt", "b.txt"], max_workers=2)

    manifest = load_manifest(tmp_path)
    assert result.hashed == 2
    assert set(manifest) == {"a.txt", "b.txt"}


def test_verify_checksums_without_records_is_empty_success(tmp_path):
    result = verify_checksums(tmp_path)

    assert result.ok is True
    assert result.checked == 0


def test_load_manifest_rejects_malformed_manifest_file(tmp_path):
    (tmp_path / ".manifest").write_text("{not-json}\n", encoding="utf-8")

    try:
        load_manifest(tmp_path)
    except ValueError as exc:
        assert "Malformed line" in str(exc)
    else:
        raise AssertionError("malformed manifest file should fail")


def test_load_manifest_requires_header(tmp_path):
    (tmp_path / ".manifest").write_text('{"path": "a.txt"}\n', encoding="utf-8")

    try:
        load_manifest(tmp_path)
    except ValueError as exc:
        assert "missing header" in str(exc)
    else:
        raise AssertionError("headerless manifest should fail")


def test_load_manifest_rejects_unknown_schema_or_version(tmp_path):
    for row, expected in (
        ({"schema": "other", "version": MANIFEST_VERSION}, "schema"),
        ({"schema": MANIFEST_SCHEMA, "version": 999}, "version"),
    ):
        (tmp_path / ".manifest").write_text(json.dumps(row) + "\n", encoding="utf-8")
        try:
            load_manifest(tmp_path)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"unsupported manifest {expected} should fail")


def test_load_manifest_rejects_rows_without_path(tmp_path):
    header = {"schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION}
    (tmp_path / ".manifest").write_text(json.dumps(header) + "\n{}\n", encoding="utf-8")

    try:
        load_manifest(tmp_path)
    except ValueError as exc:
        assert "Malformed line" in str(exc)
    else:
        raise AssertionError("manifest row without path should fail")


def test_load_manifest_ignores_blank_lines(tmp_path):
    header = {"schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION}
    row = {"path": "a.txt", "size": 1, "mtime_ns": 2, "sha256": "sha", "git_blob_sha1": "blob"}
    (tmp_path / ".manifest").write_text(
        "\n" + json.dumps(header) + "\n\n" + json.dumps(row) + "\n\n",
        encoding="utf-8",
    )

    manifest = load_manifest(tmp_path)

    assert manifest == {"a.txt": row}
