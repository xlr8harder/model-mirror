from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml


VERIFICATION_FILE = ".verification"


@dataclass(slots=True)
class VerificationState:
    status: str
    repo_id: str
    repo_type: str = "model"
    requested_revision: str = "main"
    resolved_commit: str = ""
    upstream_commit: str = ""
    upstream_status: str = "unknown"
    offline_only: bool = False
    repair_paths: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    checked_at_utc: str = ""

    @property
    def clean(self) -> bool:
        return self.status == "clean"


def audit_state_path(root: Path) -> Path:
    return verification_state_path(root)


def verification_state_path(root: Path) -> Path:
    return root / VERIFICATION_FILE


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_verification_state(root: Path) -> VerificationState | None:
    path = verification_state_path(root)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Verification state must contain a YAML mapping: {path}")
    return VerificationState(
        status=str(data.get("status", "dirty")),
        repo_id=str(data.get("repo_id", "")),
        repo_type=str(data.get("repo_type", "model")),
        requested_revision=str(data.get("requested_revision", data.get("revision", "main"))),
        resolved_commit=str(data.get("resolved_commit", "")),
        upstream_commit=str(data.get("upstream_commit", "")),
        upstream_status=str(data.get("upstream_status", "unknown")),
        offline_only=bool(data.get("offline_only", False)),
        repair_paths=sorted(str(item) for item in data.get("repair_paths", []) or []),
        issues=[str(item) for item in data.get("issues", []) or []],
        checked_at_utc=str(data.get("checked_at_utc", "")),
    )


def write_verification_state(root: Path, state: VerificationState) -> Path:
    path = verification_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": state.status,
        "repo_id": state.repo_id,
        "repo_type": state.repo_type,
        "requested_revision": state.requested_revision,
        "resolved_commit": state.resolved_commit,
        "upstream_commit": state.upstream_commit,
        "upstream_status": state.upstream_status,
        "offline_only": state.offline_only,
        "repair_paths": sorted(set(state.repair_paths)),
        "issues": state.issues,
        "checked_at_utc": state.checked_at_utc or utc_now(),
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def repair_paths_from_results(remote_result, audit_result=None) -> list[str]:
    paths = set()
    paths.update(getattr(remote_result, "missing", []))
    paths.update(getattr(remote_result, "size_mismatches", []))
    paths.update(getattr(remote_result, "hash_mismatches", []))
    if audit_result is not None:
        paths.update(getattr(audit_result, "missing_files", []))
        for failure in getattr(audit_result, "failures", []):
            candidate = str(failure).split(":", 1)[0]
            if candidate.endswith((".json", ".safetensors", ".bin", ".txt", ".model")):
                paths.add(candidate)
    return sorted(paths)


def state_from_results(
    repo_id: str,
    repo_type: str,
    requested_revision: str,
    remote_result,
    audit_result=None,
    *,
    resolved_commit: str = "",
    upstream_commit: str = "",
    offline_only: bool = False,
) -> VerificationState:
    issues = []
    for name in ("missing", "size_mismatches", "hash_mismatches", "cached_hash_missing", "extras"):
        values = getattr(remote_result, name, [])
        issues.extend(f"{name}: {value}" for value in values)
    if audit_result is not None:
        issues.extend(f"missing: {value}" for value in getattr(audit_result, "missing_files", []))
        issues.extend(f"audit: {value}" for value in getattr(audit_result, "failures", []))

    ok = getattr(remote_result, "ok", False) and (audit_result is None or getattr(audit_result, "ok", False))
    repair_paths = repair_paths_from_results(remote_result, audit_result)
    cache_incomplete = bool(getattr(remote_result, "cached_hash_missing", [])) and not repair_paths
    return VerificationState(
        status="clean" if ok else "incomplete" if cache_incomplete else "dirty",
        repo_id=repo_id,
        repo_type=repo_type,
        requested_revision=requested_revision,
        resolved_commit=resolved_commit,
        upstream_commit=upstream_commit,
        upstream_status=upstream_status(resolved_commit, upstream_commit),
        offline_only=offline_only,
        repair_paths=[] if ok else repair_paths,
        issues=[] if ok else issues,
        checked_at_utc=utc_now(),
    )


def upstream_status(resolved_commit: str, upstream_commit: str) -> str:
    if not upstream_commit:
        return "unknown"
    if not resolved_commit:
        return "unknown"
    return "current" if resolved_commit == upstream_commit else "changed"


# Backward-compatible internal aliases while the implementation is being split up.
AuditState = VerificationState
read_audit_state = read_verification_state
write_audit_state = write_verification_state
