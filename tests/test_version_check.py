from __future__ import annotations

from io import BytesIO
import json

import pytest

import model_mirror.version_check as version_module
from model_mirror.version_check import (
    ReleaseNote,
    VersionCheckError,
    check_version,
    fetch_latest_version,
    fetch_release_notes,
)


def test_fetch_latest_version_uses_pypi_json_api(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["accept"] = request.get_header("Accept")
        captured["user_agent"] = request.get_header("User-agent")
        captured["timeout"] = timeout
        return BytesIO(json.dumps({"info": {"version": "1.2.3"}}).encode())

    monkeypatch.setattr(version_module, "urlopen", fake_urlopen)

    assert fetch_latest_version(timeout=2.5) == "1.2.3"
    assert captured == {
        "url": "https://pypi.org/pypi/model-mirror-cli/json",
        "accept": "application/json",
        "user_agent": "model-mirror-cli-version-check",
        "timeout": 2.5,
    }


def test_fetch_latest_version_wraps_request_errors(monkeypatch):
    def fail_urlopen(request, timeout):
        raise OSError("offline")

    monkeypatch.setattr(version_module, "urlopen", fail_urlopen)

    with pytest.raises(VersionCheckError, match="PyPI request failed: offline"):
        fetch_latest_version()


@pytest.mark.parametrize(
    "document,error",
    [
        ({}, "did not contain info.version"),
        (None, "did not contain info.version"),
        ({"info": {"version": ""}}, "version is missing or invalid"),
        ({"info": {"version": "not a version"}}, "version is invalid"),
    ],
)
def test_fetch_latest_version_rejects_invalid_responses(monkeypatch, document, error):
    monkeypatch.setattr(
        version_module,
        "urlopen",
        lambda request, timeout: BytesIO(json.dumps(document).encode()),
    )

    with pytest.raises(VersionCheckError, match=error):
        fetch_latest_version()


def test_fetch_release_notes_returns_every_published_version_in_range(monkeypatch):
    document = [
        {"tag_name": "v1.3.0", "html_url": "https://example/1.3.0", "body": " third "},
        {"tag_name": "v1.1.0", "html_url": "https://example/1.1.0", "body": "first"},
        {"tag_name": "v1.2.0", "html_url": "", "body": None},
        {"tag_name": "v1.0.0", "body": "installed"},
        {"tag_name": "v2.0.0", "body": "future"},
        {"tag_name": "v1.2.1", "body": "draft", "draft": True},
        {"tag_name": "v1.2.2", "body": "prerelease", "prerelease": True},
        {"tag_name": "invalid", "body": "not a version"},
        {"tag_name": "v-invalid", "body": "not a version"},
        "not a release",
    ]
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return BytesIO(json.dumps(document).encode())

    monkeypatch.setattr(version_module, "urlopen", fake_urlopen)

    assert fetch_release_notes("1.0.0", "1.3.0", timeout=3.0) == (
        ReleaseNote("1.1.0", "https://example/1.1.0", "first"),
        ReleaseNote(
            "1.2.0",
            "https://github.com/xlr8harder/model-mirror/releases/tag/v1.2.0",
            "",
        ),
        ReleaseNote("1.3.0", "https://example/1.3.0", "third"),
    )
    assert captured == {
        "url": "https://api.github.com/repos/xlr8harder/model-mirror/releases?per_page=100&page=1",
        "timeout": 3.0,
    }


def test_fetch_release_notes_paginates(monkeypatch):
    pages = [
        [{"tag_name": "not-a-release"}] * 100,
        [{"tag_name": "v1.1.0", "html_url": "https://example/1.1.0", "body": "notes"}],
    ]
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        return BytesIO(json.dumps(pages.pop(0)).encode())

    monkeypatch.setattr(version_module, "urlopen", fake_urlopen)

    assert fetch_release_notes("1.0.0", "1.1.0") == (
        ReleaseNote("1.1.0", "https://example/1.1.0", "notes"),
    )
    assert urls == [
        "https://api.github.com/repos/xlr8harder/model-mirror/releases?per_page=100&page=1",
        "https://api.github.com/repos/xlr8harder/model-mirror/releases?per_page=100&page=2",
    ]


def test_fetch_release_notes_rejects_non_list_response(monkeypatch):
    monkeypatch.setattr(
        version_module,
        "urlopen",
        lambda request, timeout: BytesIO(b"{}"),
    )

    with pytest.raises(VersionCheckError, match="response was not a list"):
        fetch_release_notes("1.0.0", "1.1.0")


@pytest.mark.parametrize(
    "installed,latest,status",
    [
        ("1.0.0", "1.0.0", "current"),
        ("1.0.0", "1.0.1", "out-of-date"),
        ("1.1.0", "1.0.1", "ahead"),
    ],
)
def test_check_version_compares_pep_440_versions(installed, latest, status):
    result = check_version(
        installed,
        latest_version_provider=lambda: latest,
        release_notes_provider=lambda installed, latest: (),
    )

    assert result.installed_version == installed
    assert result.latest_version == latest
    assert result.status == status
    assert result.error is None
    assert result.releases == ()
    assert result.release_notes_error is None


@pytest.mark.parametrize(
    "installed,provider,error",
    [
        ("invalid", lambda: "1.0.0", "installed version is invalid"),
        ("1.0.0", lambda: None, "PyPI version is missing or invalid"),
        ("1.0.0", lambda: (_ for _ in ()).throw(OSError("offline")), "offline"),
        ("1.0.0", lambda: (_ for _ in ()).throw(ValueError("bad response")), "bad response"),
        ("1.0.0", lambda: (_ for _ in ()).throw(VersionCheckError("bad metadata")), "bad metadata"),
    ],
)
def test_check_version_reports_unavailable(installed, provider, error):
    result = check_version(installed, latest_version_provider=provider)

    assert result.installed_version == installed
    assert result.latest_version is None
    assert result.status == "unavailable"
    assert error in result.error


def test_check_version_includes_release_notes_for_every_pending_release():
    notes = (
        ReleaseNote("1.1.0", "https://example/1.1.0", "first"),
        ReleaseNote("1.2.0", "https://example/1.2.0", "second"),
    )

    result = check_version(
        "1.0.0",
        latest_version_provider=lambda: "1.2.0",
        release_notes_provider=lambda installed, latest: notes,
    )

    assert result.releases == notes
    assert result.release_notes_error is None


@pytest.mark.parametrize(
    "error",
    [
        OSError("offline"),
        ValueError("bad response"),
        VersionCheckError("bad metadata"),
    ],
)
def test_check_version_keeps_update_status_when_release_notes_are_unavailable(error):
    def fail_notes(installed, latest):
        raise error

    result = check_version(
        "1.0.0",
        latest_version_provider=lambda: "1.1.0",
        release_notes_provider=fail_notes,
    )

    assert result.status == "out-of-date"
    assert result.releases == ()
    assert result.release_notes_error == str(error)
