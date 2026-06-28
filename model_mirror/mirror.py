from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .checksums import write_checksums
from .config import Config, archive_path
from .hub import HuggingFaceHub, get_snapshot
from .lock import ModelLock
from .repair import derive_state
from .state import VerificationState, read_verification_state, write_verification_state
from .verify import verify_remote


@dataclass(slots=True)
class MirrorResult:
    status: str
    path: Path
    files: int


def mirror(
    config: Config,
    repo_id: str,
    *,
    hub=None,
    repo_type: str | None = None,
    revision: str | None = None,
    force: bool = False,
    verify_after: bool = True,
) -> MirrorResult:
    selected_type = repo_type or config.repo_type
    selected_revision = revision or config.revision
    selected_hub = hub or HuggingFaceHub(config)
    destination = archive_path(config, repo_id, selected_type)
    with ModelLock(destination, "mirror", repo_id, selected_type):
        existing_state = read_verification_state(destination)
        if existing_state is None:
            write_verification_state(
                destination,
                VerificationState(
                    status="in_progress",
                    repo_id=repo_id,
                    repo_type=selected_type,
                    requested_revision=selected_revision,
                    issues=["mirror started"],
                ),
            )
        return mirror_locked(
            config,
            repo_id,
            selected_hub,
            selected_type,
            selected_revision,
            destination,
            existing_state=existing_state,
            force=force,
            verify_after=verify_after,
        )


def mirror_locked(
    config: Config,
    repo_id: str,
    selected_hub,
    selected_type: str,
    selected_revision: str,
    destination: Path,
    *,
    existing_state: VerificationState | None,
    force: bool,
    verify_after: bool,
) -> MirrorResult:
    snapshot = get_snapshot(selected_hub, repo_id, selected_type, selected_revision)
    metadata = snapshot.files

    if not force and verify_remote(destination, metadata, quick=True).ok:
        if not verify_after:
            return MirrorResult("complete", destination, len(metadata))
        if (
            existing_state is not None
            and existing_state.clean
            and existing_state.resolved_commit == snapshot.resolved_commit
        ):
            return MirrorResult("complete", destination, len(metadata))
        checksums_written = False
        if config.checksum:
            write_checksums(destination, max_workers=config.checksum_workers)
            checksums_written = True
        state = derive_state(
            config,
            repo_id,
            selected_hub,
            selected_type,
            selected_revision,
            destination,
            snapshot=snapshot,
            upstream_commit=snapshot.resolved_commit,
            quick=False,
            from_checksums=checksums_written,
        )
        return MirrorResult("complete" if state.clean else "downloaded-unverified", destination, len(metadata))

    write_verification_state(
        destination,
        VerificationState(
            status="in_progress",
            repo_id=repo_id,
            repo_type=selected_type,
            requested_revision=selected_revision,
            resolved_commit=snapshot.resolved_commit,
            upstream_commit=snapshot.resolved_commit,
            upstream_status="current",
            issues=["mirror in progress"],
        ),
    )
    destination.mkdir(parents=True, exist_ok=True)
    selected_hub.snapshot_download(repo_id, selected_type, snapshot.resolved_commit, destination)
    checksums_written = False
    if config.checksum:
        write_checksums(destination, max_workers=config.checksum_workers)
        checksums_written = True
    if verify_after:
        state = derive_state(
            config,
            repo_id,
            selected_hub,
            selected_type,
            selected_revision,
            destination,
            snapshot=snapshot,
            upstream_commit=snapshot.resolved_commit,
            quick=False,
            from_checksums=checksums_written,
        )
        return MirrorResult("downloaded" if state.clean else "downloaded-unverified", destination, len(metadata))
    write_verification_state(
        destination,
        VerificationState(
            status="dirty",
            repo_id=repo_id,
            repo_type=selected_type,
            requested_revision=selected_revision,
            resolved_commit=snapshot.resolved_commit,
            upstream_commit=snapshot.resolved_commit,
            upstream_status="current",
            issues=["verification skipped"],
        ),
    )
    return MirrorResult("downloaded", destination, len(metadata))
