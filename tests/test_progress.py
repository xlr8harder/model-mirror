from datetime import datetime, timezone

import model_mirror.progress as progress_module
from model_mirror.progress import ProgressRecorder, idle_seconds, progress_path, progress_snapshot


def test_progress_recorder_writes_active_snapshot_and_cleans_up(tmp_path):
    recorder = ProgressRecorder(tmp_path, min_interval_seconds=0, min_bytes=1)
    progress = recorder.track("file.bin", total=10, stage="downloading")
    progress.update(5, stage="downloading")

    snapshot = progress_snapshot(
        tmp_path,
        stall_timeout_seconds=600,
        now=datetime.now(timezone.utc),
    )

    assert snapshot.source == "heartbeat"
    assert len(snapshot.entries) == 1
    entry = snapshot.entries[0]
    assert entry.path == "file.bin"
    assert entry.stage == "downloading"
    assert entry.bytes_done == 5
    assert entry.bytes_total == 10
    assert entry.rate_bytes_per_second is not None
    assert entry.stalled is False

    progress.finish()

    assert not progress_path(tmp_path).exists()
    assert progress_snapshot(tmp_path).entries == []


def test_progress_recorder_throttles_small_updates(tmp_path):
    recorder = ProgressRecorder(tmp_path, min_interval_seconds=999, min_bytes=999)
    progress = recorder.track("file.bin", total=10, stage="downloading")
    progress.update(1, stage="downloading")

    snapshot = progress_snapshot(tmp_path)

    assert snapshot.entries[0].bytes_done == 0


def test_progress_recorder_emits_on_byte_threshold(tmp_path):
    recorder = ProgressRecorder(tmp_path, min_interval_seconds=999, min_bytes=2)
    progress = recorder.track("file.bin", total=10, stage="downloading")
    progress.update(2, stage="downloading")

    snapshot = progress_snapshot(tmp_path)

    assert snapshot.entries[0].bytes_done == 2


def test_progress_recorder_handles_zero_elapsed_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(progress_module.time, "monotonic", lambda: 1.0)
    recorder = ProgressRecorder(tmp_path, min_interval_seconds=0, min_bytes=1)
    progress = recorder.track("file.bin", total=10, stage="downloading")
    progress.update(1, stage="downloading", force=True)

    snapshot = progress_snapshot(tmp_path)

    assert snapshot.entries[0].rate_bytes_per_second is None


def test_progress_snapshot_marks_old_heartbeat_stalled(tmp_path):
    path = progress_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        """{
  "schema": "model-mirror-progress",
  "version": 1,
  "active_files": {
    "file.bin": {
      "path": "file.bin",
      "stage": "downloading",
      "bytes_done": 3,
      "bytes_total": 10,
      "updated_at_utc": "2026-07-06T10:00:00+00:00"
    }
  }
}
""",
        encoding="utf-8",
    )

    snapshot = progress_snapshot(
        tmp_path,
        stall_timeout_seconds=600,
        now=datetime(2026, 7, 6, 10, 11, tzinfo=timezone.utc),
    )

    assert snapshot.any_stalled is True
    assert snapshot.entries[0].idle_seconds == 660


def test_progress_snapshot_ignores_unknown_progress_file(tmp_path):
    path = progress_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": "other", "version": 1}\n', encoding="utf-8")

    assert progress_snapshot(tmp_path).entries == []


def test_progress_snapshot_falls_back_to_incomplete_files(tmp_path):
    partial = tmp_path / "model.safetensors.incomplete"
    partial.write_bytes(b"abc")
    metadata_partial = tmp_path / ".model-mirror" / "ignored.incomplete"
    metadata_partial.parent.mkdir()
    metadata_partial.write_bytes(b"ignored")
    partial_dir = tmp_path / "dir.incomplete"
    partial_dir.mkdir()

    snapshot = progress_snapshot(
        tmp_path,
        stall_timeout_seconds=0,
        now=datetime.now(timezone.utc),
    )

    assert snapshot.source == "partial-file"
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].path == "model.safetensors"
    assert snapshot.entries[0].bytes_done == 3
    assert snapshot.entries[0].stalled is False


def test_idle_seconds_handles_invalid_and_naive_timestamps():
    now = datetime(2026, 7, 6, 10, 1, tzinfo=timezone.utc)

    assert idle_seconds("not-a-time", now) is None
    assert idle_seconds("2026-07-06T10:00:00", now) == 60
