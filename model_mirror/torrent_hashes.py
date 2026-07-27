from __future__ import annotations

import hashlib
from dataclasses import dataclass


BLOCK_LENGTH = 16 * 1024
ZERO_HASH = bytes(32)


@dataclass(frozen=True, slots=True)
class TorrentFileHashes:
    v1_piece_hashes: tuple[bytes, ...]
    v2_piece_hashes: tuple[bytes, ...]
    v2_file_root: bytes | None


class TorrentFileHasher:
    """Accumulate hybrid v1/v2 hashes for one file in canonical file order."""

    def __init__(self, *, expected_size: int, piece_length: int, pad_v1_tail: bool):
        if expected_size < 0:
            raise ValueError("expected size must not be negative")
        if piece_length < BLOCK_LENGTH or piece_length & (piece_length - 1):
            raise ValueError("piece length must be a power of two and at least 16 KiB")
        self.expected_size = expected_size
        self.piece_length = piece_length
        self.pad_v1_tail = pad_v1_tail
        self._blocks_per_piece = piece_length // BLOCK_LENGTH
        self.reset()

    def reset(self) -> None:
        self._bytes_hashed = 0
        self._v1_hasher = hashlib.sha1()
        self._v1_piece_bytes = 0
        self._v1_piece_hashes: list[bytes] = []
        self._block_buffer = bytearray()
        self._piece_leaves: list[bytes] = []
        self._v2_piece_hashes: list[bytes] = []
        self._result: TorrentFileHashes | None = None

    @property
    def bytes_hashed(self) -> int:
        return self._bytes_hashed

    def update(self, data: bytes) -> None:
        if self._result is not None:
            raise RuntimeError("torrent file hasher is already finalized")
        if not data:
            return
        if self._bytes_hashed + len(data) > self.expected_size:
            raise ValueError("torrent file hasher received more bytes than expected")
        self._update_v1(data)
        self._update_v2(data)
        self._bytes_hashed += len(data)

    def finalize(self) -> TorrentFileHashes:
        if self._result is not None:
            return self._result
        if self._bytes_hashed != self.expected_size:
            raise ValueError(
                f"torrent file hasher expected {self.expected_size} bytes, got {self._bytes_hashed}"
            )

        if self._v1_piece_bytes:
            if self.pad_v1_tail:
                self._v1_hasher.update(bytes(self.piece_length - self._v1_piece_bytes))
            self._v1_piece_hashes.append(self._v1_hasher.digest())

        if self._block_buffer:
            self._finish_block(bytes(self._block_buffer))
            self._block_buffer.clear()
        if self._piece_leaves:
            target_blocks = (
                next_power_of_two(len(self._piece_leaves))
                if self.expected_size <= self.piece_length
                else self._blocks_per_piece
            )
            self._v2_piece_hashes.append(
                merkle_root(self._piece_leaves, target_count=target_blocks, empty_hash=ZERO_HASH)
            )
            self._piece_leaves.clear()

        file_root = self._file_root()
        self._result = TorrentFileHashes(
            v1_piece_hashes=tuple(self._v1_piece_hashes),
            v2_piece_hashes=tuple(self._v2_piece_hashes),
            v2_file_root=file_root,
        )
        return self._result

    def _update_v1(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            take = min(len(remaining), self.piece_length - self._v1_piece_bytes)
            self._v1_hasher.update(remaining[:take])
            self._v1_piece_bytes += take
            remaining = remaining[take:]
            if self._v1_piece_bytes == self.piece_length:
                self._v1_piece_hashes.append(self._v1_hasher.digest())
                self._v1_hasher = hashlib.sha1()
                self._v1_piece_bytes = 0

    def _update_v2(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            take = min(len(remaining), BLOCK_LENGTH - len(self._block_buffer))
            self._block_buffer.extend(remaining[:take])
            remaining = remaining[take:]
            if len(self._block_buffer) == BLOCK_LENGTH:
                self._finish_block(bytes(self._block_buffer))
                self._block_buffer.clear()

    def _finish_block(self, block: bytes) -> None:
        self._piece_leaves.append(hashlib.sha256(block).digest())
        if len(self._piece_leaves) == self._blocks_per_piece:
            self._v2_piece_hashes.append(
                merkle_root(
                    self._piece_leaves,
                    target_count=self._blocks_per_piece,
                    empty_hash=ZERO_HASH,
                )
            )
            self._piece_leaves.clear()

    def _file_root(self) -> bytes | None:
        if not self._v2_piece_hashes:
            return None
        if self.expected_size <= self.piece_length:
            return self._v2_piece_hashes[0]
        zero_piece_root = zero_subtree_root(self._blocks_per_piece)
        return merkle_root(
            self._v2_piece_hashes,
            target_count=next_power_of_two(len(self._v2_piece_hashes)),
            empty_hash=zero_piece_root,
        )


def next_power_of_two(value: int) -> int:
    if value < 1:
        raise ValueError("value must be positive")
    return 1 << (value - 1).bit_length()


def zero_subtree_root(leaves: int) -> bytes:
    if leaves < 1 or leaves & (leaves - 1):
        raise ValueError("leaf count must be a positive power of two")
    root = ZERO_HASH
    remaining = leaves
    while remaining > 1:
        root = hashlib.sha256(root + root).digest()
        remaining //= 2
    return root


def merkle_root(hashes: list[bytes], *, target_count: int, empty_hash: bytes) -> bytes:
    if not hashes:
        raise ValueError("at least one hash is required")
    if target_count < len(hashes) or target_count & (target_count - 1):
        raise ValueError("target count must be a power of two covering all hashes")
    level = list(hashes)
    level.extend(empty_hash for _ in range(target_count - len(level)))
    while len(level) > 1:
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0]
