from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import runpy
import sys
from types import SimpleNamespace

import yaml
import pytest

import model_mirror.cli as cli_module
from model_mirror.checksums import write_checksums
from model_mirror.cli import (
    main,
    list_model_ids,
    parse_age,
    should_skip_recent_clean,
    verification_age_label,
    verification_age_seconds,
)
from model_mirror.config import Config
from model_mirror.hub import HubSnapshot
from model_mirror.lock import ModelLock
from model_mirror.state import VerificationState, read_verification_state, write_verification_state


@dataclass
class FakeFile:
    path: str
    size: int
    lfs_sha256: str | None = None


class FakeHub:
    def __init__(self, metadata):
        self.metadata = metadata
        self.downloads = []
        self.download_revisions = []

    def files(self, repo_id, repo_type, revision):
        return self.metadata

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        self.downloads.append(repo_id)
        self.download_revisions.append(revision)
        for item in self.metadata:
            if allow_patterns and item.path not in allow_patterns:
                continue
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}" if item.path == "config.json" else b"x" * item.size)
        return local_dir


class MovingBranchHub(FakeHub):
    def snapshot(self, repo_id, repo_type, revision):
        commit = {"main": "newcommit", "oldcommit": "oldcommit"}[revision]
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit=commit,
            files=self.metadata,
        )


def test_config_directory_command_writes_config(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "archive"

    rc = main(["--config", str(config_path), "config", "directory", str(archive)])

    assert rc == 0
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["directory"] == str(archive)
    assert str(archive) in capsys.readouterr().out


def test_list_command_prints_mirrored_models(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    (tmp_path / "models" / "org" / "model").mkdir(parents=True)
    (tmp_path / "models" / "org2" / "model2").mkdir(parents=True)
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "org/model" in output
    assert "org2/model2" in output


def test_mirror_command_uses_injected_hub(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "mirror", "org/model"], hub=hub)

    assert rc == 0
    assert hub.downloads == ["org/model"]
    assert "downloaded" in capsys.readouterr().out


def test_mirror_command_accepts_commit_option(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "mirror", "--commit", "abc123", "org/model"], hub=hub)

    assert rc == 0
    assert hub.download_revisions == ["abc123"]
    state = read_verification_state(tmp_path / "models" / "org" / "model")
    assert state.requested_revision == "abc123"
    assert state.resolved_commit == "abc123"


def test_mirror_command_returns_failure_when_post_verify_is_dirty(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("file.bin", 3, lfs_sha256="not-the-downloaded-hash")])

    rc = main(["--config", str(config_path), "mirror", "org/model"], hub=hub)

    assert rc == 1
    assert "downloaded-unverified" in capsys.readouterr().out


def test_mirror_command_reports_busy_model_lock(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    hub = FakeHub([FakeFile("config.json", 2)])

    with ModelLock(archive, "test", "org/model"):
        rc = main(["--config", str(config_path), "mirror", "org/model"], hub=hub)

    assert rc == 1
    assert "model mirror is busy" in capsys.readouterr().out


def test_revision_and_commit_options_are_mutually_exclusive(tmp_path):
    config_path = tmp_path / "config.yaml"

    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "mirror", "--revision", "main", "--commit", "abc123", "org/model"])


def test_main_returns_parser_error_for_unhandled_command(tmp_path, monkeypatch):
    class FakeParser:
        message = None

        def parse_args(self, argv):
            return SimpleNamespace(config=None, command="unknown")

        def error(self, message):
            self.message = message

    parser = FakeParser()
    monkeypatch.setattr(cli_module, "build_parser", lambda: parser)
    monkeypatch.setattr(cli_module, "load_config", lambda path: Config(directory=tmp_path))

    assert cli_module.main([]) == 2
    assert parser.message == "Unhandled command: unknown"


def test_config_show_and_directory_getter(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    token_path = tmp_path / "token"
    config_path.write_text(
        yaml.safe_dump({"directory": str(tmp_path), "token_path": str(token_path)}),
        encoding="utf-8",
    )

    assert main(["--config", str(config_path), "config", "show"]) == 0
    show_output = capsys.readouterr().out
    assert "directory:" in show_output
    assert str(token_path) in show_output

    assert main(["--config", str(config_path), "config", "directory"]) == 0
    assert str(tmp_path) in capsys.readouterr().out


def test_config_show_prints_optional_range_gets_without_token(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"directory": str(tmp_path), "hf_xet_num_concurrent_range_gets": 4}),
        encoding="utf-8",
    )

    assert main(["--config", str(config_path), "config", "show"]) == 0

    output = capsys.readouterr().out
    assert "hf_xet_num_concurrent_range_gets: 4" in output
    assert "token_path:" not in output


