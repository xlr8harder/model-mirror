import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import libtorrent as lt
import pytest

import model_mirror.torrent_seed as seed_module
from model_mirror.checksums import write_checksums
from model_mirror.config import Config
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.lock import ModelBusyError
from model_mirror.state import VerificationState, write_verification_state
from model_mirror.torrent import TorrentPublicationError
from model_mirror.torrent_publication import (
    create_publication,
    fence_path,
    load_fenced_publication,
    set_seed_desired,
    update_observed_backend,
)
from model_mirror.torrent_seed import (
    ManagedSeeder,
    discover_publications,
    read_verified_metainfo,
    serve,
)


COMMIT = "a" * 40


def prepared_archive(tmp_path, *, repo_type="model"):
    type_dir = {"model": "models", "dataset": "datasets", "space": "spaces"}[repo_type]
    root = tmp_path / type_dir / "org" / "model"
    root.mkdir(parents=True)
    payload = b"payload"
    (root / "file.bin").write_bytes(payload)
    item = HubFile("file.bin", len(payload), lfs_sha256=hashlib.sha256(payload).hexdigest())
    write_checksums(root)
    write_snapshot_plan(root, HubSnapshot("org/model", repo_type, "main", COMMIT, [item]))
    write_verification_state(
        root,
        VerificationState(
            "clean",
            "org/model",
            repo_type=repo_type,
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
        ),
    )
    return root


class FakeSession:
    def __init__(self, *, add_error=None):
        self.added = []
        self.removed = []
        self.add_error = add_error

    def add_torrent(self, params):
        if self.add_error:
            raise self.add_error
        handle = object()
        self.added.append((params, handle))
        return handle

    def remove_torrent(self, handle):
        self.removed.append(handle)


def test_managed_seeder_reconciles_durable_intent_stop_restart_and_mutation(tmp_path):
    root = prepared_archive(tmp_path)
    publication = create_publication(
        root,
        repo_id="org/model",
        repo_type="model",
        desired_seed=True,
    )
    config = Config(directory=tmp_path)
    session = FakeSession()
    seeder = ManagedSeeder(libtorrent_module=lt, session=session)

    seeder.reconcile(config)
    assert len(session.added) == 1 and publication.record.publication_id in seeder.active
    params = session.added[0][0]
    assert params.have_pieces == [True] * params.ti.num_pieces()
    assert load_fenced_publication(root)[0].observed_backend == "seeding"
    seeder.reconcile(config)
    assert len(session.added) == 1

    set_seed_desired(root, desired=False)
    seeder.reconcile(config)
    assert len(session.removed) == 1 and not seeder.active
    assert load_fenced_publication(root)[0].observed_backend == "stopped"

    set_seed_desired(root, desired=True)
    seeder.reconcile(config)
    assert len(session.added) == 2
    seeder.close()
    record = load_fenced_publication(root)[0]
    assert record.desired_seed and record.observed_backend == "stopped"

    (root / "file.bin").write_bytes(b"changed")
    restarted = ManagedSeeder(libtorrent_module=lt, session=FakeSession())
    restarted.reconcile(config)
    record = load_fenced_publication(root)[0]
    assert record.lifecycle == "unhealthy" and record.observed_backend == "unhealthy"
    assert not restarted.active


