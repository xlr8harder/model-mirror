import hashlib

import pytest

import model_mirror.hub as hub_module
from model_mirror.checksums import FileHashes, file_hashes
from model_mirror.hub import (
    DownloadIntegrityError,
    HubFile,
    HubSnapshot,
    record_torrent_coverage,
    stream_file_to_path,
    torrent_coverage_recorder,
)
from model_mirror.torrent_hashes import TorrentFileHasher


def test_stream_retry_wrapper_resets_torrent_accumulator(tmp_path, monkeypatch):
    payload = b"x"
    item = HubFile("x", 1, lfs_sha256=hashlib.sha256(payload).hexdigest())
    snapshot = HubSnapshot("org/model", "model", "main", "a" * 40, [item])
    accumulator = TorrentFileHasher(expected_size=1, piece_length=16 * 1024, pad_v1_tail=False)
    accumulator.update(payload)
    resets = []
    real_reset = accumulator.reset

    def reset():
        resets.append(True)
        real_reset()

    monkeypatch.setattr(accumulator, "reset", reset)
    expected = FileHashes(hashlib.sha256(payload).hexdigest(), "0" * 40)
    monkeypatch.setattr(hub_module, "stream_file_to_path_once", lambda *args, **kwargs: expected)

    assert stream_file_to_path(snapshot, item, tmp_path / "x", torrent_accumulator=accumulator) == expected
    assert resets == [True]


def test_coverage_hook_declines_incomplete_snapshots_and_validates_fallback(tmp_path):
    commit = "a" * 40
    assert torrent_coverage_recorder(
        tmp_path,
        HubSnapshot("org/model", "model", "main", commit, []),
    ) is None
    for item in (HubFile("x", None, blob_id="a" * 40), HubFile("x", 1)):
        assert torrent_coverage_recorder(
            tmp_path,
            HubSnapshot("org/model", "model", "main", commit, [item]),
        ) is None

    path = tmp_path / "x"
    path.write_bytes(b"x")
    accumulator = TorrentFileHasher(expected_size=1, piece_length=16 * 1024, pad_v1_tail=False)
    record_torrent_coverage(None, HubFile("x", 1), path, file_hashes(path), accumulator)

    class Recorder:
        def record(self, *args):
            raise AssertionError("mismatched fallback must not be recorded")

    with pytest.raises(DownloadIntegrityError, match="streamed hashes changed"):
        record_torrent_coverage(
            Recorder(),
            HubFile("x", 1),
            path,
            FileHashes("0" * 64, "0" * 40),
            accumulator,
        )
