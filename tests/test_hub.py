import os
import sys
from types import SimpleNamespace

from model_mirror.config import Config
from model_mirror.hub import HuggingFaceHub


def test_huggingface_hub_adapter_uses_configured_environment(tmp_path, monkeypatch):
    captured = {}

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            captured["repo_info"] = (repo_id, repo_type, revision, files_metadata)
            captured["hf_home"] = os.environ["HF_HOME"]
            siblings = [
                SimpleNamespace(rfilename="a.bin", size=3, lfs=SimpleNamespace(sha256="abc")),
                SimpleNamespace(rfilename="README.md", size=4, lfs=None),
            ]
            return SimpleNamespace(sha="commit123", siblings=siblings)

    def fake_snapshot_download(repo_id, repo_type, revision, local_dir, allow_patterns=None):
        captured["snapshot"] = (repo_id, repo_type, revision, local_dir, allow_patterns)
        captured["xet"] = os.environ["HF_XET_HIGH_PERFORMANCE"]
        return local_dir

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=FakeApi, snapshot_download=fake_snapshot_download),
    )

    config = Config(directory=tmp_path, hf_xet_high_performance=True)
    hub = HuggingFaceHub(config)

    files = hub.files("org/model", "model", "main")
    snapshot = hub.snapshot("org/model", "model", "main")
    path = hub.snapshot_download("org/model", "model", "main", tmp_path / "models" / "org" / "model")

    assert captured["repo_info"] == ("org/model", "model", "main", True)
    assert captured["hf_home"] == str(tmp_path / ".cache")
    assert captured["snapshot"] == ("org/model", "model", "main", str(path), None)
    assert captured["xet"] == "1"
    assert snapshot.requested_revision == "main"
    assert snapshot.resolved_commit == "commit123"
    assert files[0].lfs_sha256 == "abc"
    assert files[1].lfs_sha256 is None
