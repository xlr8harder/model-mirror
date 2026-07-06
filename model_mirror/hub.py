from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

from .checksums import (
    FileHashes,
    HashingWriter,
    checksum_row_from_hashes,
    file_hashes,
    hash_file_prefix,
    load_manifest,
    record_is_current,
    write_manifest,
)
from .config import Config, TOKEN_SETUP_HINT, apply_hf_environment, hf_token_available
from .progress import DEFAULT_STALL_RETRIES, ProgressRecorder
from .verify import metadata_blob_id, metadata_lfs_sha256, metadata_path


@dataclass(slots=True)
class HubFile:
    path: str
    size: int | None
    lfs_sha256: str | None = None
    blob_id: str | None = None


@dataclass(slots=True)
class HubSnapshot:
    repo_id: str
    repo_type: str
    requested_revision: str
    resolved_commit: str
    files: list[HubFile]


class DownloadIntegrityError(RuntimeError):
    pass


class StallTimeoutError(TimeoutError):
    pass


SNAPSHOT_PLAN_DIR = ".model-mirror"
SNAPSHOT_PLAN_FILE = "snapshot.json"
SNAPSHOT_PLAN_SCHEMA = "model-mirror-snapshot"
SNAPSHOT_PLAN_VERSION = 1


class HuggingFaceHub:
    def __init__(self, config: Config):
        self.config = config
        self._warned_missing_token = False

    def files(self, repo_id: str, repo_type: str, revision: str) -> list[HubFile]:
        return self.snapshot(repo_id, repo_type, revision).files

    def snapshot(self, repo_id: str, repo_type: str, revision: str) -> HubSnapshot:
        with patch.dict(os.environ, self._environment(), clear=False):
            from huggingface_hub import HfApi

            api = HfApi()
            info = api.repo_info(repo_id, repo_type=repo_type, revision=revision, files_metadata=True)
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit=getattr(info, "sha", revision),
            files=[self._file_from_sibling(sibling) for sibling in getattr(info, "siblings", []) or []],
        )

    def snapshot_download(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        local_dir: Path,
        allow_patterns: list[str] | None = None,
        stall_timeout_seconds: int | None = None,
    ) -> Path:
        local_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot(repo_id, repo_type, revision)
        return self.download_snapshot(
            snapshot,
            local_dir,
            allow_patterns=allow_patterns,
            stall_timeout_seconds=stall_timeout_seconds,
        )

    def download_snapshot(
        self,
        snapshot: HubSnapshot,
        local_dir: Path,
        allow_patterns: list[str] | None = None,
        stall_timeout_seconds: int | None = None,
    ) -> Path:
        local_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = download_staging_dir(
            self.config,
            snapshot.repo_id,
            snapshot.repo_type,
            snapshot.resolved_commit,
            allow_patterns,
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        prune_incomplete_downloads(staging_dir)
        env = download_environment(self._environment(), staging_dir)
        with patch.dict(os.environ, env, clear=False):
            stream_snapshot(
                snapshot,
                local_dir,
                staging_dir,
                allow_patterns=allow_patterns,
                download_workers=self.config.download_workers,
                stall_timeout_seconds=(
                    self.config.stall_timeout_seconds if stall_timeout_seconds is None else stall_timeout_seconds
                ),
                stall_retries=self.config.stall_retries,
            )
        shutil.rmtree(staging_dir)
        return local_dir

    def _environment(self) -> dict[str, str]:
        env = apply_hf_environment(self.config)
        if not hf_token_available(env) and not self._warned_missing_token:
            print(
                "warning: no Hugging Face token found; private or gated repositories may fail "
                "and unauthenticated access can affect throughput. Configure a token file with: "
                f"{TOKEN_SETUP_HINT} (or export HF_TOKEN).",
                file=sys.stderr,
            )
            self._warned_missing_token = True
        return env

    @staticmethod
    def _file_from_sibling(sibling) -> HubFile:
        lfs = getattr(sibling, "lfs", None)
        lfs_sha256 = getattr(lfs, "sha256", None) if lfs is not None else None
        return HubFile(
            path=getattr(sibling, "rfilename"),
            size=getattr(sibling, "size", None),
            lfs_sha256=lfs_sha256,
            blob_id=getattr(sibling, "blob_id", None),
        )


def get_snapshot(hub, repo_id: str, repo_type: str, revision: str) -> HubSnapshot:
    if hasattr(hub, "snapshot"):
        return hub.snapshot(repo_id, repo_type, revision)
    files = hub.files(repo_id, repo_type, revision)
    return HubSnapshot(
        repo_id=repo_id,
        repo_type=repo_type,
        requested_revision=revision,
        resolved_commit=revision,
        files=files,
    )


def snapshot_plan_path(root: Path) -> Path:
    return root / SNAPSHOT_PLAN_DIR / SNAPSHOT_PLAN_FILE


def write_snapshot_plan(root: Path, snapshot: HubSnapshot) -> Path:
    path = snapshot_plan_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SNAPSHOT_PLAN_SCHEMA,
        "version": SNAPSHOT_PLAN_VERSION,
        "repo_id": snapshot.repo_id,
        "repo_type": snapshot.repo_type,
        "requested_revision": snapshot.requested_revision,
        "resolved_commit": snapshot.resolved_commit,
        "files": [
            {
                "path": item.path,
                "size": item.size,
                "lfs_sha256": getattr(item, "lfs_sha256", None),
                "blob_id": getattr(item, "blob_id", None),
            }
            for item in snapshot.files
        ],
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def read_snapshot_plan(root: Path) -> HubSnapshot | None:
    path = snapshot_plan_path(root)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SNAPSHOT_PLAN_SCHEMA:
        raise ValueError(f"Unsupported snapshot plan schema in {path}: {data.get('schema')}")
    if data.get("version") != SNAPSHOT_PLAN_VERSION:
        raise ValueError(f"Unsupported snapshot plan version in {path}: {data.get('version')}")
    return HubSnapshot(
        repo_id=str(data["repo_id"]),
        repo_type=str(data["repo_type"]),
        requested_revision=str(data["requested_revision"]),
        resolved_commit=str(data["resolved_commit"]),
        files=[
            HubFile(
                path=str(item["path"]),
                size=item.get("size"),
                lfs_sha256=item.get("lfs_sha256"),
                blob_id=item.get("blob_id"),
            )
            for item in data.get("files", [])
        ],
    )


def compatible_snapshot_plan(
    root: Path,
    *,
    repo_id: str,
    repo_type: str,
    requested_revision: str,
) -> HubSnapshot | None:
    snapshot = read_snapshot_plan(root)
    if snapshot is None:
        return None
    if snapshot.repo_id != repo_id:
        return None
    if snapshot.repo_type != repo_type:
        return None
    if snapshot.requested_revision != requested_revision:
        return None
    return snapshot


def prune_incomplete_downloads(local_dir: Path) -> int:
    download_dir = local_dir / ".cache" / "huggingface" / "download"
    if not download_dir.exists():
        return 0
    removed = 0
    for path in sorted(download_dir.rglob("*.incomplete")):
        if not path.is_file():
            continue
        path.unlink()
        removed += 1
    return removed


def stream_snapshot(
    snapshot: HubSnapshot,
    local_dir: Path,
    staging_dir: Path,
    *,
    allow_patterns: list[str] | None,
    download_workers: int = 1,
    stall_timeout_seconds: int = 600,
    stall_retries: int = DEFAULT_STALL_RETRIES,
) -> None:
    manifest = load_manifest(local_dir)
    selected_files = filtered_snapshot_files(snapshot.files, allow_patterns)
    progress_recorder = ProgressRecorder(local_dir)
    work = []
    for item in selected_files:
        destination = local_dir / item.path
        if verified_manifest_record(local_dir, destination, item, manifest):
            continue
        work.append(item)

    if not work:
        return

    workers = max(1, download_workers)
    if workers == 1:
        for item in work:
            row = stream_snapshot_file(
                snapshot,
                local_dir,
                staging_dir,
                item,
                progress_recorder=progress_recorder,
                stall_timeout_seconds=stall_timeout_seconds,
                stall_retries=stall_retries,
            )
            manifest[row["path"]] = row
            write_manifest(local_dir, manifest)
        return

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                stream_snapshot_file,
                snapshot,
                local_dir,
                staging_dir,
                item,
                progress_recorder=progress_recorder,
                stall_timeout_seconds=stall_timeout_seconds,
                stall_retries=stall_retries,
            )
            for item in work
        ]
        errors = []
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                errors.append(exc)
                continue
            manifest[row["path"]] = row
            write_manifest(local_dir, manifest)
        if errors:
            raise errors[0]


