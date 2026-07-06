from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .checksums import write_checksums
from .config import Config, archive_path
from .hub import HuggingFaceHub, get_snapshot
from .lock import ModelLock
from .state import VerificationState, read_verification_state, write_verification_state


INLINE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
REFERENCE_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
REFERENCE_DEF_RE = re.compile(r"^\[([^\]]+)\]:\s*(\S+)", re.MULTILINE)
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"][^>]*>", re.IGNORECASE)


@dataclass(slots=True)
class CardDownloadResult:
    status: str
    path: Path
    paths: list[str]


@dataclass(frozen=True, slots=True)
class CardAssetReference:
    path: str
    revision: str | None = None


def download_card_assets(
    config: Config,
    repo_id: str,
    *,
    hub=None,
    repo_type: str | None = None,
    revision: str | None = None,
) -> CardDownloadResult:
    selected_type = repo_type or config.repo_type
    selected_revision = revision or config.revision
    selected_hub = hub or HuggingFaceHub(config)
    destination = archive_path(config, repo_id, selected_type)
    with ModelLock(destination, "card", repo_id, selected_type):
        existing_state = read_verification_state(destination)
        snapshot = get_snapshot(selected_hub, repo_id, selected_type, selected_revision)
        available_paths = {item.path for item in snapshot.files}
        readme_path = select_readme_path(available_paths)
        if readme_path is None:
            return CardDownloadResult("missing-readme", destination, [])

        destination.mkdir(parents=True, exist_ok=True)
        selected_hub.snapshot_download(
            repo_id,
            selected_type,
            snapshot.resolved_commit,
            destination,
            allow_patterns=[readme_path],
        )
        readme_text = (destination / readme_path).read_text(encoding="utf-8")
        asset_references = card_asset_references(readme_text, readme_path, repo_id, selected_type, available_paths)
        current_paths = [readme_path, *[ref.path for ref in asset_references if ref.revision is None]]
        selected_hub.snapshot_download(
            repo_id,
            selected_type,
            snapshot.resolved_commit,
            destination,
            allow_patterns=current_paths,
        )
        paths = list(current_paths)
        seen_paths = set(paths)
        for revision in sorted({ref.revision for ref in asset_references if ref.revision is not None}):
            revision_snapshot = get_snapshot(selected_hub, repo_id, selected_type, revision)
            revision_paths = {item.path for item in revision_snapshot.files}
            selected_paths = [
                ref.path
                for ref in asset_references
                if ref.revision == revision and ref.path in revision_paths and ref.path not in seen_paths
            ]
            if not selected_paths:
                continue
            selected_hub.snapshot_download(
                repo_id,
                selected_type,
                revision_snapshot.resolved_commit,
                destination,
                allow_patterns=selected_paths,
            )
            paths.extend(selected_paths)
            seen_paths.update(selected_paths)
        if config.checksum:
            write_checksums(destination, max_workers=config.checksum_workers)
        if existing_state is None:
            write_verification_state(
                destination,
                VerificationState(
                    status="card-only",
                    repo_id=repo_id,
                    repo_type=selected_type,
                    requested_revision=selected_revision,
                    resolved_commit=snapshot.resolved_commit,
                    upstream_commit=snapshot.resolved_commit,
                    upstream_status="current",
                    issues=["README and referenced repository assets downloaded"],
                ),
            )
        return CardDownloadResult("downloaded", destination, paths)


def select_readme_path(paths: set[str]) -> str | None:
    if "README.md" in paths:
        return "README.md"
    candidates = sorted(path for path in paths if "/" not in path and path.lower() == "readme.md")
    return candidates[0] if candidates else None


def card_asset_paths(
    markdown: str,
    readme_path: str,
    repo_id: str,
    repo_type: str,
    available_paths: set[str],
) -> list[str]:
    return [reference.path for reference in card_asset_references(markdown, readme_path, repo_id, repo_type, available_paths)]


def card_asset_references(
    markdown: str,
    readme_path: str,
    repo_id: str,
    repo_type: str,
    available_paths: set[str],
) -> list[CardAssetReference]:
    assets: list[CardAssetReference] = []
    seen: set[CardAssetReference] = set()
    for reference in image_references(markdown):
        asset = resolve_repo_asset(reference, readme_path, repo_id, repo_type, available_paths)
        if asset is None or asset in seen:
            continue
        seen.add(asset)
        assets.append(asset)
    return assets


def image_references(markdown: str) -> list[str]:
    references = [markdown_destination(match.group(1)) for match in INLINE_IMAGE_RE.finditer(markdown)]
    references.extend(match.group(1).strip() for match in HTML_IMAGE_RE.finditer(markdown))

    definitions = {
        match.group(1).strip().lower(): markdown_destination(match.group(2))
        for match in REFERENCE_DEF_RE.finditer(markdown)
    }
    for match in REFERENCE_IMAGE_RE.finditer(markdown):
        alt_text, label = match.groups()
        key = (label or alt_text).strip().lower()
        if key in definitions:
            references.append(definitions[key])

    return [reference for reference in references if reference]


def markdown_destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<"):
        end = value.find(">")
        return value[1:end].strip() if end != -1 else value[1:].strip()
    return value.split()[0] if value else ""


def resolve_repo_asset(
    reference: str,
    readme_path: str,
    repo_id: str,
    repo_type: str,
    available_paths: set[str],
) -> CardAssetReference | None:
    parsed = urlsplit(reference.strip())
    if parsed.scheme in {"http", "https"}:
        asset = resolve_huggingface_asset_reference(parsed.netloc, parsed.path, repo_id, repo_type)
        if asset is None:
            return None
        if asset.revision is not None:
            return asset
        candidate = asset.path
    elif parsed.scheme or parsed.path in {"", "."}:
        return None
    else:
        raw_path = parsed.path
        if raw_path.startswith("/"):
            candidate = raw_path.lstrip("/")
        else:
            readme_dir = PurePosixPath(readme_path).parent.as_posix()
            candidate = f"{readme_dir}/{raw_path}" if readme_dir != "." else raw_path

    normalized = normalize_repo_path(candidate)
    return CardAssetReference(normalized) if normalized in available_paths else None


def resolve_huggingface_asset_reference(
    netloc: str,
    path: str,
    repo_id: str,
    repo_type: str,
) -> CardAssetReference | None:
    resolved = resolve_huggingface_asset_url(netloc, path, repo_id, repo_type)
    if resolved is None:
        return None
    revision, asset_path = resolved
    return CardAssetReference(asset_path, revision)


def resolve_huggingface_asset_url(netloc: str, path: str, repo_id: str, repo_type: str) -> tuple[str | None, str] | None:
    if netloc != "huggingface.co":
        return None
    segments = [unquote(segment) for segment in path.strip("/").split("/") if segment]
    prefix = {"model": [], "dataset": ["datasets"], "space": ["spaces"]}[repo_type]
    if segments[: len(prefix)] != prefix:
        return None
    segments = segments[len(prefix):]
    repo_segments = repo_id.split("/")
    if segments[: len(repo_segments)] != repo_segments:
        return None
    remainder = segments[len(repo_segments):]
    if len(remainder) < 3 or remainder[0] not in {"blob", "resolve"}:
        return None
    revision = remainder[1]
    asset_path = "/".join(remainder[2:])
    return None if revision in {"main", "HEAD"} else revision, asset_path


def normalize_repo_path(path: str) -> str | None:
    decoded = unquote(path)
    normalized = posixpath.normpath(decoded).lstrip("/")
    if normalized in {"", "."} or normalized.startswith("../") or "/../" in f"/{normalized}/":
        return None
    return normalized
