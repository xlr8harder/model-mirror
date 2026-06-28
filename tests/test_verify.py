import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import model_mirror.verify as verify_module
from model_mirror.checksums import file_hashes, write_checksums
from model_mirror.verify import (
    RemoteVerifyResult,
    current_manifest_hash,
    merge_checksum_result,
    metadata_blob_id,
    metadata_lfs_sha256,
    metadata_path,
    verify_remote,
)


@dataclass
class FakeFile:
    path: str
    size: int
    lfs_sha256: str | None = None
    blob_id: str | None = None


def test_verify_remote_can_check_presence_and_size_only(tmp_path):
    (tmp_path / "weights.safetensors").write_bytes(b"abc")
    metadata = [FakeFile("weights.safetensors", 3, "not-the-real-hash")]

    result = verify_remote(tmp_path, metadata, check_hashes=False)

    assert result.ok is True
    assert result.files_checked == 1
    assert result.hashes_checked == 0


def test_verify_remote_cached_compares_lfs_hashes_without_rehashing(tmp_path, monkeypatch):
    payload = b"abc"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "weights.safetensors").write_bytes(payload)
    write_checksums(tmp_path)
    metadata = [FakeFile("weights.safetensors", 3, digest)]

    def fail_if_called(path):
        raise AssertionError("cached verify should not hash file bytes")

    monkeypatch.setattr(verify_module, "file_hashes", fail_if_called)
    result = verify_remote(tmp_path, metadata, cached=True)

    assert result.ok is True
    assert result.hashes_checked == 1


def test_verify_remote_full_hashes_lfs_when_manifest_is_not_used(tmp_path):
    payload = b"abc"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "weights.safetensors").write_bytes(payload)
    metadata = [FakeFile("weights.safetensors", 3, digest)]

    result = verify_remote(tmp_path, metadata)

    assert result.ok is True
    assert result.hashes_checked == 1


def test_verify_remote_cached_reports_missing_or_stale_manifest_hashes(tmp_path):
    stale = tmp_path / "stale-cache.safetensors"
    stale.write_bytes(b"abc")
    write_checksums(tmp_path)
    (tmp_path / "missing-cache.safetensors").write_bytes(b"abc")
    stale.write_bytes(b"abcd")
    metadata = [
        FakeFile("missing-cache.safetensors", 3, hashlib.sha256(b"abc").hexdigest()),
        FakeFile("stale-cache.safetensors", 4, hashlib.sha256(b"abcd").hexdigest()),
    ]

    result = verify_remote(tmp_path, metadata, cached=True)

    assert result.ok is False
    assert result.cached_hash_missing == ["missing-cache.safetensors", "stale-cache.safetensors"]


def test_current_manifest_hash_rejects_missing_rows():
    assert current_manifest_hash({}, "file.bin", 1, 2, "sha256") is None


def test_verify_remote_validates_regular_git_blob_id_from_cached_manifest(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"abc")
    write_checksums(tmp_path)
    metadata = [FakeFile("README.md", 3, blob_id=file_hashes(path).git_blob_sha1)]

    result = verify_remote(tmp_path, metadata, cached=True)

    assert result.ok is True
    assert result.hashes_checked == 1


def test_verify_remote_cached_reports_missing_regular_git_blob_hash(tmp_path):
    (tmp_path / "README.md").write_bytes(b"abc")
    metadata = [FakeFile("README.md", 3, blob_id="blob123")]

    result = verify_remote(tmp_path, metadata, cached=True)

    assert result.ok is False
    assert result.cached_hash_missing == ["README.md"]


def test_verify_remote_reports_regular_git_blob_mismatch(tmp_path):
    (tmp_path / "README.md").write_bytes(b"abc")
    metadata = [FakeFile("README.md", 3, blob_id="0" * 40)]

    result = verify_remote(tmp_path, metadata)

    assert result.ok is False
    assert result.hash_mismatches == ["README.md"]


def test_verify_remote_reports_missing_size_and_hash_failures(tmp_path):
    (tmp_path / "wrong-size.bin").write_bytes(b"abc")
    (tmp_path / "wrong-hash.bin").write_bytes(b"abc")
    metadata = [
        FakeFile("missing.bin", 1, None),
        FakeFile("wrong-size.bin", 4, None),
        FakeFile("wrong-hash.bin", 3, "0" * 64),
    ]

    result = verify_remote(tmp_path, metadata)

    assert result.ok is False
    assert result.missing == ["missing.bin"]
    assert result.size_mismatches == ["wrong-size.bin"]
    assert result.hash_mismatches == ["wrong-hash.bin"]


def test_verify_remote_strict_reports_extra_payload_files(tmp_path):
    (tmp_path / "expected.bin").write_bytes(b"a")
    (tmp_path / "extra.bin").write_bytes(b"b")
    metadata = [FakeFile("expected.bin", 1, None)]

    result = verify_remote(tmp_path, metadata, check_hashes=False, strict=True)

    assert result.ok is False
    assert result.extras == ["extra.bin"]


def test_metadata_adapters_support_huggingface_sibling_shape():
    lfs = SimpleNamespace(sha256="abc")
    sibling = SimpleNamespace(rfilename="file.bin", size=1, lfs=lfs, blob_id="blob123")

    assert metadata_path(sibling) == "file.bin"
    assert metadata_lfs_sha256(sibling) == "abc"
    assert metadata_blob_id(sibling) == "blob123"


def test_merge_checksum_result_adds_only_new_paths():
    remote = RemoteVerifyResult(missing=["a"], hash_mismatches=["b"], extras=["c"])
    checksum = SimpleNamespace(missing=["a", "d"], failures=["b", "e"], extras=["c", "f"])

    merge_checksum_result(remote, checksum)

    assert remote.missing == ["a", "d"]
    assert remote.hash_mismatches == ["b", "e"]
    assert remote.extras == ["c", "f"]
