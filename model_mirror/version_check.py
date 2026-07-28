from __future__ import annotations

from dataclasses import dataclass
import json
from urllib.request import Request, urlopen

from packaging.version import InvalidVersion, Version


DISTRIBUTION_NAME = "model-mirror-cli"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DISTRIBUTION_NAME}/json"
GITHUB_RELEASES_URL = "https://api.github.com/repos/xlr8harder/model-mirror/releases"
RELEASE_URL_TEMPLATE = "https://github.com/xlr8harder/model-mirror/releases/tag/v{version}"
UPDATE_COMMAND = f"uv tool upgrade {DISTRIBUTION_NAME}"


class VersionCheckError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseNote:
    version: str
    url: str
    notes: str


@dataclass(frozen=True, slots=True)
class VersionCheck:
    installed_version: str
    latest_version: str | None
    status: str
    error: str | None = None
    releases: tuple[ReleaseNote, ...] = ()
    release_notes_error: str | None = None


def fetch_latest_version(*, timeout: float = 5.0) -> str:
    document = _get_json(
        PYPI_JSON_URL,
        source="PyPI",
        timeout=timeout,
    )
    try:
        latest_version = document["info"]["version"]
    except (KeyError, TypeError) as exc:
        raise VersionCheckError("PyPI response did not contain info.version") from exc
    _parse_version(latest_version, "PyPI")
    return latest_version


def fetch_release_notes(
    installed_version: str,
    latest_version: str,
    *,
    timeout: float = 5.0,
) -> tuple[ReleaseNote, ...]:
    installed = _parse_version(installed_version, "installed")
    latest = _parse_version(latest_version, "PyPI")
    releases: list[tuple[Version, ReleaseNote]] = []
    page = 1
    while True:
        document = _get_json(
            f"{GITHUB_RELEASES_URL}?per_page=100&page={page}",
            source="GitHub releases",
            timeout=timeout,
        )
        if not isinstance(document, list):
            raise VersionCheckError("GitHub releases response was not a list")
        for item in document:
            if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
                continue
            tag = item.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith("v"):
                continue
            try:
                release_version = _parse_version(tag[1:], "GitHub release")
            except VersionCheckError:
                continue
            if not installed < release_version <= latest:
                continue
            url = item.get("html_url")
            if not isinstance(url, str) or not url:
                url = RELEASE_URL_TEMPLATE.format(version=tag[1:])
            notes = item.get("body")
            releases.append(
                (
                    release_version,
                    ReleaseNote(
                        version=tag[1:],
                        url=url,
                        notes=notes.strip() if isinstance(notes, str) else "",
                    ),
                )
            )
        if len(document) < 100:
            break
        page += 1
    releases.sort(key=lambda entry: entry[0])
    return tuple(note for _, note in releases)


def check_version(
    installed_version: str,
    *,
    latest_version_provider=fetch_latest_version,
    release_notes_provider=fetch_release_notes,
) -> VersionCheck:
    try:
        installed = _parse_version(installed_version, "installed")
        latest_version = latest_version_provider()
        latest = _parse_version(latest_version, "PyPI")
    except (OSError, ValueError, VersionCheckError) as exc:
        return VersionCheck(
            installed_version=installed_version,
            latest_version=None,
            status="unavailable",
            error=str(exc),
        )
    if installed < latest:
        status = "out-of-date"
    elif installed > latest:
        status = "ahead"
    else:
        status = "current"
    releases: tuple[ReleaseNote, ...] = ()
    release_notes_error = None
    if status == "out-of-date":
        try:
            releases = release_notes_provider(installed_version, latest_version)
        except (OSError, ValueError, VersionCheckError) as exc:
            release_notes_error = str(exc)
    return VersionCheck(
        installed_version=installed_version,
        latest_version=latest_version,
        status=status,
        releases=releases,
        release_notes_error=release_notes_error,
    )


def _get_json(url: str, *, source: str, timeout: float):
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{DISTRIBUTION_NAME}-version-check",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError) as exc:
        raise VersionCheckError(f"{source} request failed: {exc}") from exc


def _parse_version(value: object, source: str) -> Version:
    if not isinstance(value, str) or not value:
        raise VersionCheckError(f"{source} version is missing or invalid")
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise VersionCheckError(f"{source} version is invalid: {value!r}") from exc
