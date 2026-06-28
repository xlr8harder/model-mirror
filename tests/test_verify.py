import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

from model_mirror.checksums import write_checksums
from model_mirror.verify import (
    RemoteVerifyResult,
    git_blob_sha1_file,
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


def test_verify_remote_quick_checks_presence_and_size_only(tmp_path):
    (tmp_path / "weights.safetensors").write_bytes(b"abc")
    metadata = [FakeFile("weights.safetensors", 3, "not-the-real-hash")]

    result = verify_remote(tmp_path, metadata, quick=True)

    assert result.ok is True
    assert result.files_checked == 1
    assert result.hashes_checked == 0


def test_verify_remote_from_checksums_compares_lfs_hashes_without_rehashing(tmp_path):
    payload = b"abc"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "weights.safetensors").write_bytes(payload)
    write_checksums(tmp_path)
    metadata = [FakeFile("weights.safetensors", 3, digest)]

    result = verify_remote(tmp_path, metadata, from_checksums=True)

    assert result.ok is True
    assert result.hashes_checked == 1


def test_git_blob_sha1_file_uses_git_blob_object_format(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"hello\n")
    expected = hashlib.sha1(b"blob 6\0hello\n").hexdigest()

    assert git_blob_sha1_file(path) == expected


def test_verify_remote_validates_regular_git_blob_id(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"abc")
    metadata = [FakeFile("README.md", 3, blob_id=git_blob_sha1_file(path))]

    result = verify_remote(tmp_path, metadata, from_checksums=True)

    assert result.ok is True
    assert result.hashes_checked == 1


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

    result = verify_remote(tmp_path, metadata, quick=True, strict=True)

    assert result.ok is False
    assert result.extras == ["extra.bin"]


def test_verify_remote_reports_missing_checksum_record(tmp_path):
    (tmp_path / "weights.safetensors").write_bytes(b"abc")
    metadata = [FakeFile("weights.safetensors", 3, "0" * 64)]

    result = verify_remote(tmp_path, metadata, from_checksums=True)

    assert result.ok is False
    assert result.hash_missing == ["weights.safetensors"]


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
