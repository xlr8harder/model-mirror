import pytest

import model_mirror.checksums as checksums_module
from model_mirror.checksums import (
    load_records,
    record_is_current,
    update_checksums,
    verify_checksums,
    write_checksums,
)


def test_write_and_verify_checksums_excludes_metadata(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / ".model-mirror").mkdir()
    (tmp_path / ".model-mirror" / "meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "partial").write_text("partial", encoding="utf-8")
    (tmp_path / ".verification.lock").write_text("", encoding="utf-8")
    (tmp_path / ".verification.tmp").write_text("partial", encoding="utf-8")
    (tmp_path / ".checksums.tmp").write_text("partial", encoding="utf-8")
    (tmp_path / ".manifest.tmp").write_text("partial", encoding="utf-8")

    write_checksums(tmp_path)
    checksums, manifest = load_records(tmp_path)

    assert set(checksums) == {"a.txt"}
    assert manifest["a.txt"]["size"] == 5
    result = verify_checksums(tmp_path)
    assert result.ok is True
    assert result.checked == 1


def test_write_checksums_skips_current_manifest_records(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    first = write_checksums(tmp_path)
    assert first.hashed == 1

    calls = []

    def fail_if_called(path):
        calls.append(path)
        raise AssertionError("current file should not be rehashed")

    monkeypatch.setattr(checksums_module, "sha256_file", fail_if_called)
    second = write_checksums(tmp_path)

    assert second.hashed == 0
    assert second.skipped == 1
    assert calls == []


def test_write_checksums_checkpoints_completed_files_before_failure(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    def fake_sha(path):
        if path.name == "b.txt":
            raise RuntimeError("boom")
        return "digest-a"

    monkeypatch.setattr(checksums_module, "sha256_file", fake_sha)
    with pytest.raises(RuntimeError):
        write_checksums(tmp_path)

    checksums, manifest = load_records(tmp_path)
    assert checksums == {"a.txt": "digest-a"}
    assert set(manifest) == {"a.txt"}


def test_write_checksums_supports_multiple_workers(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    result = write_checksums(tmp_path, max_workers=2)

    checksums, manifest = load_records(tmp_path)
    assert result.hashed == 2
    assert set(checksums) == {"a.txt", "b.txt"}
    assert set(manifest) == {"a.txt", "b.txt"}


def test_write_checksums_removes_records_for_deleted_files(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "a.txt").unlink()

    result = write_checksums(tmp_path)

    checksums, manifest = load_records(tmp_path)
    assert result.removed == 1
    assert checksums == {}
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

    checksums, manifest = load_records(tmp_path)
    assert set(checksums) == {"a.txt"}
    assert manifest["a.txt"]["size"] == 7
    assert verify_checksums(tmp_path).ok is True


def test_update_checksums_removes_missing_record_without_work(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    write_checksums(tmp_path)
    (tmp_path / "a.txt").unlink()

    result = update_checksums(tmp_path, ["a.txt"])

    checksums, manifest = load_records(tmp_path)
    assert result.removed == 1
    assert checksums == {}
    assert manifest == {}


def test_update_checksums_ignores_missing_untracked_path(tmp_path):
    result = update_checksums(tmp_path, ["not-tracked.bin"])

    assert result.removed == 0
    assert result.hashed == 0


def test_record_is_current_rejects_missing_manifest_row():
    assert record_is_current(None, 1, 2) is False


def test_update_checksums_supports_multiple_workers(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")

    result = update_checksums(tmp_path, ["a.txt", "b.txt"], max_workers=2)

    checksums, manifest = load_records(tmp_path)
    assert result.hashed == 2
    assert set(checksums) == {"a.txt", "b.txt"}
    assert set(manifest) == {"a.txt", "b.txt"}


def test_verify_checksums_without_records_is_empty_success(tmp_path):
    result = verify_checksums(tmp_path)

    assert result.ok is True
    assert result.checked == 0


def test_load_records_rejects_malformed_checksum_file(tmp_path):
    (tmp_path / ".checksums").write_text("not split correctly\n", encoding="utf-8")

    try:
        load_records(tmp_path)
    except ValueError as exc:
        assert "Malformed line" in str(exc)
    else:
        raise AssertionError("malformed checksum file should fail")


def test_load_records_rejects_malformed_manifest_file(tmp_path):
    (tmp_path / ".manifest").write_text("{not-json}\n", encoding="utf-8")

    try:
        load_records(tmp_path)
    except ValueError as exc:
        assert "Malformed line" in str(exc)
    else:
        raise AssertionError("malformed manifest file should fail")


def test_load_records_ignores_blank_lines(tmp_path):
    (tmp_path / ".checksums").write_text("\nabc  a.txt\n\n", encoding="utf-8")
    (tmp_path / ".manifest").write_text('\n{"path": "a.txt", "size": 1, "mtime_ns": 2}\n\n', encoding="utf-8")

    checksums, manifest = load_records(tmp_path)

    assert checksums == {"a.txt": "abc"}
    assert manifest == {"a.txt": {"path": "a.txt", "size": 1, "mtime_ns": 2}}
