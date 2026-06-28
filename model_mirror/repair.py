from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import audit_model
from .checksums import CHECKSUMS, update_checksums, write_checksums
from .config import Config, archive_path
from .hub import HuggingFaceHub, HubSnapshot, get_snapshot
from .lock import ModelLock
from .state import AuditState, read_audit_state, state_from_results, write_audit_state
from .verify import verify_remote


@dataclass(slots=True)
class RepairResult:
    status: str
    path: Path
    paths: list[str]
    upstream_status: str = "unknown"
    resolved_commit: str = ""
    upstream_commit: str = ""


def repair(
    config: Config,
    repo_id: str,
    *,
    hub=None,
    repo_type: str | None = None,
    revision: str | None = None,
    update: bool = False,
) -> RepairResult:
    selected_type = repo_type or config.repo_type
    selected_revision = revision or config.revision
    selected_hub = hub or HuggingFaceHub(config)
    root = archive_path(config, repo_id, selected_type)
    with ModelLock(root, "repair", repo_id, selected_type):
        return repair_locked(
            config,
            repo_id,
            selected_hub,
            selected_type,
            selected_revision,
            root,
            update=update,
        )


def repair_locked(
    config: Config,
    repo_id: str,
    selected_hub,
    selected_type: str,
    selected_revision: str,
    root: Path,
    *,
    update: bool,
) -> RepairResult:
    state = read_audit_state(root)
    if state is None:
        return RepairResult("verify-required", root, [])
    if update and state.upstream_status == "changed" and state.upstream_commit:
        return update_to_upstream(
            config,
            repo_id,
            selected_hub,
            selected_type,
            selected_revision,
            root,
            state,
        )
    if state.clean:
        return repair_result("complete", root, [], state)

    paths = sorted(set(state.repair_paths))
    if not paths:
        return repair_result("no-repair-paths", root, [], state)

    requested_revision = state.requested_revision or selected_revision
    target_revision = state.resolved_commit or requested_revision
    root.mkdir(parents=True, exist_ok=True)
    for rel in paths:
        target = root / rel
        if target.exists() and target.is_file():
            target.unlink()

    selected_hub.snapshot_download(repo_id, selected_type, target_revision, root, allow_patterns=paths)
    checksums_available = False
    if config.checksum:
        if (root / CHECKSUMS).exists():
            update_checksums(root, paths, max_workers=config.checksum_workers)
        else:
            write_checksums(root, max_workers=config.checksum_workers)
        checksums_available = True

    final_state = derive_state(
        config,
        repo_id,
        selected_hub,
        selected_type,
        state.requested_revision or requested_revision,
        root,
        resolved_commit=target_revision,
        upstream_commit=state.upstream_commit,
        quick=False,
        from_checksums=checksums_available,
    )
    write_audit_state(root, final_state)
    return repair_result("repaired" if final_state.clean else "incomplete", root, paths, final_state)


def update_to_upstream(
    config: Config,
    repo_id: str,
    selected_hub,
    selected_type: str,
    selected_revision: str,
    root: Path,
    state: AuditState,
) -> RepairResult:
    requested_revision = state.requested_revision or selected_revision
    target_revision = state.upstream_commit
    root.mkdir(parents=True, exist_ok=True)
    selected_hub.snapshot_download(repo_id, selected_type, target_revision, root)
    checksums_available = False
    if config.checksum:
        write_checksums(root, max_workers=config.checksum_workers)
        checksums_available = True
    final_state = derive_state(
        config,
        repo_id,
        selected_hub,
        selected_type,
        requested_revision,
        root,
        resolved_commit=target_revision,
        upstream_commit=target_revision,
        quick=False,
        from_checksums=checksums_available,
    )
    write_audit_state(root, final_state)
    return repair_result("updated" if final_state.clean else "incomplete", root, [], final_state)


def repair_result(
    status: str,
    root: Path,
    paths: list[str],
    state: AuditState,
) -> RepairResult:
    return RepairResult(
        status,
        root,
        paths,
        state.upstream_status,
        state.resolved_commit,
        state.upstream_commit,
    )


def derive_state(
    config: Config,
    repo_id: str,
    hub,
    repo_type: str,
    requested_revision: str,
    root: Path,
    *,
    snapshot: HubSnapshot | None = None,
    resolved_commit: str | None = None,
    upstream_commit: str | None = None,
    quick: bool = True,
    from_checksums: bool = False,
) -> AuditState:
    if snapshot is None:
        snapshot = get_snapshot(hub, repo_id, repo_type, resolved_commit or requested_revision)
    metadata = snapshot.files
    remote_result = verify_remote(root, metadata, quick=quick, from_checksums=from_checksums)
    audit_result = audit_model(root, skip_transformers=True) if repo_type == "model" else None
    state = state_from_results(
        repo_id,
        repo_type,
        requested_revision,
        remote_result,
        audit_result,
        resolved_commit=resolved_commit or snapshot.resolved_commit,
        upstream_commit=upstream_commit or snapshot.resolved_commit,
    )
    write_audit_state(root, state)
    return state