def test_seeder_external_missing_error_and_discovery_paths(tmp_path):
    root = prepared_archive(tmp_path / "external")
    create_publication(
        root,
        repo_id="org/model",
        repo_type="model",
        desired_seed=False,
        client_mode="external",
    )
    config = Config(directory=tmp_path / "external")
    session = FakeSession()
    ManagedSeeder(libtorrent_module=lt, session=session).reconcile(config)
    assert not session.added

    dataset = prepared_archive(tmp_path / "types", repo_type="dataset")
    create_publication(dataset, repo_id="org/model", repo_type="dataset", desired_seed=True)
    assert discover_publications(Config(directory=tmp_path / "types"))[0][0] == dataset
    fence_path(dataset).write_text("{")
    assert discover_publications(Config(directory=tmp_path / "types")) == []

    root = prepared_archive(tmp_path / "error")
    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)
    error_seeder = ManagedSeeder(
        libtorrent_module=lt,
        session=FakeSession(add_error=RuntimeError("cannot add")),
    )
    error_seeder.reconcile(Config(directory=tmp_path / "error"))
    assert load_fenced_publication(root)[0].observed_backend == "error"

    update_observed_backend(root, state="seeding")
    set_seed_desired(root, desired=False)
    ManagedSeeder(libtorrent_module=lt, session=FakeSession()).reconcile(
        Config(directory=tmp_path / "error")
    )
    assert load_fenced_publication(root)[0].observed_backend == "stopped"


def test_verified_metainfo_rejects_unsafe_missing_and_changed_artifacts(tmp_path):
    root = prepared_archive(tmp_path)
    result = create_publication(root, repo_id="org/model", repo_type="model")
    assert read_verified_metainfo(root, result.record) == result.torrent_path.read_bytes()

    record = load_fenced_publication(root)[0]
    record.torrent_path = "../escape"
    with pytest.raises(TorrentPublicationError, match="unsafe"):
        read_verified_metainfo(root, record)
    record.torrent_path = "missing.torrent"
    with pytest.raises(TorrentPublicationError, match="unavailable"):
        read_verified_metainfo(root, record)
    record.torrent_path = result.torrent_path.relative_to(root).as_posix()
    result.torrent_path.write_bytes(b"changed")
    with pytest.raises(TorrentPublicationError, match="digest mismatch"):
        read_verified_metainfo(root, record)


def test_seeder_handles_removed_fence_busy_updates_and_close(tmp_path, monkeypatch):
    root = prepared_archive(tmp_path)
    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)
    config = Config(directory=tmp_path)
    session = FakeSession()
    seeder = ManagedSeeder(libtorrent_module=lt, session=session)
    seeder.reconcile(config)
    fence_path(root).unlink()
    seeder.reconcile(config)
    assert session.removed and not seeder.active

    # _set_observed has explicit no-fence and busy paths for daemon races.
    seeder._set_observed(root, "stopped")
    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)

    class BusyLock:
        def __init__(self, root, *args):
            self.root = root

        def __enter__(self):
            raise ModelBusyError(self.root, {"command": "repair"})

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(seed_module, "ModelLock", BusyLock)
    seeder._set_observed(root, "x")
    seeder.reconcile(config)


def test_serve_once_and_signal_driven_loop(tmp_path, monkeypatch):
    root = prepared_archive(tmp_path)
    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)
    config = Config(directory=tmp_path)
    with pytest.raises(ValueError, match="positive"):
        serve(config, poll_seconds=0, once=True, libtorrent_module=lt, session=FakeSession())

    session = FakeSession()
    serve(config, once=True, libtorrent_module=lt, session=session)
    assert session.added and session.removed

    handlers = {}

    def fake_signal(signum, handler):
        previous = handlers.get(signum, object())
        handlers[signum] = handler
        return previous

    def fake_sleep(_seconds):
        handlers[seed_module.signal.SIGTERM](None, None)

    monkeypatch.setattr(seed_module.signal, "signal", fake_signal)
    monkeypatch.setattr(seed_module.time, "sleep", fake_sleep)
    session = FakeSession()
    serve(config, poll_seconds=0.01, libtorrent_module=lt, session=session)
    assert session.added and session.removed


