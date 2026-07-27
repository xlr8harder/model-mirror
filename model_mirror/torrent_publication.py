from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .hub import read_snapshot_plan
from .state import read_verification_state, utc_now
from .torrent import (
    PUBLICATION_PROFILE,
    HybridMetainfo,
    TorrentPublicationError,
    build_publication_descriptor,
    create_hybrid_metainfo_from_coverage,
)
from .torrent_coverage import load_coverage, upgrade_coverage


PUBLICATION_SCHEMA = "model-mirror-torrent-publication"
PUBLICATION_VERSION = 1
RECOVERY_SCHEMA = "model-mirror-torrent-recovery"
RECOVERY_VERSION = 1
FENCE_SCHEMA = "model-mirror-publication-fence"
FENCE_VERSION = 1
TORRENT_ROOT = Path(".model-mirror") / "torrent"
PUBLICATIONS_DIR = TORRENT_ROOT / "publications"
FENCE_FILE = TORRENT_ROOT / "fence.json"


@dataclass(slots=True)
class PublicationRecord:
    repo_id: str
    repo_type: str
    resolved_commit: str
    profile: str
    descriptor_sha256: str
    metainfo_sha256: str
    infohash_v1: str
    infohash_v2: str
    magnet_uri: str
    torrent_path: str
    recovery_path: str
    payload_fingerprints: dict[str, dict[str, int]]
    lifecycle: str = "published"
    desired_seed: bool = False
    client_mode: str = "managed"
    observed_backend: str = "stopped"
    observed_detail: str = ""
    maintenance_resume_seed: bool = False
    content_verification: str = "upstream-verified"
    publication_trust: str = "local-verified-publication"
    upstream_provenance: str = "upstream-verified"
    upstream_availability: str = "available"
    created_at_utc: str = ""
    updated_at_utc: str = ""

    @property
    def publication_id(self) -> str:
        return f"huggingface:{self.repo_type}:{self.repo_id}@{self.resolved_commit}"

    @property
    def active(self) -> bool:
        return self.lifecycle != "retired"

    def to_dict(self) -> dict:
        return {
            "schema": PUBLICATION_SCHEMA,
            "version": PUBLICATION_VERSION,
            "publication_id": self.publication_id,
            "repo_id": self.repo_id,
            "repo_type": self.repo_type,
            "resolved_commit": self.resolved_commit,
            "profile": self.profile,
            "descriptor_sha256": self.descriptor_sha256,
            "metainfo_sha256": self.metainfo_sha256,
            "infohash_v1": self.infohash_v1,
            "infohash_v2": self.infohash_v2,
            "magnet_uri": self.magnet_uri,
            "torrent_path": self.torrent_path,
            "recovery_path": self.recovery_path,
            "payload_fingerprints": self.payload_fingerprints,
            "lifecycle": self.lifecycle,
            "desired_seed": self.desired_seed,
            "client_mode": self.client_mode,
            "observed_backend": self.observed_backend,
            "observed_detail": self.observed_detail,
            "maintenance_resume_seed": self.maintenance_resume_seed,
            "content_verification": self.content_verification,
            "publication_trust": self.publication_trust,
            "upstream_provenance": self.upstream_provenance,
            "upstream_availability": self.upstream_availability,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
        }

    @classmethod
    def from_dict(cls, value: object, *, source: Path) -> PublicationRecord:
        if not isinstance(value, dict):
            raise ValueError(f"Torrent publication record must contain an object: {source}")
        if value.get("schema") != PUBLICATION_SCHEMA or value.get("version") != PUBLICATION_VERSION:
            raise ValueError(
                f"Unsupported torrent publication format in {source}: "
                f"schema={value.get('schema')!r} version={value.get('version')!r}"
            )
        try:
            fingerprints = value["payload_fingerprints"]
            if not isinstance(fingerprints, dict):
                raise TypeError
            record = cls(
                repo_id=str(value["repo_id"]),
                repo_type=str(value["repo_type"]),
                resolved_commit=str(value["resolved_commit"]),
                profile=str(value["profile"]),
                descriptor_sha256=require_digest(value["descriptor_sha256"], 64, source),
                metainfo_sha256=require_digest(value["metainfo_sha256"], 64, source),
                infohash_v1=require_digest(value["infohash_v1"], 40, source),
                infohash_v2=require_digest(value["infohash_v2"], 64, source),
                magnet_uri=str(value["magnet_uri"]),
                torrent_path=str(value["torrent_path"]),
                recovery_path=str(value["recovery_path"]),
                payload_fingerprints={
                    str(path): {
                        "size": int(fingerprint["size"]),
                        "mtime_ns": int(fingerprint["mtime_ns"]),
                    }
                    for path, fingerprint in fingerprints.items()
                },
                lifecycle=str(value.get("lifecycle", "published")),
                desired_seed=bool(value.get("desired_seed", False)),
                client_mode=str(value.get("client_mode", "managed")),
                observed_backend=str(value.get("observed_backend", "unknown")),
                observed_detail=str(value.get("observed_detail", "")),
                maintenance_resume_seed=bool(value.get("maintenance_resume_seed", False)),
                content_verification=str(value.get("content_verification", "upstream-verified")),
                publication_trust=str(
                    value.get("publication_trust", "local-verified-publication")
                ),
                upstream_provenance=str(value.get("upstream_provenance", "upstream-verified")),
                upstream_availability=str(value.get("upstream_availability", "available")),
                created_at_utc=str(value.get("created_at_utc", "")),
                updated_at_utc=str(value.get("updated_at_utc", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed torrent publication record: {source}") from exc
        if value.get("publication_id") not in {None, record.publication_id}:
            raise ValueError(f"Torrent publication identity mismatch: {source}")
        if record.lifecycle not in {"published", "maintenance", "unhealthy", "retired"}:
            raise ValueError(f"Malformed torrent publication lifecycle in {source}")
        if record.client_mode not in {"managed", "external"}:
            raise ValueError(f"Malformed torrent publication client mode in {source}")
        return record


@dataclass(frozen=True, slots=True)
class PublicationResult:
    record: PublicationRecord
    record_path: Path
    torrent_path: Path
    recovery_path: Path
    created: bool
    coverage_hashed_files: int
    coverage_hashed_bytes: int


def publication_dir(root: Path, commit: str, profile: str = PUBLICATION_PROFILE) -> Path:
    return root / PUBLICATIONS_DIR / f"{profile}--{commit}"


def publication_record_path(root: Path, commit: str, profile: str = PUBLICATION_PROFILE) -> Path:
    return publication_dir(root, commit, profile) / "publication.json"


def fence_path(root: Path) -> Path:
    return root / FENCE_FILE


def load_publication(path: Path) -> PublicationRecord | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed torrent publication record: {path}") from exc
    return PublicationRecord.from_dict(value, source=path)


def load_fenced_publication(root: Path) -> tuple[PublicationRecord, Path] | None:
    path = fence_path(root)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Malformed publication fence: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != FENCE_SCHEMA
        or value.get("version") != FENCE_VERSION
    ):
        raise ValueError(f"Unsupported publication fence format: {path}")
    relative = Path(str(value.get("publication_record", "")))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe publication record path in fence: {path}")
    record_path = root / relative
    record = load_publication(record_path)
    if record is None or not record.active:
        raise ValueError(f"Publication fence points to a missing or retired record: {path}")
    if value.get("publication_id") != record.publication_id:
        raise ValueError(f"Publication fence identity mismatch: {path}")
    return record, record_path


def create_publication(
    root: Path,
    *,
    repo_id: str,
    repo_type: str,
    desired_seed: bool | None = None,
    client_mode: str | None = None,
) -> PublicationResult:
    if client_mode not in {None, "managed", "external"}:
        raise ValueError(f"unsupported torrent client mode: {client_mode}")
    imported = reusable_imported_publication(
        root,
        repo_id=repo_id,
        repo_type=repo_type,
        desired_seed=desired_seed,
        client_mode=client_mode,
    )
    if imported is not None:
        return imported
    descriptor = build_publication_descriptor(root, repo_id=repo_id, repo_type=repo_type)
    existing_fence = load_fenced_publication(root)
    if existing_fence is not None and existing_fence[0].resolved_commit != descriptor.resolved_commit:
        raise TorrentPublicationError(
            f"archive is fenced by publication {existing_fence[0].publication_id}; "
            "retire it before publishing another commit"
        )
    snapshot = read_snapshot_plan(root)
    if snapshot is None:  # pragma: no cover - descriptor already enforces this
        raise TorrentPublicationError("pinned snapshot is missing")
    coverage_result = upgrade_coverage(root, snapshot)
    coverage = load_coverage(coverage_result.path)
    if coverage is None or not coverage_result.complete:  # pragma: no cover - defensive
        raise TorrentPublicationError("torrent hash coverage is incomplete after upgrade")
    artifact = create_hybrid_metainfo_from_coverage(root, descriptor, coverage)

    target_dir = publication_dir(root, descriptor.resolved_commit, descriptor.profile)
    torrent_path = target_dir / f"{root.name}@{descriptor.resolved_commit}.torrent"
    recovery_path = target_dir / "recovery.json"
    record_path = target_dir / "publication.json"
    existing = load_publication(record_path)
    if existing is not None and (
        existing.metainfo_sha256 != artifact.metainfo_sha256
        or existing.descriptor_sha256 != artifact.descriptor_sha256
        or existing.infohash_v1 != artifact.infohash_v1
        or existing.infohash_v2 != artifact.infohash_v2
    ):
        raise TorrentPublicationError(
            f"existing publication conflicts with deterministic output: {record_path}"
        )
    if torrent_path.exists() and hashlib.sha256(torrent_path.read_bytes()).hexdigest() != artifact.metainfo_sha256:
        raise TorrentPublicationError(f"existing torrent artifact is corrupt or conflicting: {torrent_path}")

    now = utc_now()
    effective_mode = client_mode or (existing.client_mode if existing is not None else "managed")
    effective_desired = (
        desired_seed
        if desired_seed is not None
        else (existing.desired_seed if existing is not None else False)
    )
    record = PublicationRecord(
        repo_id=descriptor.repo_id,
        repo_type=descriptor.repo_type,
        resolved_commit=descriptor.resolved_commit,
        profile=descriptor.profile,
        descriptor_sha256=artifact.descriptor_sha256,
        metainfo_sha256=artifact.metainfo_sha256,
        infohash_v1=artifact.infohash_v1,
        infohash_v2=artifact.infohash_v2,
        magnet_uri=artifact.magnet_uri,
        torrent_path=torrent_path.relative_to(root).as_posix(),
        recovery_path=recovery_path.relative_to(root).as_posix(),
        payload_fingerprints={
            item.path: {
                "size": coverage.files[item.path].size,
                "mtime_ns": coverage.files[item.path].mtime_ns,
            }
            for item in descriptor.files
        },
        lifecycle="published",
        desired_seed=effective_desired,
        client_mode=effective_mode,
        observed_backend=(
            existing.observed_backend
            if existing is not None and existing.client_mode == effective_mode
            else ("external" if effective_mode == "external" else "pending")
        ),
        observed_detail=existing.observed_detail if existing is not None else "",
        maintenance_resume_seed=False,
        created_at_utc=existing.created_at_utc if existing is not None else now,
        updated_at_utc=now,
    )
    write_bytes_atomic(torrent_path, artifact.metainfo)
    write_json_atomic(recovery_path, recovery_value(record))
    write_json_atomic(record_path, record.to_dict())
    write_json_atomic(
        fence_path(root),
        {
            "schema": FENCE_SCHEMA,
            "version": FENCE_VERSION,
            "publication_id": record.publication_id,
            "publication_record": record_path.relative_to(root).as_posix(),
        },
    )
    return PublicationResult(
        record=record,
        record_path=record_path,
        torrent_path=torrent_path,
        recovery_path=recovery_path,
        created=existing is None or existing.lifecycle == "retired",
        coverage_hashed_files=coverage_result.hashed_files,
        coverage_hashed_bytes=coverage_result.hashed_bytes,
    )


def reusable_imported_publication(
    root: Path,
    *,
    repo_id: str,
    repo_type: str,
    desired_seed: bool | None,
    client_mode: str | None,
) -> PublicationResult | None:
    state = read_verification_state(root)
    if (
        state is None
        or not state.resolved_commit
        or state.repo_id != repo_id
        or state.repo_type != repo_type
    ):
        return None
    path = publication_record_path(root, state.resolved_commit)
    record = load_publication(path)
    if record is None or record.content_verification != "torrent-verified":
        return None
    matches, detail = payload_fingerprints_match(root, record)
    if not matches:
        raise TorrentPublicationError(f"cannot reuse imported publication: {detail}")
    torrent_path = root / record.torrent_path
    try:
        metainfo = torrent_path.read_bytes()
    except OSError as exc:
        raise TorrentPublicationError(f"imported torrent artifact is unavailable: {torrent_path}") from exc
    if hashlib.sha256(metainfo).hexdigest() != record.metainfo_sha256:
        raise TorrentPublicationError(f"imported torrent artifact digest mismatch: {torrent_path}")

    was_retired = record.lifecycle == "retired"
    record.lifecycle = "published"
    if client_mode is not None:
        record.client_mode = client_mode
    if desired_seed is not None:
        record.desired_seed = desired_seed
    record.observed_backend = (
        "external"
        if record.client_mode == "external"
        else "pending" if record.desired_seed else "stopped"
    )
    record.observed_detail = ""
    record.updated_at_utc = utc_now()
    recovery_path = root / record.recovery_path
    write_json_atomic(recovery_path, recovery_value(record))
    write_json_atomic(path, record.to_dict())
    write_json_atomic(
        fence_path(root),
        {
            "schema": FENCE_SCHEMA,
            "version": FENCE_VERSION,
            "publication_id": record.publication_id,
            "publication_record": path.relative_to(root).as_posix(),
        },
    )
    return PublicationResult(
        record=record,
        record_path=path,
        torrent_path=torrent_path,
        recovery_path=recovery_path,
        created=was_retired,
        coverage_hashed_files=0,
        coverage_hashed_bytes=0,
    )


def set_seed_desired(
    root: Path,
    *,
    desired: bool,
    client_mode: str | None = None,
) -> PublicationRecord:
    fenced = load_fenced_publication(root)
    if fenced is None:
        raise TorrentPublicationError("archive has no active torrent publication")
    record, path = fenced
    if client_mode is not None:
        if client_mode not in {"managed", "external"}:
            raise ValueError(f"unsupported torrent client mode: {client_mode}")
        record.client_mode = client_mode
    record.desired_seed = desired
    if record.client_mode == "external":
        record.observed_backend = "external" if desired else "stopped"
    elif desired:
        record.observed_backend = "pending"
    else:
        record.observed_backend = (
            "stopping"
            if record.observed_backend in {"seeding", "stopping"}
            else "stopped"
        )
    record.observed_detail = ""
    record.maintenance_resume_seed = False
    record.updated_at_utc = utc_now()
    write_json_atomic(path, record.to_dict())
    return record


def retire_publication(root: Path) -> PublicationRecord:
    fenced = load_fenced_publication(root)
    if fenced is None:
        raise TorrentPublicationError("archive has no active torrent publication")
    record, path = fenced
    if record.observed_backend in {"seeding", "stopping"}:
        raise TorrentPublicationError(
            f"cannot retire while backend state is {record.observed_backend}; "
            "stop the seed and wait for backend detachment"
        )
    record.lifecycle = "retired"
    record.desired_seed = False
    record.observed_backend = "stopped"
    record.observed_detail = ""
    record.maintenance_resume_seed = False
    record.updated_at_utc = utc_now()
    write_json_atomic(path, record.to_dict())
    fence_path(root).unlink()
    return record


def update_observed_backend(
    root: Path,
    *,
    state: str,
    detail: str = "",
    lifecycle: str | None = None,
) -> PublicationRecord:
    fenced = load_fenced_publication(root)
    if fenced is None:
        raise TorrentPublicationError("archive has no active torrent publication")
    record, path = fenced
    record.observed_backend = state
    record.observed_detail = detail
    if lifecycle is not None:
        record.lifecycle = lifecycle
    record.updated_at_utc = utc_now()
    write_json_atomic(path, record.to_dict())
    return record


def begin_maintenance(root: Path) -> PublicationRecord | None:
    fenced = load_fenced_publication(root)
    if fenced is None:
        return None
    record, path = fenced
    if record.client_mode == "external" and record.observed_backend != "stopped":
        raise TorrentPublicationError(
            "published payload is assigned to an external torrent client; "
            "stop it, then run model-mirror torrent stop before repair"
        )
    if record.lifecycle != "maintenance":
        record.maintenance_resume_seed = record.desired_seed
    record.desired_seed = False
    record.lifecycle = "maintenance"
    if record.observed_backend == "seeding":
        record.observed_backend = "stopping"
    record.observed_detail = ""
    record.updated_at_utc = utc_now()
    write_json_atomic(path, record.to_dict())
    return record


def wait_for_maintenance_detach(root: Path, *, timeout_seconds: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout_seconds
    while True:
        fenced = load_fenced_publication(root)
        if fenced is None:
            return
        record = fenced[0]
        if record.observed_backend not in {"seeding", "stopping"}:
            return
        if time.monotonic() >= deadline:
            raise TorrentPublicationError(
                f"managed seeder did not detach {record.publication_id}; "
                "ensure model-mirror torrent serve is running or stop the backend before repair"
            )
        time.sleep(0.1)


def finish_maintenance(root: Path, *, healthy: bool) -> PublicationRecord | None:
    fenced = load_fenced_publication(root)
    if fenced is None:
        return None
    record, path = fenced
    resume = record.maintenance_resume_seed
    record.maintenance_resume_seed = False
    record.lifecycle = "published" if healthy else "unhealthy"
    record.desired_seed = resume if healthy else False
    record.observed_backend = "pending" if record.desired_seed else "stopped"
    if not healthy:
        record.observed_detail = "same-commit maintenance did not finish cleanly"
    else:
        record.observed_detail = ""
    record.updated_at_utc = utc_now()
    write_json_atomic(path, record.to_dict())
    return record


def assert_commit_update_allowed(root: Path, target_commit: str) -> None:
    fenced = load_fenced_publication(root)
    if fenced is None or fenced[0].resolved_commit == target_commit:
        return
    record = fenced[0]
    raise TorrentPublicationError(
        f"update blocked by active publication {record.publication_id}; "
        f"run model-mirror torrent retire {record.repo_id} before updating"
    )


def payload_fingerprints_match(root: Path, record: PublicationRecord) -> tuple[bool, str]:
    for rel, expected in record.payload_fingerprints.items():
        path = root / rel
        if not path.exists() or not path.is_file() or path.is_symlink():
            return False, f"missing or unsafe payload file: {rel}"
        stat = path.stat()
        if stat.st_size != expected["size"] or stat.st_mtime_ns != expected["mtime_ns"]:
            return False, f"payload fingerprint changed: {rel}"
    return True, ""


def recovery_value(record: PublicationRecord) -> dict:
    return {
        "schema": RECOVERY_SCHEMA,
        "version": RECOVERY_VERSION,
        "publication_id": record.publication_id,
        "profile": record.profile,
        "descriptor_sha256": record.descriptor_sha256,
        "metainfo_sha256": record.metainfo_sha256,
        "infohash_v1": record.infohash_v1,
        "infohash_v2": record.infohash_v2,
        "magnet_uri": record.magnet_uri,
    }


def write_json_atomic(path: Path, value: dict) -> None:
    write_bytes_atomic(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


def write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def require_digest(value: object, length: int, source: Path) -> str:
    text = str(value).lower()
    if len(text) != length or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"Malformed digest in torrent publication record: {source}")
    return text
