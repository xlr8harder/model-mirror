import errno

import pytest

import model_mirror.lock as lock_module
from model_mirror.lock import ModelLock, lock_label, read_active_lock


def test_model_lock_exit_without_handle_is_noop(tmp_path):
    lock = ModelLock(tmp_path, "verify", "org/model")

    assert lock.__exit__(None, None, None) is False


def test_read_active_lock_returns_none_for_missing_or_unlocked_lock(tmp_path):
    assert read_active_lock(tmp_path) is None
    assert not (tmp_path / ".verification.lock").exists()

    (tmp_path / ".verification.lock").write_text("", encoding="utf-8")

    assert read_active_lock(tmp_path) is None


def test_lock_label_handles_empty_and_partial_metadata():
    assert lock_label(None) == "lock held"
    assert lock_label({}) == "lock held"
    assert lock_label({"pid": ""}) == "lock held"
    assert lock_label({"command": "mirror"}) == "command=mirror"


def test_model_lock_reraises_unexpected_flock_errors(tmp_path, monkeypatch):
    lock = ModelLock(tmp_path, "mirror", "org/model")

    def raise_unexpected(*args):
        raise OSError(errno.EINVAL, "unexpected")

    monkeypatch.setattr(lock_module.fcntl, "flock", raise_unexpected)

    with pytest.raises(OSError, match="unexpected"):
        lock.__enter__()

    if lock.handle is not None:
        lock.handle.close()


def test_read_active_lock_reraises_unexpected_flock_errors(tmp_path, monkeypatch):
    (tmp_path / ".verification.lock").write_text("", encoding="utf-8")

    def raise_unexpected(*args):
        raise OSError(errno.EINVAL, "unexpected")

    monkeypatch.setattr(lock_module.fcntl, "flock", raise_unexpected)

    with pytest.raises(OSError, match="unexpected"):
        read_active_lock(tmp_path)