def test_seeder_default_session_and_reconciliation_race_guards(tmp_path, monkeypatch):
    made = {}

    class FakeLT:
        @staticmethod
        def session(settings):
            made["settings"] = settings
            return FakeSession()

    created = ManagedSeeder(libtorrent_module=FakeLT(), listen_interfaces="127.0.0.1:1")
    assert made["settings"] == {"listen_interfaces": "127.0.0.1:1"}
    assert isinstance(created.session, FakeSession)
    ManagedSeeder(libtorrent_module=FakeLT())
    assert made["settings"] == {}

    root = prepared_archive(tmp_path)
    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)
    config = Config(directory=tmp_path)
    session = FakeSession()
    seeder = ManagedSeeder(libtorrent_module=lt, session=session)
    seeder.reconcile(config)
    real_lock = seed_module.ModelLock

    set_seed_desired(root, desired=False)

    class RestoreDesiredLock(real_lock):
        def __enter__(self):
            entered = super().__enter__()
            set_seed_desired(root, desired=True)
            return entered

    monkeypatch.setattr(seed_module, "ModelLock", RestoreDesiredLock)
    seeder.reconcile(config)
    assert seeder.active

    set_seed_desired(root, desired=False)

    class BusyLock:
        def __init__(self, selected_root, *args):
            self.root = selected_root

        def __enter__(self):
            raise ModelBusyError(self.root, {"command": "repair"})

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(seed_module, "ModelLock", BusyLock)
    seeder.reconcile(config)
    assert seeder.active
    seeder.close()
    assert seeder.active  # busy close leaves state for process teardown

    monkeypatch.setattr(seed_module, "ModelLock", real_lock)
    seeder.close()
    assert not seeder.active


def test_seeder_handles_state_disappearing_or_changing_after_discovery(tmp_path, monkeypatch):
    root = prepared_archive(tmp_path)
    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)
    config = Config(directory=tmp_path)
    real_lock = seed_module.ModelLock

    class DeleteFenceLock(real_lock):
        def __enter__(self):
            entered = super().__enter__()
            fence_path(root).unlink(missing_ok=True)
            return entered

    monkeypatch.setattr(seed_module, "ModelLock", DeleteFenceLock)
    session = FakeSession()
    ManagedSeeder(libtorrent_module=lt, session=session).reconcile(config)
    assert not session.added

    create_publication(root, repo_id="org/model", repo_type="model", desired_seed=True)

    class StopBeforeAddLock(real_lock):
        def __enter__(self):
            entered = super().__enter__()
            set_seed_desired(root, desired=False)
            return entered

    monkeypatch.setattr(seed_module, "ModelLock", StopBeforeAddLock)
    ManagedSeeder(libtorrent_module=lt, session=FakeSession()).reconcile(config)

    monkeypatch.setattr(seed_module, "ModelLock", real_lock)
    set_seed_desired(root, desired=True)
    seeder = ManagedSeeder(libtorrent_module=lt, session=FakeSession())
    real_match = seed_module.payload_fingerprints_match
    monkeypatch.setattr(
        seed_module,
        "payload_fingerprints_match",
        lambda *args: (_ for _ in ()).throw(RuntimeError("race failure")),
    )
    seeder.reconcile(config)
    assert load_fenced_publication(root)[0].observed_backend == "error"
    monkeypatch.setattr(seed_module, "payload_fingerprints_match", real_match)

    real_load = seed_module.load_fenced_publication
    monkeypatch.setattr(seed_module, "load_fenced_publication", lambda candidate: None)
    assert discover_publications(config) == []
    monkeypatch.setattr(seed_module, "load_fenced_publication", real_load)

    seeder._set_observed(root, "stopped", lifecycle="published")
    assert load_fenced_publication(root)[0].observed_backend == "stopped"

    # Removal also tolerates a fence disappearing after discovery.
    set_seed_desired(root, desired=True)
    active_session = FakeSession()
    active = ManagedSeeder(libtorrent_module=lt, session=active_session)
    active.reconcile(config)
    set_seed_desired(root, desired=False)

    class DeleteDuringRemovalLock(real_lock):
        def __enter__(self):
            entered = super().__enter__()
            fence_path(root).unlink(missing_ok=True)
            return entered

    monkeypatch.setattr(seed_module, "ModelLock", DeleteDuringRemovalLock)
    active.reconcile(config)
    assert active_session.removed and not active.active
