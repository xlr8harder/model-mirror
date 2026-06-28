import os
import sys
from types import SimpleNamespace

import model_mirror.config as config_module
from model_mirror.config import Config
from model_mirror.hub import HuggingFaceHub


def test_huggingface_hub_adapter_uses_configured_environment(tmp_path, monkeypatch, capsys):
    captured = {}
    token_path = tmp_path / "token"
    token_path.write_text("hf_example", encoding="utf-8")

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            captured["repo_info"] = (repo_id, repo_type, revision, files_metadata)
            captured["hf_home"] = os.environ["HF_HOME"]
            captured["token_path"] = os.environ["HF_TOKEN_PATH"]
            siblings = [
                SimpleNamespace(rfilename="a.bin", size=3, lfs=SimpleNamespace(sha256="abc"), blob_id="pointer"),
                SimpleNamespace(rfilename="README.md", size=4, lfs=None, blob_id="blob123"),
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

    config = Config(directory=tmp_path, hf_xet_high_performance=True, token_path=token_path)
    hub = HuggingFaceHub(config)

    files = hub.files("org/model", "model", "main")
    snapshot = hub.snapshot("org/model", "model", "main")
    path = hub.snapshot_download("org/model", "model", "main", tmp_path / "models" / "org" / "model")

    assert captured["repo_info"] == ("org/model", "model", "main", True)
    assert captured["hf_home"] == str(tmp_path / ".cache")
    assert captured["token_path"] == str(token_path)
    assert captured["snapshot"] == ("org/model", "model", "main", str(path), None)
    assert captured["xet"] == "1"
    assert snapshot.requested_revision == "main"
    assert snapshot.resolved_commit == "commit123"
    assert files[0].lfs_sha256 == "abc"
    assert files[1].lfs_sha256 is None
    assert files[1].blob_id == "blob123"
    assert capsys.readouterr().err == ""


def test_huggingface_hub_warns_once_when_no_token_is_available(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN_PATH", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("MODEL_MIRROR_TOKEN_PATH", raising=False)
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path / "home")

    class FakeApi:
        def repo_info(self, repo_id, repo_type, revision, files_metadata):
            return SimpleNamespace(sha="commit123", siblings=[])

    def fake_snapshot_download(repo_id, repo_type, revision, local_dir, allow_patterns=None):
        return local_dir

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(HfApi=FakeApi, snapshot_download=fake_snapshot_download),
    )

    hub = HuggingFaceHub(Config(directory=tmp_path))

    hub.files("org/model", "model", "main")
    hub.snapshot_download("org/model", "model", "main", tmp_path / "models" / "org" / "model")

    err = capsys.readouterr().err
    assert err.count("no Hugging Face token found") == 1
    assert "model-mirror config set token-path /path/to/huggingface/token" in err
    assert "hf_secret" not in err