def stream_snapshot_file(
    snapshot: HubSnapshot,
    local_dir: Path,
    staging_dir: Path,
    item: HubFile,
    *,
    progress_recorder: ProgressRecorder,
    stall_timeout_seconds: int,
    stall_retries: int,
) -> dict:
    destination = local_dir / item.path
    progress = progress_recorder.track(item.path, total=item.size, stage="starting")
    completed = False
    try:
        if destination.exists() and destination.is_file():
            row = hash_existing_file(local_dir, destination, item, progress=progress)
            if row is not None:
                completed = True
                return row
            destination.unlink()

        row = promote_legacy_staged_file(local_dir, staging_dir, item, progress=progress)
        if row is not None:
            completed = True
            return row

        hashes = stream_file_to_path(
            snapshot,
            item,
            destination,
            progress=progress,
            stall_timeout_seconds=stall_timeout_seconds,
            stall_retries=stall_retries,
        )
        completed = True
        return checksum_row_from_hashes(local_dir, destination, hashes)
    finally:
        if completed:
            progress.finish()


def filtered_snapshot_files(files: list[HubFile], allow_patterns: list[str] | None) -> list[HubFile]:
    if allow_patterns is None:
        return list(files)
    patterns = list(allow_patterns)
    return [item for item in files if any(fnmatch.fnmatchcase(item.path, pattern) for pattern in patterns)]


