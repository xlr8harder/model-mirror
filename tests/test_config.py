from pathlib import Path

import yaml

import model_mirror.config as config_module
from model_mirror.config import (
    Config,
    apply_hf_environment,
    archive_path,
    detect_token_path,
    hf_token_available,
    load_config,
    parse_bool,
    parse_optional_positive_int,
    parse_positive_int,
    save_config,
    safe_repo_path,
    token_path_candidates,
)


def test_load_config_defaults_are_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = load_config(tmp_path / "missing.yaml")

    assert config.directory == tmp_path / ".local" / "share" / "model-mirror"
    assert config.repo_type == "model"
    assert config.revision == "main"
    assert config.checksum is True
    assert config.checksum_workers == 1
    assert config.download_workers == 1
    assert config.verify_after_mirror is True
    assert config.hf_xet_high_performance is False
    assert config.hf_xet_reconstruct_write_sequentially is False
    assert config.hf_xet_num_concurrent_range_gets is None
    assert config.token_path is None


def test_load_config_file_and_environment_override(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "directory": str(tmp_path / "from-file"),
                "revision": "abc123",
                "checksum_workers": 2,
                "download_workers": 3,
                "hf_xet_high_performance": True,
                "hf_xet_reconstruct_write_sequentially": True,
                "hf_xet_num_concurrent_range_gets": 8,
                "token_path": str(tmp_path / "token"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_MIRROR_DIRECTORY", str(tmp_path / "from-env"))

    config = load_config(config_path)

    assert config.directory == tmp_path / "from-env"
    assert config.revision == "abc123"
    assert config.checksum_workers == 2
    assert config.download_workers == 3
    assert config.hf_xet_high_performance is True
    assert config.hf_xet_reconstruct_write_sequentially is True
    assert config.hf_xet_num_concurrent_range_gets == 8
    assert config.token_path == tmp_path / "token"


def test_load_config_accepts_legacy_audit_after_mirror_key(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"audit_after_mirror": False}), encoding="utf-8")

    config = load_config(config_path)

    assert config.verify_after_mirror is False


def test_load_config_accepts_empty_yaml_file(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    config = load_config(config_path, environ={})

    assert config.repo_type == "model"


def test_load_config_rejects_non_mapping_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    try:
        load_config(config_path)
    except ValueError as exc:
        assert "YAML mapping" in str(exc)
    else:
        raise AssertionError("non-mapping config should fail")


def test_load_config_environment_overrides_more_fields(tmp_path):
    config = load_config(
        tmp_path / "missing.yaml",
        environ={
            "MODEL_MIRROR_REPO_TYPE": "dataset",
            "MODEL_MIRROR_REVISION": "rev",
            "MODEL_MIRROR_TOKEN_PATH": str(tmp_path / "token"),
            "MODEL_MIRROR_HF_XET_HIGH_PERFORMANCE": "yes",
            "MODEL_MIRROR_HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY": "yes",
            "MODEL_MIRROR_HF_XET_NUM_CONCURRENT_RANGE_GETS": "12",
            "MODEL_MIRROR_CHECKSUM_WORKERS": "3",
            "MODEL_MIRROR_DOWNLOAD_WORKERS": "4",
        },
    )

    assert config.repo_type == "dataset"
    assert config.revision == "rev"
    assert config.token_path == tmp_path / "token"
    assert config.hf_xet_high_performance is True
    assert config.hf_xet_reconstruct_write_sequentially is True
    assert config.hf_xet_num_concurrent_range_gets == 12
    assert config.checksum_workers == 3
    assert config.download_workers == 4


def test_save_config_writes_yaml_without_secret_values(tmp_path):
    config_path = tmp_path / "config.yaml"
    config = Config(
        directory=tmp_path / "archive",
        token_path=tmp_path / "token",
        hf_xet_high_performance=True,
        hf_xet_reconstruct_write_sequentially=True,
        hf_xet_num_concurrent_range_gets=4,
        checksum_workers=2,
        download_workers=3,
    )

    save_config(config, config_path)

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["directory"] == str(tmp_path / "archive")
    assert data["token_path"] == str(tmp_path / "token")
    assert data["hf_xet_reconstruct_write_sequentially"] is True
    assert data["hf_xet_num_concurrent_range_gets"] == 4
    assert data["checksum_workers"] == 2
    assert data["download_workers"] == 3
    assert "token" not in data


def test_save_config_includes_optional_cache_and_tmp_dirs(tmp_path):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path / "archive", cache_dir=tmp_path / "cache", tmp_dir=tmp_path / "tmp")

    save_config(config, config_path)

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["cache_dir"] == str(tmp_path / "cache")
    assert data["tmp_dir"] == str(tmp_path / "tmp")


