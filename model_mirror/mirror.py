from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .checksums import write_checksums
from .config import Config, archive_path
from .hub import HuggingFaceHub, cached_manifest_verifies, compatible_snapshot_plan, get_snapshot, write_snapshot_plan
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
    stall_timeout_seconds: int | None = None,
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
            stall_timeout_seconds=stall_timeout_seconds,
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
    stall_timeout_seconds: int | None,
) -> MirrorResult:
    snapshot = select_mirror_snapshot(
        selected_hub,
        repo_id,
        selected_type,
        selected_revision,
        destination,
        existing_state=existing_state,
        force=force,
    )
    metadata = snapshot.files

    if not force and verify_remote(destination, metadata, check_hashes=False).ok:
        if not verify_after:
            return MirrorResult("complete", destination, len(metadata))
        if (
            existing_state is not None
            and existing_state.clean
            and existing_state.resolved_commit == snapshot.resolved_commit
        ):
            return MirrorResult("complete", destination, len(metadata))
        checksums_written = cached_manifest_verifies(destination, metadata)
        if config.checksum and not checksums_written:
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
            cached=False,
            from_manifest=checksums_written,
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
    write_snapshot_plan(destination, snapshot)
    download_snapshot(selected_hub, snapshot, destination, stall_timeout_seconds=stall_timeout_seconds)
    checksums_written = cached_manifest_verifies(destination, metadata)
    if config.checksum and not checksums_written:
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
            cached=False,
            from_manifest=checksums_written,
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


def select_mirror_snapshot(
    selected_hub,
    repo_id: str,
    selected_type: str,
    selected_revision: str,
    destination: Path,
    *,
    existing_state: VerificationState | None,
    force: bool,
):
    if not force and existing_state is not None and existing_state.status == "in_progress":
        frozen = compatible_snapshot_plan(
            destination,
            repo_id=repo_id,
            repo_type=selected_type,
            requested_revision=selected_revision,
        )
        if frozen is not None:
            return frozen
    return get_snapshot(selected_hub, repo_id, selected_type, selected_revision)


def download_snapshot(selected_hub, snapshot, destination: Path, *, stall_timeout_seconds: int | None = None) -> None:
    if hasattr(selected_hub, "download_snapshot"):
        selected_hub.download_snapshot(snapshot, destination, stall_timeout_seconds=stall_timeout_seconds)
        return
    selected_hub.snapshot_download(snapshot.repo_id, snapshot.repo_type, snapshot.resolved_commit, destination)
