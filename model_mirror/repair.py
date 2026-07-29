from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .checksums import load_manifest, remove_checksum_paths, update_checksums, write_checksums
from .config import Config, archive_path
from .hub import (
    HubFile,
    HuggingFaceHub,
    HubSnapshot,
    cached_manifest_verifies,
    get_snapshot,
    read_snapshot_plan,
    write_snapshot_plan,
)
from .lock import ModelBusyError, ModelLock
from .payload import validate_payload_parent
from .state import AuditState, read_audit_state, state_from_results, write_audit_state
from .verify import current_manifest_hash, metadata_blob_id, metadata_lfs_sha256, metadata_path, metadata_size, verify_remote


@dataclass(slots=True)
class RepairResult:
    status: str
    path: Path
    paths: list[str]
    upstream_status: str = "unknown"
    resolved_commit: str = ""
    upstream_commit: str = ""


@dataclass(frozen=True, slots=True)
class UpdateChange:
    old: HubFile
    new: HubFile


@dataclass(slots=True)
class UpdatePlan:
    repo_id: str
    repo_type: str
    current_commit: str
    target_commit: str
    added: list[HubFile] = field(default_factory=list)
    changed: list[UpdateChange] = field(default_factory=list)
    removed: list[HubFile] = field(default_factory=list)
    unchanged: list[HubFile] = field(default_factory=list)

    @property
    def current_files(self) -> int:
        return len(self.removed) + len(self.changed) + len(self.unchanged)

    @property
    def target_files(self) -> int:
        return len(self.added) + len(self.changed) + len(self.unchanged)

    @property
    def current_bytes(self) -> int:
        return sum_known_sizes(
            [*self.removed, *(item.old for item in self.changed), *self.unchanged]
        )

    @property
    def target_bytes(self) -> int:
        return sum_known_sizes(
            [*self.added, *(item.new for item in self.changed), *self.unchanged]
        )

    @property
    def candidate_download_bytes(self) -> int:
        return sum_known_sizes([*self.added, *(item.new for item in self.changed)])

    @property
    def removed_bytes(self) -> int:
        return sum_known_sizes(self.removed)


def preview_update(
    config: Config,
    repo_id: str,
    *,
    hub=None,
    repo_type: str | None = None,
    revision: str | None = None,
) -> UpdatePlan:
    selected_type = repo_type or config.repo_type
    selected_revision = revision or config.revision
    selected_hub = hub or HuggingFaceHub(config)
    root = archive_path(config, repo_id, selected_type)
    state = read_audit_state(root)
    if state is None:
        raise ValueError(f"verification state unavailable; run: model-mirror verify {repo_id}")
    if state.offline_only:
        raise ValueError("mirror is offline-only; an upstream update cannot be previewed")
    if (
        state.upstream_status != "changed"
        or not state.resolved_commit
        or not state.upstream_commit
    ):
        raise ValueError(
            f"no changed upstream commit is recorded; run: model-mirror verify {repo_id}"
        )

    current = read_snapshot_plan(root)
    if current is None or current.resolved_commit != state.resolved_commit:
        current = snapshot_for_requested_revision(
            get_snapshot(selected_hub, repo_id, selected_type, state.resolved_commit),
            state.requested_revision or selected_revision,
        )
    target = snapshot_for_requested_revision(
        get_snapshot(selected_hub, repo_id, selected_type, state.upstream_commit),
        state.requested_revision or selected_revision,
    )
    return build_update_plan(current, target)


def build_update_plan(current: HubSnapshot, target: HubSnapshot) -> UpdatePlan:
    current_files = {metadata_path(item): item for item in current.files}
    target_files = {metadata_path(item): item for item in target.files}
    plan = UpdatePlan(
        repo_id=target.repo_id,
        repo_type=target.repo_type,
        current_commit=current.resolved_commit,
        target_commit=target.resolved_commit,
    )
    for rel in sorted(current_files.keys() | target_files.keys()):
        old = current_files.get(rel)
        new = target_files.get(rel)
        if old is None:
            plan.added.append(new)
        elif new is None:
            plan.removed.append(old)
        elif reusable_file_identity(old, new):
            plan.unchanged.append(new)
        else:
            plan.changed.append(UpdateChange(old=old, new=new))
    return plan


