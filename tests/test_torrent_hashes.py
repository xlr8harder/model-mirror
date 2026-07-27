import hashlib

import libtorrent as lt
import pytest

from model_mirror.torrent_hashes import (
    BLOCK_LENGTH,
    ZERO_HASH,
    TorrentFileHasher,
    merkle_root,
    next_power_of_two,
    zero_subtree_root,
)


def test_streaming_hybrid_hashes_match_libtorrent(tmp_path):
    piece_length = 4 * BLOCK_LENGTH
    root = tmp_path / "payload"
    root.mkdir()
    payloads = {
        "a-empty.bin": b"",
        "b-one.bin": b"x",
        "c-block.bin": b"b" * BLOCK_LENGTH,
        "d-block-plus.bin": bytes(range(251)) * 66,
        "e-piece-plus.bin": bytes(range(199)) * 400,
        "f-two-pieces.bin": b"z" * (2 * piece_length),
        "z-final-partial.bin": b"end" * 17000,
    }
    for rel, payload in payloads.items():
        (root / rel).write_bytes(payload)

    storage = lt.file_storage()
    for rel in sorted(payloads):
        storage.add_file(f"{root.name}/{rel}", len(payloads[rel]))
    creator = lt.create_torrent(storage, piece_length, 0)
    lt.set_piece_hashes(creator, str(root.parent))
    metainfo = creator.generate()
    info = metainfo[b"info"]
    expected_v1 = info[b"pieces"]
    actual_v1 = bytearray()

    for rel in sorted(payloads):
        payload = payloads[rel]
        hasher = TorrentFileHasher(
            expected_size=len(payload),
            piece_length=piece_length,
            pad_v1_tail=True,
        )
        hasher.update(b"")
        for offset in range(0, len(payload), 7777):
            hasher.update(payload[offset : offset + 7777])
        hashes = hasher.finalize()
        assert hasher.finalize() is hashes
        actual_v1.extend(b"".join(hashes.v1_piece_hashes))

        file_entry = info[b"file tree"][rel.encode("utf-8")][b""]
        assert hashes.v2_file_root == file_entry.get(b"pieces root")
        if len(payload) > piece_length:
            assert metainfo[b"piece layers"][hashes.v2_file_root] == b"".join(hashes.v2_piece_hashes)
        else:
            assert hashes.v2_file_root not in metainfo[b"piece layers"]

    assert bytes(actual_v1) == expected_v1


def test_torrent_file_hasher_reset_validation_and_unpadded_tail():
    with pytest.raises(ValueError, match="must not be negative"):
        TorrentFileHasher(expected_size=-1, piece_length=BLOCK_LENGTH, pad_v1_tail=True)
    for invalid_piece_length in (BLOCK_LENGTH - 1, 3 * BLOCK_LENGTH):
        with pytest.raises(ValueError, match="power of two"):
            TorrentFileHasher(
                expected_size=1,
                piece_length=invalid_piece_length,
                pad_v1_tail=True,
            )

    too_much = TorrentFileHasher(expected_size=1, piece_length=BLOCK_LENGTH, pad_v1_tail=True)
    with pytest.raises(ValueError, match="more bytes"):
        too_much.update(b"ab")

    incomplete = TorrentFileHasher(expected_size=2, piece_length=BLOCK_LENGTH, pad_v1_tail=True)
    incomplete.update(b"a")
    with pytest.raises(ValueError, match="expected 2 bytes, got 1"):
        incomplete.finalize()

    hasher = TorrentFileHasher(expected_size=1, piece_length=BLOCK_LENGTH, pad_v1_tail=False)
    hasher.update(b"x")
    hashes = hasher.finalize()
    assert hashes.v1_piece_hashes == (hashlib.sha1(b"x").digest(),)
    with pytest.raises(RuntimeError, match="already finalized"):
        hasher.update(b"x")

    hasher.reset()
    hasher.update(b"y")
    assert hasher.finalize().v1_piece_hashes == (hashlib.sha1(b"y").digest(),)


def test_merkle_helpers_validate_inputs_and_compute_zero_subtrees():
    assert next_power_of_two(1) == 1
    assert next_power_of_two(3) == 4
    with pytest.raises(ValueError, match="positive"):
        next_power_of_two(0)

    assert zero_subtree_root(1) == ZERO_HASH
    assert zero_subtree_root(2) == hashlib.sha256(ZERO_HASH + ZERO_HASH).digest()
    for invalid_count in (0, 3):
        with pytest.raises(ValueError, match="positive power of two"):
            zero_subtree_root(invalid_count)

    value = hashlib.sha256(b"value").digest()
    assert merkle_root([value], target_count=1, empty_hash=ZERO_HASH) == value
    with pytest.raises(ValueError, match="at least one"):
        merkle_root([], target_count=1, empty_hash=ZERO_HASH)
    for invalid_target in (0, 3):
        with pytest.raises(ValueError, match="power of two"):
            merkle_root([value, value], target_count=invalid_target, empty_hash=ZERO_HASH)
