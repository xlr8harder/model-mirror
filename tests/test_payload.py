import pytest

from model_mirror.payload import (
    PayloadMissingError,
    UnsafePayloadError,
    validate_payload_destination,
    validate_payload_file,
    validate_payload_parent,
    validate_payload_path,
)


@pytest.mark.parametrize(
    "value",
    ["", "/absolute", "../escape", "dir/../escape", "./file", "dir\\file", ".manifest", "a//b"],
)
def test_validate_payload_path_rejects_unsafe_or_noncanonical_values(value):
    with pytest.raises(UnsafePayloadError):
        validate_payload_path(value)


def test_validate_payload_path_accepts_canonical_nested_path():
    assert validate_payload_path("weights/shard.bin") == "weights/shard.bin"


def test_validate_payload_file_distinguishes_missing_and_unsafe_entries(tmp_path):
    with pytest.raises(PayloadMissingError):
        validate_payload_file(tmp_path, tmp_path / "missing.bin", "missing.bin")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(UnsafePayloadError, match="not a regular file"):
        validate_payload_file(tmp_path, directory, "directory")

    target = tmp_path / "target.bin"
    target.write_bytes(b"x")
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    with pytest.raises(UnsafePayloadError, match="symlink"):
        validate_payload_file(tmp_path, link, "link.bin")


def test_validate_payload_destination_rejects_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePayloadError, match="symlink"):
        validate_payload_destination(tmp_path, linked / "file.bin", "linked/file.bin")


def test_validate_payload_parent_rejects_non_directory_parent(tmp_path):
    parent = tmp_path / "parent"
    parent.write_bytes(b"x")

    with pytest.raises(UnsafePayloadError, match="non-directory parent"):
        validate_payload_parent(tmp_path, parent / "file.bin", "parent/file.bin")


def test_validate_payload_file_rejects_mismatched_path_argument(tmp_path):
    path = tmp_path / "actual.bin"
    path.write_bytes(b"x")

    with pytest.raises(UnsafePayloadError, match="expected location"):
        validate_payload_file(tmp_path, path, "different.bin")