def verified_manifest_record(
    root: Path,
    path: Path,
    item: HubFile,
    manifest: dict[str, dict],
) -> bool:
    if not path.exists() or not path.is_file():
        return False
    expected_size = require_expected_size(item)
    stat = path.stat()
    if stat.st_size != expected_size:
        return False
    rel = path.relative_to(root).as_posix()
    row = manifest.get(rel)
    if not record_is_current(row, stat.st_size, stat.st_mtime_ns):
        return False
    return manifest_hash_matches(item, row)


def manifest_hash_matches(item: HubFile, row: dict | None) -> bool:
    if row is None:
        return False
    if item.lfs_sha256 is not None:
        return row.get("sha256") == item.lfs_sha256
    if item.blob_id is not None:
        return row.get("git_blob_sha1") == item.blob_id
    return False


def hash_existing_file(
    root: Path,
    path: Path,
    item: HubFile,
    *,
    progress,
) -> dict | None:
    progress.update(0, stage="hashing-final", force=True)
    hashes = file_hashes(path, on_progress=lambda done: progress.update(done, stage="hashing-final"))
    try:
        ensure_hashes_match(item, path, hashes)
    except DownloadIntegrityError:
        return None
    return checksum_row_from_hashes(root, path, hashes)


def promote_legacy_staged_file(
    root: Path,
    staging_dir: Path,
    item: HubFile,
    *,
    progress,
) -> dict | None:
    staged_path = staging_dir / item.path
    if not staged_path.exists() or not staged_path.is_file():
        return None
    progress.update(0, stage="hashing-staged", force=True)
    hashes = file_hashes(staged_path, on_progress=lambda done: progress.update(done, stage="hashing-staged"))
    try:
        ensure_hashes_match(item, staged_path, hashes)
    except DownloadIntegrityError:
        staged_path.unlink()
        return None
    destination = root / item.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    progress.update(staged_path.stat().st_size, stage="promoting-staged", force=True)
    staged_path.replace(destination)
    return checksum_row_from_hashes(root, destination, hashes)