def test_safe_repo_path_rejects_path_traversal():
    assert safe_repo_path("org/model") == Path("org/model")

    for repo_id in ["../bad", "org/../bad", "org//bad", "org\\bad"]:
        try:
            safe_repo_path(repo_id)
        except ValueError:
            pass
        else:
            raise AssertionError(f"repo id should have been rejected: {repo_id}")


def test_archive_path_uses_repo_type_directories(tmp_path):
    config = Config(directory=tmp_path)

    assert archive_path(config, "org/model") == tmp_path / "models" / "org" / "model"
    assert archive_path(config, "org/data", repo_type="dataset") == tmp_path / "datasets" / "org" / "data"
    assert archive_path(config, "org/space", repo_type="space") == tmp_path / "spaces" / "org" / "space"


def test_archive_path_rejects_unknown_repo_type(tmp_path):
    try:
        archive_path(Config(directory=tmp_path), "org/model", repo_type="unknown")
    except ValueError as exc:
        assert "Unsupported repo type" in str(exc)
    else:
        raise AssertionError("unknown repo type should fail")


def test_apply_hf_environment_keeps_all_cache_state_under_archive(tmp_path, monkeypatch):
    config = Config(
        directory=tmp_path,
        hf_xet_high_performance=True,
        hf_xet_reconstruct_write_sequentially=True,
        hf_xet_num_concurrent_range_gets=6,
        token_path=tmp_path / "token",
    )
    env = apply_hf_environment(config, environ={})

    assert env["HF_HOME"] == str(tmp_path / ".cache")
    assert env["HF_HUB_CACHE"] == str(tmp_path / ".cache" / "hub")
    assert env["HF_ASSETS_CACHE"] == str(tmp_path / ".cache" / "assets")
    assert env["HF_XET_CACHE"] == str(tmp_path / ".cache" / "xet")
    assert env["TMPDIR"] == str(tmp_path / ".tmp")
    assert env["HF_TOKEN_PATH"] == str(tmp_path / "token")
    assert env["HF_XET_HIGH_PERFORMANCE"] == "1"
    assert env["HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"] == "1"
    assert env["HF_XET_NUM_CONCURRENT_RANGE_GETS"] == "6"


def test_apply_hf_environment_overrides_inherited_cache_paths(tmp_path):
    inherited = {
        "HF_HOME": "/small/cache",
        "HF_HUB_CACHE": "/small/cache/hub",
        "HF_ASSETS_CACHE": "/small/cache/assets",
        "HF_XET_CACHE": "/small/cache/xet",
        "HF_XET_HIGH_PERFORMANCE": "1",
        "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY": "1",
        "HF_XET_NUM_CONCURRENT_RANGE_GETS": "64",
        "TRANSFORMERS_CACHE": "/small/cache/transformers",
        "XDG_CACHE_HOME": "/small/cache/xdg",
        "TMPDIR": "/small/tmp",
        "HF_TOKEN": "hf_secret",
    }

    env = apply_hf_environment(Config(directory=tmp_path), environ=inherited)

    assert env["HF_HOME"] == str(tmp_path / ".cache")
    assert env["HF_HUB_CACHE"] == str(tmp_path / ".cache" / "hub")
    assert env["HF_ASSETS_CACHE"] == str(tmp_path / ".cache" / "assets")
    assert env["HF_XET_CACHE"] == str(tmp_path / ".cache" / "xet")
    assert env["TRANSFORMERS_CACHE"] == str(tmp_path / ".cache" / "transformers")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / ".cache" / "xdg")
    assert env["TMPDIR"] == str(tmp_path / ".tmp")
    assert env["HF_TOKEN"] == "hf_secret"
    assert "HF_XET_HIGH_PERFORMANCE" not in env
    assert "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY" not in env
    assert "HF_XET_NUM_CONCURRENT_RANGE_GETS" not in env