def reusable_file_identity(old: HubFile, new: HubFile) -> bool:
    if metadata_size(old) != metadata_size(new):
        return False
    old_lfs = metadata_lfs_sha256(old)
    new_lfs = metadata_lfs_sha256(new)
    if old_lfs is not None or new_lfs is not None:
        return old_lfs is not None and old_lfs == new_lfs
    old_blob = metadata_blob_id(old)
    new_blob = metadata_blob_id(new)
    return old_blob is not None and old_blob == new_blob


def sum_known_sizes(files) -> int:
    return sum(size for item in files if (size := metadata_size(item)) is not None)


def repair(
    config: Config,
    repo_id: str,
    *,
    hub=None,
    repo_type: str | None = None,
    revision: str | None = None,
    update: bool = False,
    force_partial: bool = False,
) -> RepairResult:
    selected_type = repo_type or config.repo_type
    selected_revision = revision or config.revision
    selected_hub = hub or HuggingFaceHub(config)
    root = archive_path(config, repo_id, selected_type)
    initial_state = read_audit_state(root)
    from .torrent_publication import (
        assert_commit_update_allowed,
        begin_maintenance,
        finish_maintenance,
        wait_for_maintenance_detach,
    )

    if (
        update
        and initial_state is not None
        and initial_state.upstream_status == "changed"
        and initial_state.upstream_commit
    ):
        assert_commit_update_allowed(root, initial_state.upstream_commit)

    maintenance = False
    if (
        initial_state is not None
        and not initial_state.clean
        and bool(initial_state.repair_paths)
        and not update
    ):
        with ModelLock(root, "torrent-maintenance", repo_id, selected_type):
            maintenance = begin_maintenance(root) is not None
        if maintenance:
            wait_for_maintenance_detach(root)

    result = None
    try:
        with ModelLock(root, "repair", repo_id, selected_type):
            result = repair_locked(
                config,
                repo_id,
                selected_hub,
                selected_type,
                selected_revision,
                root,
                update=update,
                force_partial=force_partial,
            )
            if maintenance:
                finish_maintenance(
                    root,
                    healthy=result.status in {"complete", "repaired"},
                )
            return result
    except Exception:
        if maintenance:
            try:
                with ModelLock(root, "torrent-maintenance-failed", repo_id, selected_type):
                    finish_maintenance(root, healthy=False)
            except ModelBusyError:
                pass
        raise