def test_config_options_describes_supported_keys(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "config", "options"]) == 0

    output = capsys.readouterr().out
    assert "directory:" in output
    assert "Archive root" in output
    assert "checksum_workers:" in output
    assert "MODEL_MIRROR_CHECKSUM_WORKERS" in output
    assert "64 GB RAM" in output
    assert "hf_xet_reconstruct_write_sequentially:" in output
    assert "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY" in output


def test_config_without_subcommand_defaults_to_options(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "config"]) == 0

    output = capsys.readouterr().out
    assert "directory:" in output
    assert "Archive root" in output


@pytest.mark.parametrize(
    "key,value,expected_key,expected_value",
    [
        ("repo-type", "dataset", "repo_type", "dataset"),
        ("directory", "/tmp/archive", "directory", "/tmp/archive"),
        ("revision", "rev", "revision", "rev"),
        ("checksum", "false", "checksum", False),
        ("checksum-workers", "2", "checksum_workers", 2),
        ("verify-after-mirror", "false", "verify_after_mirror", False),
        ("hf-xet-high-performance", "true", "hf_xet_high_performance", True),
        ("hf-xet-reconstruct-write-sequentially", "true", "hf_xet_reconstruct_write_sequentially", True),
        ("hf-xet-num-concurrent-range-gets", "6", "hf_xet_num_concurrent_range_gets", 6),
        ("token-path", "/tmp/token", "token_path", "/tmp/token"),
        ("cache-dir", "/tmp/cache", "cache_dir", "/tmp/cache"),
        ("tmp-dir", "/tmp/tmp", "tmp_dir", "/tmp/tmp"),
    ],
)
def test_config_set_command_updates_supported_keys(tmp_path, key, value, expected_key, expected_value):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "config", "set", key, value]) == 0

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data[expected_key] == expected_value


def test_config_set_command_rejects_unknown_key(tmp_path):
    config_path = tmp_path / "config.yaml"

    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "config", "set", "unknown", "value"])


def test_handle_config_returns_error_for_unknown_config_command(tmp_path):
    args = SimpleNamespace(config_command="unknown")

    assert cli_module.handle_config(args, Config(directory=tmp_path), None) == 2


def test_list_command_is_empty_when_directory_missing(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path / "missing")}), encoding="utf-8")

    assert main(["--config", str(config_path), "list"]) == 0
    assert capsys.readouterr().out == ""


def test_verify_quick_command_succeeds_with_injected_hub(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "org/model"], hub=hub)

    assert rc == 0
    assert "verified (quick)" in capsys.readouterr().out


def test_verify_uses_stored_commit_and_reports_changed_upstream(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
        ),
    )
    hub = MovingBranchHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "org/model"], hub=hub)

    assert rc == 0
    output = capsys.readouterr().out
    assert "upstream=changed" in output
    state = read_verification_state(archive)
    assert state.resolved_commit == "oldcommit"
    assert state.upstream_commit == "newcommit"


def test_verify_command_reports_failure(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "org/model"], hub=hub)

    assert rc == 1
    assert "verification failed" in capsys.readouterr().out


def test_verify_dataset_skips_model_audit(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "datasets" / "org" / "data"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(
        ["--config", str(config_path), "verify", "--repo-type", "dataset", "--quick", "org/data"],
        hub=hub,
    )

    assert rc == 0
    assert "verified (quick)" in capsys.readouterr().out


def test_verify_all_checks_every_model(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    for repo in ("one/model", "two/model"):
        archive = tmp_path / "models" / repo
        archive.mkdir(parents=True)
        (archive / "config.json").write_text("{}", encoding="utf-8")
        (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "--all"], hub=hub)

    assert rc == 0
    assert capsys.readouterr().out.count("verified (quick)") == 2


def test_verify_all_returns_failure_when_any_model_fails(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    ok_archive = tmp_path / "models" / "ok" / "model"
    ok_archive.mkdir(parents=True)
    (ok_archive / "config.json").write_text("{}", encoding="utf-8")
    (ok_archive / "file.bin").write_bytes(b"abc")
    bad_archive = tmp_path / "models" / "bad" / "model"
    bad_archive.mkdir(parents=True)
    (bad_archive / "config.json").write_text("{}", encoding="utf-8")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "verified (quick): ok/model" in output
    assert "verification failed: bad/model" in output


def test_verify_requires_model_without_all(tmp_path):
    config_path = tmp_path / "config.yaml"

    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "verify"])


