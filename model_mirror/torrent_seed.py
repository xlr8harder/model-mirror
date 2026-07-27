from __future__ import annotations

import hashlib
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, REPO_TYPE_DIRS
from .lock import ModelBusyError, ModelLock
from .torrent import TorrentPublicationError, load_libtorrent, verified_seed_params
from .torrent_publication import (
    PublicationRecord,
    fence_path,
    load_fenced_publication,
    payload_fingerprints_match,
    update_observed_backend,
)


@dataclass(slots=True)
class ActiveSeed:
    root: Path
    record: PublicationRecord
    handle: object


class ManagedSeeder:
    def __init__(self, *, libtorrent_module=None, session=None, listen_interfaces: str | None = None):
        self.lt = libtorrent_module or load_libtorrent()
        if session is None:
            settings = {}
            if listen_interfaces:
                settings["listen_interfaces"] = listen_interfaces
            session = self.lt.session(settings)
        self.session = session
        self.active: dict[str, ActiveSeed] = {}

    def reconcile(self, config: Config) -> None:
        discovered = {root: record for root, record in discover_publications(config)}
        for publication_id, active in list(self.active.items()):
            record = discovered.get(active.root)
            if (
                record is None
                or record.publication_id != publication_id
                or not record.desired_seed
                or record.client_mode != "managed"
                or record.lifecycle != "published"
            ):
                if record is None:
                    self.session.remove_torrent(active.handle)
                    del self.active[publication_id]
                else:
                    try:
                        with ModelLock(
                            active.root,
                            "torrent-reconcile",
                            record.repo_id,
                            record.repo_type,
                        ):
                            current = load_fenced_publication(active.root)
                            if current is not None and (
                                current[0].desired_seed
                                and current[0].client_mode == "managed"
                                and current[0].lifecycle == "published"
                            ):
                                continue
                            self.session.remove_torrent(active.handle)
                            del self.active[publication_id]
                            if current is not None:
                                update_observed_backend(active.root, state="stopped")
                    except ModelBusyError:
                        continue

        for root, record in discovered.items():
            if (
                not record.desired_seed
                and record.client_mode == "managed"
                and record.observed_backend == "stopping"
            ):
                self._set_observed(root, "stopped")
                continue
            if (
                not record.desired_seed
                or record.client_mode != "managed"
                or record.lifecycle != "published"
                or record.publication_id in self.active
            ):
                continue
            try:
                with ModelLock(root, "torrent-reconcile", record.repo_id, record.repo_type):
                    current = load_fenced_publication(root)
                    if current is None:
                        continue
                    record = current[0]
                    if (
                        not record.desired_seed
                        or record.client_mode != "managed"
                        or record.lifecycle != "published"
                    ):
                        continue
                    matches, detail = payload_fingerprints_match(root, record)
                    if not matches:
                        update_observed_backend(
                            root,
                            state="unhealthy",
                            detail=detail,
                            lifecycle="unhealthy",
                        )
                        continue
                    try:
                        metainfo = read_verified_metainfo(root, record)
                        params = verified_seed_params(metainfo, root, libtorrent_module=self.lt)
                        handle = self.session.add_torrent(params)
                    except Exception as exc:
                        update_observed_backend(root, state="error", detail=str(exc))
                        continue
                    self.active[record.publication_id] = ActiveSeed(root, record, handle)
                    update_observed_backend(root, state="seeding")
            except ModelBusyError:
                continue
            except Exception as exc:
                self._set_observed(root, "error", str(exc))

    def close(self) -> None:
        for publication_id, active in list(self.active.items()):
            try:
                with ModelLock(
                    active.root,
                    "torrent-reconcile",
                    active.record.repo_id,
                    active.record.repo_type,
                ):
                    self.session.remove_torrent(active.handle)
                    update_observed_backend(
                        active.root,
                        state="stopped",
                        detail="managed seeder stopped",
                    )
                    del self.active[publication_id]
            except ModelBusyError:
                continue

    def _set_observed(
        self,
        root: Path,
        state: str,
        detail: str = "",
        *,
        lifecycle: str | None = None,
    ) -> None:
        fenced = load_fenced_publication(root)
        if fenced is None:
            return
        record = fenced[0]
        try:
            with ModelLock(root, "torrent-reconcile", record.repo_id, record.repo_type):
                update_observed_backend(root, state=state, detail=detail, lifecycle=lifecycle)
        except ModelBusyError:
            return


def discover_publications(config: Config) -> list[tuple[Path, PublicationRecord]]:
    result = []
    archive_root = Path(config.directory)
    for type_dir in REPO_TYPE_DIRS.values():
        base = archive_root / type_dir
        if not base.exists():
            continue
        for candidate in sorted(base.glob("*/*")):
            if not fence_path(candidate).exists():
                continue
            try:
                fenced = load_fenced_publication(candidate)
            except (OSError, ValueError):
                continue
            if fenced is not None:
                result.append((candidate, fenced[0]))
    return result


def read_verified_metainfo(root: Path, record: PublicationRecord) -> bytes:
    relative = Path(record.torrent_path)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise TorrentPublicationError("unsafe torrent artifact path in publication record")
    path = root / relative
    try:
        metainfo = path.read_bytes()
    except OSError as exc:
        raise TorrentPublicationError(f"torrent artifact is unavailable: {path}") from exc
    if hashlib.sha256(metainfo).hexdigest() != record.metainfo_sha256:
        raise TorrentPublicationError(f"torrent artifact digest mismatch: {path}")
    return metainfo


def serve(
    config: Config,
    *,
    poll_seconds: float = 2.0,
    once: bool = False,
    libtorrent_module=None,
    session=None,
) -> None:
    if poll_seconds <= 0:
        raise ValueError("poll interval must be positive")
    seeder = ManagedSeeder(libtorrent_module=libtorrent_module, session=session)
    if once:
        seeder.reconcile(config)
        seeder.close()
        return

    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, stop)
    try:
        while not stopped:
            seeder.reconcile(config)
            time.sleep(poll_seconds)
    finally:
        seeder.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
