from dataclasses import dataclass

from model_mirror.card import (
    card_asset_paths,
    download_card_assets,
    image_references,
    markdown_destination,
    normalize_repo_path,
    resolve_repo_asset,
    resolve_huggingface_asset_url,
    select_readme_path,
)
from model_mirror.config import Config
from model_mirror.hub import HubSnapshot
from model_mirror.state import VerificationState, read_verification_state, write_verification_state


@dataclass
class FakeFile:
    path: str
    size: int = 1


class FakeHub:
    def __init__(self, files, contents=None, commit="commit123", files_by_revision=None):
        self.files = files
        self.contents = contents or {}
        self.commit = commit
        self.files_by_revision = files_by_revision or {}
        self.downloads = []

    def snapshot(self, repo_id, repo_type, revision):
        files = self.files_by_revision.get(revision, self.files)
        commit = revision if revision in self.files_by_revision else self.commit
        return HubSnapshot(repo_id, repo_type, revision, commit, files)

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        self.downloads.append((repo_id, repo_type, revision, allow_patterns))
        files = self.files_by_revision.get(revision, self.files)
        for item in files:
            if allow_patterns and item.path not in allow_patterns:
                continue
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.contents.get(item.path, "x" * item.size), encoding="utf-8")
        return local_dir


def test_download_card_assets_fetches_readme_and_referenced_repo_images(tmp_path):
    readme = """
![hero](assets/hero.png)
![ignored](https://example.com/remote.png)
<img src="https://huggingface.co/datasets/org/data/resolve/main/media/chart.jpg">
![ref][badge]

[badge]: ./badge.svg "badge"
"""
    hub = FakeHub(
        [
            FakeFile("README.md"),
            FakeFile("assets/hero.png"),
            FakeFile("media/chart.jpg"),
            FakeFile("badge.svg"),
            FakeFile("data.parquet"),
        ],
        {"README.md": readme},
    )

    result = download_card_assets(
        Config(directory=tmp_path),
        "org/data",
        hub=hub,
        repo_type="dataset",
    )

    root = tmp_path / "datasets" / "org" / "data"
    state = read_verification_state(root)
    assert result.status == "downloaded"
    assert result.paths == ["README.md", "assets/hero.png", "media/chart.jpg", "badge.svg"]
    assert (root / "README.md").exists()
    assert (root / "assets" / "hero.png").exists()
    assert (root / "media" / "chart.jpg").exists()
    assert not (root / "data.parquet").exists()
    assert hub.downloads[0] == ("org/data", "dataset", "commit123", ["README.md"])
    assert hub.downloads[1] == (
        "org/data",
        "dataset",
        "commit123",
        ["README.md", "assets/hero.png", "media/chart.jpg", "badge.svg"],
    )
    assert state.status == "card-only"
    assert state.repo_type == "dataset"


def test_download_card_assets_reports_missing_readme(tmp_path):
    result = download_card_assets(
        Config(directory=tmp_path),
        "org/data",
        hub=FakeHub([FakeFile("data.parquet")]),
        repo_type="dataset",
    )

    assert result.status == "missing-readme"
    assert result.paths == []


def test_download_card_assets_fetches_same_repo_huggingface_url_at_pinned_revision(tmp_path):
    readme = """
<img src="https://huggingface.co/datasets/org/data/resolve/oldcommit/assets/hero.png">
"""
    hub = FakeHub(
        [FakeFile("README.md")],
        {"README.md": readme, "assets/hero.png": "png"},
        files_by_revision={"oldcommit": [FakeFile("README.md"), FakeFile("assets/hero.png")]},
    )

    result = download_card_assets(Config(directory=tmp_path), "org/data", hub=hub, repo_type="dataset")

    assert result.paths == ["README.md", "assets/hero.png"]
    assert (tmp_path / "datasets" / "org" / "data" / "assets" / "hero.png").exists()
    assert hub.downloads[-1] == ("org/data", "dataset", "oldcommit", ["assets/hero.png"])


def test_download_card_assets_skips_missing_pinned_revision_asset(tmp_path):
    readme = """
<img src="https://huggingface.co/datasets/org/data/resolve/oldcommit/assets/missing.png">
"""
    hub = FakeHub(
        [FakeFile("README.md")],
        {"README.md": readme},
        files_by_revision={"oldcommit": [FakeFile("README.md")]},
    )

    result = download_card_assets(Config(directory=tmp_path), "org/data", hub=hub, repo_type="dataset")

    assert result.paths == ["README.md"]
    assert hub.downloads == [
        ("org/data", "dataset", "commit123", ["README.md"]),
        ("org/data", "dataset", "commit123", ["README.md"]),
    ]