def stream_file_to_path(
    snapshot: HubSnapshot,
    item: HubFile,
    destination: Path,
    *,
    progress=None,
    stall_timeout_seconds: int = 600,
    stall_retries: int = DEFAULT_STALL_RETRIES,
) -> FileHashes:
    integrity_retry_used = False
    stall_retry_count = 0
    allow_resume = True
    while True:
        try:
            return stream_file_to_path_once(
                snapshot,
                item,
                destination,
                allow_resume=allow_resume,
                progress=progress,
                stall_timeout_seconds=stall_timeout_seconds,
            )
        except StallTimeoutError:
            if stall_retry_count >= stall_retries:
                raise
            stall_retry_count += 1
            allow_resume = True
            partial = incomplete_path_for(destination)
            current_size = partial.stat().st_size if partial.exists() else 0
            if progress is not None:
                progress.update(current_size, stage=f"retrying-stall-{stall_retry_count}", force=True)
            continue
        except DownloadIntegrityError:
            if integrity_retry_used:
                raise
            integrity_retry_used = True
            allow_resume = False
            incomplete_path_for(destination).unlink(missing_ok=True)
            destination.unlink(missing_ok=True)


def stream_file_to_path_once(
    snapshot: HubSnapshot,
    item: HubFile,
    destination: Path,
    *,
    allow_resume: bool,
    progress=None,
    stall_timeout_seconds: int = 600,
) -> FileHashes:
    expected_size = require_expected_size(item)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = incomplete_path_for(destination)
    if not allow_resume:
        tmp_path.unlink(missing_ok=True)
    resume_size = resumable_prefix_size(tmp_path, expected_size) if allow_resume else 0
    hash_state = (
        hash_file_prefix(
            tmp_path,
            total_size=expected_size,
            prefix_size=resume_size,
            on_progress=(
                (lambda done: progress.update(done, stage="hashing-partial")) if progress is not None else None
            ),
        )
        if resume_size
        else None
    )
    if resume_size == expected_size:
        if progress is not None:
            progress.update(resume_size, stage="verifying-partial", force=True)
        hashes = hash_state.hashes if hash_state is not None else file_hashes(tmp_path)
        ensure_hashes_match(item, tmp_path, hashes)
        tmp_path.replace(destination)
        return hashes
    try:
        metadata, headers, url_to_download = hf_transport_metadata(snapshot, item)
        if metadata.size is not None and metadata.size != expected_size:
            raise DownloadIntegrityError(
                f"size metadata mismatch for {item.path}: repo_info={expected_size} head={metadata.size}"
            )
        mode = "r+b" if tmp_path.exists() else "wb"
        with tmp_path.open(mode) as raw:
            raw.seek(resume_size)
            if progress is not None:
                progress.update(resume_size, stage="downloading", force=True)
            writer = HashingWriter(
                raw,
                expected_size=expected_size,
                hash_state=hash_state,
                on_progress=(
                    (lambda done: progress.update(done, stage="downloading")) if progress is not None else None
                ),
            )
            if metadata.xet_file_data is not None:
                stream_xet_file(
                    metadata.xet_file_data,
                    headers,
                    writer,
                    expected_size,
                    item.path,
                    resume_size,
                    stall_timeout_seconds=stall_timeout_seconds,
                )
            else:
                stream_http_file(
                    url_to_download,
                    headers,
                    writer,
                    expected_size,
                    item.path,
                    resume_size,
                    stall_timeout_seconds=stall_timeout_seconds,
                )
            raw.flush()
            os.fsync(raw.fileno())
            hashes = writer.hashes
        if tmp_path.stat().st_size != expected_size:
            raise DownloadIntegrityError(
                f"downloaded size mismatch for {item.path}: expected {expected_size}, got {tmp_path.stat().st_size}"
            )
        if progress is not None:
            progress.update(expected_size, stage="verifying", force=True)
        ensure_hashes_match(item, tmp_path, hashes)
        tmp_path.replace(destination)
        return hashes
    except Exception as exc:
        if not allow_resume and not isinstance(exc, StallTimeoutError):
            tmp_path.unlink(missing_ok=True)
        raise


def incomplete_path_for(destination: Path) -> Path:
    return destination.with_name(f"{destination.name}.incomplete")