def repair_locked(
    config: Config,
    repo_id: str,
    selected_hub,
    selected_type: str,
    selected_revision: str,
    root: Path,
    *,
    update: bool,
    force_partial: bool,
) -> RepairResult:
    state = read_audit_state(root)
    if state is None:
        return RepairResult("verify-required", root, [])
    if state.offline_only:
        return repair_result("offline-only", root, [], state)
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
    pinned_snapshot = read_snapshot_plan(root)
    if (
        state.clean
        and state.resolved_commit
        and pinned_snapshot is not None
        and pinned_snapshot.resolved_commit != state.resolved_commit
    ):
        return reconcile_snapshot_plan(
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
    target_revision = state.resolved_commit
    if not target_revision:
        return verification_incomplete_result(root, paths, state)
    snapshot = snapshot_for_requested_revision(
        get_snapshot(selected_hub, repo_id, selected_type, target_revision),
        requested_revision,
    )
    expected_paths = {metadata_path(item) for item in snapshot.files}
    paths = [rel for rel in paths if rel in expected_paths]
    if (
        config.checksum
        and not force_partial
        and missing_manifest_paths(root, snapshot.files, ignored_paths=set(paths))
    ):
        return verification_incomplete_result(root, paths, state)
    if not paths:
        checksums_available = cached_manifest_verifies(root, snapshot.files)
        final_state = derive_state(
            config,
            repo_id,
            selected_hub,
            selected_type,
            requested_revision,
            root,
            snapshot=snapshot,
            resolved_commit=target_revision,
            upstream_commit=state.upstream_commit,
            cached=checksums_available,
            from_manifest=checksums_available,
            persist=False,
        )
        persist_repair_state(root, snapshot, final_state)
        return repair_result(repair_status(final_state, success="repaired"), root, [], final_state)
    root.mkdir(parents=True, exist_ok=True)
    for rel in paths:
        target = root / rel
        rel = validate_payload_parent(root, target, rel)
        if target.is_symlink() or (target.exists() and target.is_file()):
            target.unlink()

    download_snapshot(selected_hub, snapshot, root, allow_patterns=paths)
    checksums_available = cached_manifest_verifies(root, snapshot.files)
    if config.checksum and not checksums_available:
        update_checksums(root, paths, max_workers=config.checksum_workers)
        checksums_available = True

    final_state = derive_state(
        config,
        repo_id,
        selected_hub,
        selected_type,
        state.requested_revision or requested_revision,
        root,
        snapshot=snapshot,
        resolved_commit=target_revision,
        upstream_commit=state.upstream_commit,
        cached=checksums_available,
        from_manifest=checksums_available,
        persist=False,
    )
    persist_repair_state(root, snapshot, final_state)
    status = repair_status(final_state, success="repaired")
    return repair_result(status, root, paths, final_state)


def verification_incomplete_result(root: Path, paths: list[str], state: AuditState) -> RepairResult:
    return RepairResult(
        "verification-incomplete",
        root,
        paths,
        state.upstream_status,
        state.resolved_commit,
        state.upstream_commit,
    )


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
    current_snapshot = read_snapshot_plan(root)
    snapshot = snapshot_for_requested_revision(
        get_snapshot(selected_hub, repo_id, selected_type, target_revision),
        requested_revision,
    )
    removed_files = []
    if current_snapshot is not None and current_snapshot.resolved_commit == state.resolved_commit:
        removed_files = build_update_plan(current_snapshot, snapshot).removed
    blocking_removed, deferred_removed = partition_blocking_removals(
        removed_files,
        snapshot.files,
    )
    remove_obsolete_snapshot_files(root, blocking_removed)
    remove_checksum_paths(root, [metadata_path(item) for item in blocking_removed])
    download_snapshot(selected_hub, snapshot, root)
    remove_obsolete_snapshot_files(root, deferred_removed)
    remove_checksum_paths(root, [metadata_path(item) for item in deferred_removed])
    checksums_available = cached_manifest_verifies(root, snapshot.files)
    if config.checksum and not checksums_available:
        write_checksums(root, max_workers=config.checksum_workers)
        checksums_available = cached_manifest_verifies(root, snapshot.files)
    final_state = derive_state(
        config,
        repo_id,
        selected_hub,
        selected_type,
        requested_revision,
        root,
        snapshot=snapshot,
        resolved_commit=target_revision,
        upstream_commit=target_revision,
        cached=checksums_available,
        from_manifest=checksums_available,
        persist=False,
    )
    persist_repair_state(root, snapshot, final_state)
    status = repair_status(final_state, success="updated")
    return repair_result(status, root, [], final_state)


def remove_obsolete_snapshot_files(root: Path, files: list[HubFile]) -> None:
    for item in files:
        rel = metadata_path(item)
        target = root / rel
        validate_payload_parent(root, target, rel)
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            if not target.is_file():
                raise ValueError(f"obsolete payload path is not a regular file: {rel}")
            target.unlink()
        prune_empty_payload_parents(target.parent, root)


def partition_blocking_removals(
    removed: list[HubFile],
    target: list[HubFile],
) -> tuple[list[HubFile], list[HubFile]]:
    target_paths = [metadata_path(item) for item in target]
    blocking = []
    deferred = []
    for item in removed:
        rel = metadata_path(item)
        if any(
            candidate.startswith(f"{rel}/") or rel.startswith(f"{candidate}/")
            for candidate in target_paths
        ):
            blocking.append(item)
        else:
            deferred.append(item)
    return blocking, deferred


def prune_empty_payload_parents(path: Path, root: Path) -> None:
    while path != root:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def reconcile_snapshot_plan(
    config: Config,
    repo_id: str,
    selected_hub,
    selected_type: str,
    selected_revision: str,
    root: Path,
    state: AuditState,
) -> RepairResult:
    requested_revision = state.requested_revision or selected_revision
    snapshot = snapshot_for_requested_revision(
        get_snapshot(selected_hub, repo_id, selected_type, state.resolved_commit),
        requested_revision,
    )
    checksums_available = cached_manifest_verifies(root, snapshot.files)
    final_state = derive_state(
        config,
        repo_id,
        selected_hub,
        selected_type,
        requested_revision,
        root,
        snapshot=snapshot,
        resolved_commit=state.resolved_commit,
        upstream_commit=state.upstream_commit,
        cached=checksums_available,
        from_manifest=checksums_available,
        persist=False,
    )
    persist_repair_state(root, snapshot, final_state)
    status = repair_status(final_state, success="repaired")
    return repair_result(status, root, [], final_state)


def snapshot_for_requested_revision(snapshot: HubSnapshot, requested_revision: str) -> HubSnapshot:
    return HubSnapshot(
        repo_id=snapshot.repo_id,
        repo_type=snapshot.repo_type,
        requested_revision=requested_revision,
        resolved_commit=snapshot.resolved_commit,
        files=snapshot.files,
    )


def persist_repair_state(root: Path, snapshot: HubSnapshot, state: AuditState) -> None:
    if state.clean:
        write_snapshot_plan(root, snapshot)
    write_audit_state(root, state)


def repair_status(state: AuditState, *, success: str) -> str:
    if state.clean:
        return success
    if state.status == "incomplete":
        return "verification-incomplete"
    return "incomplete"


def download_snapshot(selected_hub, snapshot: HubSnapshot, root: Path, *, allow_patterns: list[str] | None = None) -> None:
    if hasattr(selected_hub, "download_snapshot"):
        selected_hub.download_snapshot(snapshot, root, allow_patterns=allow_patterns)
        return
    selected_hub.snapshot_download(
        snapshot.repo_id,
        snapshot.repo_type,
        snapshot.resolved_commit,
        root,
        allow_patterns=allow_patterns,
    )


def missing_manifest_paths(root: Path, metadata, *, ignored_paths: set[str]) -> list[str]:
    manifest = load_manifest(root)
    missing = []
    for item in metadata:
        rel = metadata_path(item)
        if rel in ignored_paths:
            continue
        if metadata_lfs_sha256(item) is None and metadata_blob_id(item) is None:
            continue
        path = root / rel
        if not path.exists() or not path.is_file():
            continue
        expected_size = metadata_size(item)
        stat = path.stat()
        if expected_size is not None and stat.st_size != expected_size:
            continue
        if metadata_lfs_sha256(item) is not None:
            hash_key = "sha256"
        else:
            hash_key = "git_blob_sha1"
        if current_manifest_hash(manifest, rel, stat.st_size, stat.st_mtime_ns, hash_key) is None:
            missing.append(rel)
    return missing


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
    cached: bool = False,
    from_manifest: bool = False,
    persist: bool = True,
) -> AuditState:
    if snapshot is None:
        snapshot = get_snapshot(hub, repo_id, repo_type, resolved_commit or requested_revision)
    metadata = snapshot.files
    remote_result = verify_remote(root, metadata, cached=cached, from_manifest=from_manifest)
    state = state_from_results(
        repo_id,
        repo_type,
        requested_revision,
        remote_result,
        resolved_commit=resolved_commit or snapshot.resolved_commit,
        upstream_commit=upstream_commit or snapshot.resolved_commit,
    )
    if persist:
        write_audit_state(root, state)
    return state