def test_download_card_assets_preserves_existing_verification_state(tmp_path):
    root = tmp_path / "datasets" / "org" / "data"
    root.mkdir(parents=True)
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id="org/data",
            repo_type="dataset",
            requested_revision="main",
            resolved_commit="old",
            upstream_commit="old",
            upstream_status="current",
        ),
    )
    hub = FakeHub([FakeFile("README.md")], {"README.md": "hello"})

    result = download_card_assets(Config(directory=tmp_path), "org/data", hub=hub, repo_type="dataset")

    assert result.status == "downloaded"
    state = read_verification_state(root)
    assert state.status == "clean"
    assert state.resolved_commit == "old"


def test_download_card_assets_can_skip_checksum_manifest(tmp_path):
    hub = FakeHub([FakeFile("README.md")], {"README.md": "hello"})

    result = download_card_assets(
        Config(directory=tmp_path, checksum=False),
        "org/data",
        hub=hub,
        repo_type="dataset",
    )

    assert result.status == "downloaded"
    assert not (tmp_path / "datasets" / "org" / "data" / ".manifest").exists()


def test_card_asset_paths_resolves_only_available_repo_local_assets():
    available = {
        "README.md",
        "docs/screenshot.png",
        "root.png",
        "images/logo.svg",
        "space name.png",
    }
    markdown = """
![relative](docs/screenshot.png)
![absolute](/root.png)
![encoded](space%20name.png)
![up](../outside.png)
![missing](missing.png)
![remote](https://example.com/x.png)
<img src="https://huggingface.co/org/model/blob/main/images/logo.svg">
"""

    assert card_asset_paths(markdown, "README.md", "org/model", "model", available) == [
        "docs/screenshot.png",
        "root.png",
        "space name.png",
        "images/logo.svg",
    ]


def test_image_reference_helpers_cover_markdown_forms():
    markdown = """
![inline](<assets/hero image.png>)
<img alt="x" src='media/a.png'>
![ref][]
[ref]: badge.svg
"""

    assert image_references(markdown) == ["assets/hero image.png", "media/a.png", "badge.svg"]
    assert image_references("![missing][missing]\n") == []
    assert markdown_destination("image.png \"title\"") == "image.png"
    assert markdown_destination("<image with spaces.png>") == "image with spaces.png"
    assert markdown_destination("<unterminated.png") == "unterminated.png"
    assert markdown_destination("   ") == ""


def test_path_helpers_reject_non_repo_assets_and_path_traversal():
    assert select_readme_path({"readme.md"}) == "readme.md"
    assert select_readme_path({"docs/README.md"}) is None
    assert normalize_repo_path("assets/../logo.png") == "logo.png"
    assert normalize_repo_path("") is None
    assert normalize_repo_path("../secret.png") is None
    assert resolve_repo_asset("mailto:test@example.com", "README.md", "org/model", "model", {"x.png"}) is None
    assert resolve_repo_asset("#anchor", "README.md", "org/model", "model", {"x.png"}) is None
    assert resolve_huggingface_asset_url("example.com", "/org/model/blob/main/a.png", "org/model", "model") is None
    assert (
        resolve_huggingface_asset_url("huggingface.co", "/models/org/model/blob/main/a.png", "org/model", "model")
        is None
    )
    assert (
        resolve_huggingface_asset_url("huggingface.co", "/org/data/blob/main/a.png", "org/data", "dataset")
        is None
    )
    assert (
        resolve_huggingface_asset_url("huggingface.co", "/org/other/blob/main/a.png", "org/model", "model")
        is None
    )
    assert resolve_huggingface_asset_url("huggingface.co", "/org/model/tree/main/a.png", "org/model", "model") is None
    assert resolve_huggingface_asset_url(
        "huggingface.co",
        "/datasets/org/data/blob/main/a.png",
        "org/data",
        "dataset",
    ) == (None, "a.png")
    assert resolve_huggingface_asset_url(
        "huggingface.co",
        "/spaces/org/space/resolve/main/a.png",
        "org/space",
        "space",
    ) == (None, "a.png")
    assert resolve_huggingface_asset_url(
        "huggingface.co",
        "/org/model/resolve/abc123/a.png",
        "org/model",
        "model",
    ) == ("abc123", "a.png")