def resumable_prefix_size(path: Path, expected_size: int) -> int:
    if not path.exists() or not path.is_file():
        return 0
    size = path.stat().st_size
    if size > expected_size:
        path.unlink()
        return 0
    return size


def hf_transport_metadata(snapshot: HubSnapshot, item: HubFile):
    from huggingface_hub import hf_hub_url
    from huggingface_hub.file_download import get_hf_file_metadata
    from huggingface_hub.utils import build_hf_headers

    headers = build_hf_headers()
    url = hf_hub_url(
        snapshot.repo_id,
        item.path,
        repo_type=snapshot.repo_type,
        revision=snapshot.resolved_commit,
    )
    metadata = get_hf_file_metadata(url=url, headers=headers, retry_on_errors=True)
    if metadata.commit_hash is not None and metadata.commit_hash != snapshot.resolved_commit:
        raise DownloadIntegrityError(
            f"commit metadata mismatch for {item.path}: expected {snapshot.resolved_commit}, got {metadata.commit_hash}"
        )
    url_to_download = metadata.location
    if metadata.xet_file_data is None and url != metadata.location:
        if urlparse(url).netloc != urlparse(metadata.location).netloc:
            headers = dict(headers)
            headers.pop("authorization", None)
    return metadata, headers, url_to_download


def stream_xet_file(
    xet_file_data,
    headers: dict[str, str],
    writer: HashingWriter,
    expected_size: int,
    rel: str,
    resume_size: int,
    *,
    stall_timeout_seconds: int,
) -> None:
    from hf_xet import XetFileInfo
    from huggingface_hub.utils._xet import abort_xet_session, get_xet_session, xet_headers_without_auth
    from tqdm.auto import tqdm

    session = get_xet_session()
    xet_headers = xet_headers_without_auth(headers)
    watchdog = StallWatchdog(
        stall_timeout_seconds,
        rel,
        abort_callback=abort_xet_session if stall_timeout_seconds > 0 else None,
    )
    try:
        with watchdog:
            group = session.new_download_stream_group(
                token_refresh_url=xet_file_data.refresh_route,
                token_refresh_headers=headers,
                custom_headers=xet_headers,
            )
            stream = group.download_stream(XetFileInfo(xet_file_data.file_hash, expected_size), start=resume_size)
            display = rel if len(rel) <= 40 else f"(…){rel[-40:]}"
            with tqdm(
                total=expected_size,
                initial=resume_size,
                unit="B",
                unit_scale=True,
                desc=display,
                leave=False,
            ) as progress:
                for chunk in stream:
                    if chunk:
                        writer.write(chunk)
                        watchdog.progress()
                        progress.update(len(chunk))
            watchdog.raise_if_stalled()
    except KeyboardInterrupt:
        abort_xet_session()
        raise
    except Exception as exc:
        if watchdog.stalled:
            raise StallTimeoutError(f"stalled download for {rel} after {stall_timeout_seconds}s") from exc
        raise


def stream_http_file(
    url: str,
    headers: dict[str, str],
    writer: HashingWriter,
    expected_size: int,
    rel: str,
    resume_size: int,
    *,
    stall_timeout_seconds: int,
) -> None:
    from huggingface_hub.file_download import http_get
    from huggingface_hub import constants

    previous_timeout = constants.HF_HUB_DOWNLOAD_TIMEOUT
    if stall_timeout_seconds > 0:
        constants.HF_HUB_DOWNLOAD_TIMEOUT = stall_timeout_seconds
    try:
        http_get(
            url=url,
            temp_file=writer,
            resume_size=resume_size,
            headers=headers,
            expected_size=expected_size,
            displayed_filename=rel,
            _nb_retries=0 if stall_timeout_seconds > 0 else 5,
        )
    except Exception as exc:
        if stall_timeout_seconds > 0 and is_timeout_exception(exc):
            raise StallTimeoutError(f"stalled HTTP download for {rel} after {stall_timeout_seconds}s") from exc
        raise
    finally:
        constants.HF_HUB_DOWNLOAD_TIMEOUT = previous_timeout