def test_apply_hf_environment_uses_legacy_token_path_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".cache" / "huggingface" / "token"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("hf_example", encoding="utf-8")

    env = apply_hf_environment(Config(directory=tmp_path / "archive"), environ={})

    assert env["HF_TOKEN_PATH"] == str(legacy)


def test_apply_hf_environment_detects_hf_home_token_before_relocating_cache(tmp_path):
    hf_home = tmp_path / "hf-home"
    token = hf_home / "token"
    hf_home.mkdir()
    token.write_text("hf_example", encoding="utf-8")

    env = apply_hf_environment(
        Config(directory=tmp_path / "archive"),
        environ={"HF_HOME": str(hf_home)},
    )

    assert env["HF_TOKEN_PATH"] == str(token)
    assert env["HF_HOME"] == str(tmp_path / "archive" / ".cache")


def test_detect_token_path_prefers_configured_path_even_if_missing(tmp_path):
    configured = tmp_path / "configured-token"
    fallback = tmp_path / "hf-home" / "token"
    fallback.parent.mkdir()
    fallback.write_text("hf_example", encoding="utf-8")

    detected = detect_token_path(
        Config(directory=tmp_path / "archive", token_path=configured),
        environ={"HF_HOME": str(fallback.parent)},
    )

    assert detected == configured


def test_token_path_candidates_include_common_locations_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path / "home")

    candidates = token_path_candidates(
        {
            "HF_TOKEN_PATH": str(tmp_path / "token"),
            "MODEL_MIRROR_TOKEN_PATH": str(tmp_path / "token"),
            "HF_HOME": str(tmp_path / "hf-home"),
        }
    )

    assert candidates == [
        tmp_path / "token",
        tmp_path / "hf-home" / "token",
        tmp_path / "home" / ".cache" / "huggingface" / "token",
        tmp_path / "home" / ".huggingface" / "token",
    ]


def test_hf_token_available_checks_env_token_and_nonempty_token_file(tmp_path):
    token = tmp_path / "token"

    assert hf_token_available({"HF_TOKEN": "hf_secret"}) is True
    assert hf_token_available({"HF_TOKEN_PATH": str(token)}) is False

    token.write_text("", encoding="utf-8")
    assert hf_token_available({"HF_TOKEN_PATH": str(token)}) is False

    token.write_text("hf_example", encoding="utf-8")
    assert hf_token_available({"HF_TOKEN_PATH": str(token)}) is True


def test_hf_token_available_handles_token_file_stat_errors(tmp_path, monkeypatch):
    def raise_stat(self):
        raise OSError("stat failed")

    monkeypatch.setattr(config_module.Path, "is_file", lambda self: True)
    monkeypatch.setattr(config_module.Path, "stat", raise_stat)

    assert hf_token_available({"HF_TOKEN_PATH": str(tmp_path / "token")}) is False


def test_apply_hf_environment_leaves_optional_token_and_xet_knobs_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module.Path, "home", lambda: tmp_path / "home")

    env = apply_hf_environment(Config(directory=tmp_path / "archive"), environ={})

    assert "HF_TOKEN_PATH" not in env
    assert "HF_XET_HIGH_PERFORMANCE" not in env
    assert "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY" not in env
    assert "HF_XET_NUM_CONCURRENT_RANGE_GETS" not in env


def test_parse_bool_handles_common_values():
    assert parse_bool(True) is True
    assert parse_bool(False) is False
    assert parse_bool("on") is True
    assert parse_bool("0") is False
    assert parse_bool(1) is True


def test_positive_int_parsers_handle_defaults_and_invalid_values():
    assert parse_positive_int("", default=7) == 7
    assert parse_optional_positive_int("") is None
    assert parse_optional_positive_int("5") == 5

    try:
        parse_positive_int("0", default=1)
    except ValueError as exc:
        assert "positive integer" in str(exc)
    else:
        raise AssertionError("zero should be rejected")