def test_mirror_command_no_verify_writes_unverified_state(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "mirror", "--no-verify", "org/model"], hub=hub)

    assert rc == 0
    state = read_verification_state(tmp_path / "models" / "org" / "model")
    assert state.status == "dirty"
    assert state.issues == ["verification skipped"]


def test_list_command_prints_verification_status_and_age(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            checked_at_utc=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds"),
        ),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "org/model" in output
    assert "verification=clean" in output
    assert "age=3h" in output


def test_list_command_prints_busy_lock(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    with ModelLock(archive, "mirror", "org/model"):
        rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "org/model" in output
    assert "busy=" in output
    assert "command=mirror" in output


def test_verify_writes_dirty_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "org/model"], hub=hub)

    assert rc == 1
    state = read_verification_state(tmp_path / "models" / "org" / "model")
    assert state.status == "dirty"
    assert "missing.bin" in state.repair_paths


def test_verify_repair_repairs_dirty_archive(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--quick", "--repair", "org/model"], hub=hub)

    assert rc == 0
    assert (archive / "missing.bin").read_bytes() == b"xxx"
    assert "repaired: org/model" in capsys.readouterr().out
    assert read_verification_state(archive).status == "clean"


def test_repair_command_uses_existing_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    assert rc == 0
    assert "repaired: org/model" in capsys.readouterr().out
    assert (archive / "missing.bin").exists()


def test_repair_command_returns_failure_when_repair_is_incomplete(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    assert rc == 1
    assert "incomplete" in capsys.readouterr().out


def test_update_command_forces_mirror(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "update", "--no-verify", "org/model"], hub=hub)

    assert rc == 0
    assert hub.downloads == ["org/model"]
    assert "updated: org/model" in capsys.readouterr().out


def test_verify_all_max_age_skips_recent_clean_model(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            checked_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--all", "--quick", "--max-age", "7d"], hub=hub)

    assert rc == 0
    assert "skipped recent clean verification" in capsys.readouterr().out


def test_offline_verify_modes(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)

    assert main(["--config", str(config_path), "verify", "--offline", "--quick", "org/model"]) == 1
    assert "offline verification unavailable" in capsys.readouterr().out

    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))
    assert main(["--config", str(config_path), "verify", "--offline", "--quick", "org/model"]) == 0
    assert "verified (offline quick)" in capsys.readouterr().out


def test_offline_full_verify_modes(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    write_checksums(archive)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))

    assert main(["--config", str(config_path), "verify", "--offline", "org/model"]) == 0
    assert "verified (offline full)" in capsys.readouterr().out

    (archive / "file.bin").write_bytes(b"changed")

    assert main(["--config", str(config_path), "verify", "--offline", "org/model"]) == 1
    assert "offline verification failed" in capsys.readouterr().out


def test_list_model_ids_returns_empty_for_missing_archive_root(tmp_path):
    assert list_model_ids(Config(directory=tmp_path / "missing")) == []


def test_should_skip_recent_clean_returns_false_for_missing_or_dirty_state(tmp_path):
    config = Config(directory=tmp_path)

    assert should_skip_recent_clean(config, "org/model", "model", "7d") is False

    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            checked_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )

    assert should_skip_recent_clean(config, "org/model", "model", "7d") is False


def test_cli_module_entrypoint_invokes_main(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["model-mirror", "--config", str(config_path), "list"])

    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("model_mirror.cli", run_name="__main__")

    assert exc.value.code == 0


def test_age_helpers_cover_units_and_invalid_values():
    assert parse_age("10s") == 10
    assert parse_age("2m") == 120
    assert parse_age("3h") == 10800
    assert parse_age("4d") == 345600
    assert parse_age("5") == 5
    with pytest.raises(ValueError):
        parse_age("")

    assert verification_age_seconds("") is None
    assert verification_age_seconds("not-a-date") is None
    assert verification_age_seconds("2026-01-01T00:00:00") is not None
    assert verification_age_label("") == "unknown"
    assert verification_age_label((datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()) == "30s"
    assert verification_age_label((datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()) == "3m"
    assert verification_age_label((datetime.now(timezone.utc) - timedelta(days=3)).isoformat()) == "3d"