def is_timeout_exception(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or exc.__class__.__name__ in {"Timeout", "ReadTimeout", "ReadTimeoutError"}


class StallWatchdog:
    def __init__(self, timeout_seconds: int, rel: str, *, abort_callback=None):
        self.timeout_seconds = timeout_seconds
        self.rel = rel
        self.abort_callback = abort_callback
        self.stalled = False
        self._last_progress = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self.timeout_seconds > 0:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        return False

    def progress(self) -> None:
        self._last_progress = time.monotonic()

    def raise_if_stalled(self) -> None:
        if self.stalled:
            raise StallTimeoutError(f"stalled download for {self.rel} after {self.timeout_seconds}s")

    def _run(self) -> None:
        while not self._stop.wait(min(1.0, max(0.01, self.timeout_seconds / 10))):
            if time.monotonic() - self._last_progress < self.timeout_seconds:
                continue
            self.stalled = True
            if self.abort_callback is not None:
                self.abort_callback()
            return


def ensure_hashes_match(item: HubFile, path: Path, hashes: FileHashes) -> None:
    expected_size = require_expected_size(item)
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise DownloadIntegrityError(f"size mismatch for {item.path}: expected {expected_size}, got {actual_size}")
    if item.lfs_sha256 is not None:
        if hashes.sha256 != item.lfs_sha256:
            raise DownloadIntegrityError(f"sha256 mismatch for {item.path}")
        return
    if item.blob_id is not None:
        if hashes.git_blob_sha1 != item.blob_id:
            raise DownloadIntegrityError(f"git blob hash mismatch for {item.path}")
        return
    raise DownloadIntegrityError(f"missing remote hash metadata for {item.path}")


def require_expected_size(item: HubFile) -> int:
    if item.size is None:
        raise DownloadIntegrityError(f"missing size metadata for {item.path}")
    return item.size


def cached_manifest_verifies(root: Path, metadata) -> bool:
    manifest = load_manifest(root)
    if not manifest:
        return False
    for item in metadata:
        rel = metadata_path(item)
        path = root / rel
        if not path.exists() or not path.is_file():
            return False
        stat = path.stat()
        row = manifest.get(rel)
        if not record_is_current(row, stat.st_size, stat.st_mtime_ns):
            return False
        expected_lfs_hash = metadata_lfs_sha256(item)
        if expected_lfs_hash is not None and row.get("sha256") != expected_lfs_hash:
            return False
        expected_blob_id = metadata_blob_id(item)
        if expected_lfs_hash is None and expected_blob_id is not None and row.get("git_blob_sha1") != expected_blob_id:
            return False
    return True


def download_staging_dir(
    config: Config,
    repo_id: str,
    repo_type: str,
    revision: str,
    allow_patterns: list[str] | None,
) -> Path:
    tmp_root = Path(config.tmp_dir) if config.tmp_dir is not None else Path(config.directory) / ".tmp"
    scope = "all" if allow_patterns is None else "allow-" + stable_digest("\n".join(sorted(allow_patterns)))[:16]
    slug = safe_slug(f"{repo_type}-{repo_id}-{revision}-{scope}")
    return tmp_root / "downloads" / f"{slug}-{stable_digest(f'{repo_type}\n{repo_id}\n{revision}\n{scope}')[:16]}"


def download_environment(env: dict[str, str], staging_dir: Path) -> dict[str, str]:
    cache_dir = staging_dir / ".cache"
    tmp_dir = staging_dir / ".tmp"
    scoped = dict(env)
    scoped["HF_HOME"] = str(cache_dir / "home")
    scoped["HF_HUB_CACHE"] = str(cache_dir / "hub")
    scoped["HF_ASSETS_CACHE"] = str(cache_dir / "assets")
    scoped["HF_XET_CACHE"] = str(cache_dir / "xet")
    scoped["TRANSFORMERS_CACHE"] = str(cache_dir / "transformers")
    scoped["XDG_CACHE_HOME"] = str(cache_dir / "xdg")
    scoped["TMPDIR"] = str(tmp_dir)
    return scoped


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._=-]+", "-", value).strip(".-")
    return slug[:96] or "download"


def stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
