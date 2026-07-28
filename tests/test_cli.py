from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import yaml
import pytest
import argparse

import model_mirror.checksums as checksums_module
import model_mirror.cli as cli_module
from model_mirror.checksums import write_checksums
from model_mirror.cli import (
    format_age_seconds,
    main,
    list_model_ids,
    parse_age,
    print_verification_age,
    should_skip_recent_clean,
    verification_age_label,
    verification_age_seconds,
)
from model_mirror.config import Config
from model_mirror.hub import HubFile, HubSnapshot, download_staging_dir, write_snapshot_plan
from model_mirror.lock import ModelLock
from model_mirror.progress import ProgressEntry, ProgressSnapshot, progress_path
from model_mirror.runtime_cache import CacheIssue, RuntimeCacheLock, write_download_record
from model_mirror.state import VerificationState, read_verification_state, write_verification_state
from model_mirror.verify import RemoteVerifyResult
from model_mirror.version_check import ReleaseNote, VersionCheck


@dataclass
class FakeFile:
    path: str
    size: int
    lfs_sha256: str | None = None


class FakeHub:
    def __init__(self, metadata, metadata_by_revision=None):
        self.metadata = metadata
        self.metadata_by_revision = metadata_by_revision or {}
        self.downloads = []
        self.download_revisions = []

    def files(self, repo_id, repo_type, revision):
        return self.metadata_by_revision.get(revision, self.metadata)

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        self.downloads.append(repo_id)
        self.download_revisions.append(revision)
        selected_metadata = self.metadata_by_revision.get(revision, self.metadata)
        for item in selected_metadata:
            if allow_patterns and item.path not in allow_patterns:
                continue
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}" if item.path == "config.json" else b"x" * item.size)
        return local_dir


class MovingBranchHub(FakeHub):
    def snapshot(self, repo_id, repo_type, revision):
        commit = {"main": "newcommit", "oldcommit": "oldcommit", "newcommit": "newcommit"}[revision]
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit=commit,
            files=self.metadata,
        )


class UnavailableHub:
    def snapshot(self, repo_id, repo_type, revision):
        raise RuntimeError("repository not found")

    def snapshot_download(self, repo_id, repo_type, revision, local_dir, allow_patterns=None):
        raise RuntimeError("repository not found")


class MissingStoredCommitHub:
    def snapshot(self, repo_id, repo_type, revision):
        if revision == "oldcommit":
            raise RuntimeError("stored commit not found")
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit="newcommit",
            files=[FakeFile("file.bin", 3)],
        )


class ModernDownloadHub(FakeHub):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.stall_timeouts = []

    def snapshot(self, repo_id, repo_type, revision):
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit=revision,
            files=self.metadata,
        )

    def download_snapshot(self, snapshot, local_dir, allow_patterns=None, stall_timeout_seconds=None):
        self.downloads.append(snapshot.repo_id)
        self.stall_timeouts.append(stall_timeout_seconds)
        for item in self.metadata:
            path = local_dir / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * item.size)
        return local_dir


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


def test_mirror_command_accepts_stall_timeout_override(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = ModernDownloadHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "mirror", "--no-verify", "--stall-timeout", "0", "org/model"], hub=hub)

    assert rc == 0
    assert hub.stall_timeouts == [0]


def test_mirror_command_uses_process_supervisor_without_injected_hub(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    captured = {}

    def fake_supervisor(raw_argv, args, config):
        captured["raw_argv"] = raw_argv
        captured["model"] = args.model
        captured["directory"] = config.directory
        return 7

    monkeypatch.setattr(cli_module, "run_supervised_mirror", fake_supervisor)

    rc = main(["--config", str(config_path), "mirror", "org/model"])

    assert rc == 7
    assert captured == {
        "raw_argv": ["--config", str(config_path), "mirror", "org/model"],
        "model": "org/model",
        "directory": tmp_path,
    }


def test_should_supervise_mirror_respects_child_env_and_disabled_timeout(tmp_path, monkeypatch):
    args = SimpleNamespace(command="mirror", stall_timeout=None)
    config = Config(directory=tmp_path)

    assert cli_module.should_supervise_mirror(args, config, hub=None) is True
    assert cli_module.effective_stall_timeout(SimpleNamespace(stall_timeout=0), config) == 0

    monkeypatch.setenv("MODEL_MIRROR_SUPERVISED_CHILD", "1")

    assert cli_module.should_supervise_mirror(args, config, hub=None) is False
    assert cli_module.should_supervise_mirror(args, config, hub=object()) is False
    assert cli_module.should_supervise_mirror(SimpleNamespace(command="status"), config, hub=None) is False


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
    assert "download_workers: 1" in show_output
    assert "stall_timeout_seconds: 600" in show_output
    assert "stall_retries: 3" in show_output
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


def test_config_show_omits_null_range_gets_escape_hatch(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"directory": str(tmp_path), "hf_xet_num_concurrent_range_gets": None}),
        encoding="utf-8",
    )

    assert main(["--config", str(config_path), "config", "show"]) == 0

    output = capsys.readouterr().out
    assert "hf_xet_num_concurrent_range_gets:" not in output


def test_config_options_describes_supported_keys(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "config", "options"]) == 0

    output = capsys.readouterr().out
    assert "directory:" in output
    assert "Archive root" in output
    assert "checksum_workers:" in output
    assert "MODEL_MIRROR_CHECKSUM_WORKERS" in output
    assert "download_workers:" in output
    assert "MODEL_MIRROR_DOWNLOAD_WORKERS" in output
    assert "stall_timeout_seconds:" in output
    assert "MODEL_MIRROR_STALL_TIMEOUT" in output
    assert "stall_retries:" in output
    assert "MODEL_MIRROR_STALL_RETRIES" in output
    assert "64 GB RAM" in output
    assert "hf_xet_reconstruct_write_sequentially:" in output
    assert "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY" in output


@pytest.mark.parametrize(
    "command,expected",
    [
        ("mirror", "Exit status: 0 when complete or downloaded cleanly"),
        ("remove", "repository-name confirmation"),
        ("verify", "cached verification data is missing/stale"),
        ("repair", "--force-partial"),
    ],
)
def test_command_help_documents_exit_status_and_risky_options(command, expected, capsys):
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])

    output = capsys.readouterr().out
    normalized_output = " ".join(output.split())
    assert exc.value.code == 0
    assert "Exit status:" in output
    assert expected in normalized_output


def test_main_without_command_prints_full_help(capsys):
    assert main([]) == 0

    output = capsys.readouterr().out
    assert "usage: model-mirror" in output
    assert "mirror" in output
    assert "verify" in output
    assert "help" in output


def test_version_uses_package_metadata(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out == f"model-mirror {cli_module.__version__}\n"


@pytest.mark.parametrize(
    "result,expected_rc,expected",
    [
        (
            VersionCheck("1.0.0", "1.0.0", "current"),
            0,
            ["installed: 1.0.0", "latest: 1.0.0", "status: current"],
        ),
        (
            VersionCheck("1.0.0", "1.0.1", "out-of-date"),
            0,
            [
                "status: out-of-date",
                "update: uv tool upgrade model-mirror-cli",
            ],
        ),
        (
            VersionCheck("1.1.0", "1.0.1", "ahead"),
            0,
            ["status: ahead"],
        ),
        (
            VersionCheck("1.0.0", None, "unavailable", "offline"),
            1,
            ["latest: unavailable", "status: unavailable", "error: offline"],
        ),
    ],
)
def test_version_command_reports_pypi_comparison(monkeypatch, capsys, result, expected_rc, expected):
    monkeypatch.setattr(cli_module, "check_version", lambda installed: result)
    monkeypatch.setattr(cli_module, "load_config", lambda path: pytest.fail("version must not load archive config"))

    assert main(["version"]) == expected_rc

    output = capsys.readouterr().out
    assert all(item in output for item in expected)
    assert ("update:" in output) == (result.status == "out-of-date")


def test_version_command_supports_json(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module,
        "check_version",
        lambda installed: VersionCheck(
            "1.0.0",
            "1.0.2",
            "out-of-date",
            releases=(
                ReleaseNote("1.0.1", "https://example/1.0.1", "first"),
                ReleaseNote("1.0.2", "https://example/1.0.2", "second"),
            ),
        ),
    )

    assert main(["version", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "schema": "model-mirror-version",
        "version": 1,
        "installed_version": "1.0.0",
        "latest_version": "1.0.2",
        "status": "out-of-date",
        "update_command": "uv tool upgrade model-mirror-cli",
        "error": None,
        "release_url": "https://github.com/xlr8harder/model-mirror/releases/tag/v1.0.2",
        "releases": [
            {"version": "1.0.1", "url": "https://example/1.0.1", "notes": "first"},
            {"version": "1.0.2", "url": "https://example/1.0.2", "notes": "second"},
        ],
        "release_notes_error": None,
    }


def test_version_command_prints_all_pending_release_notes(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_module,
        "check_version",
        lambda installed: VersionCheck(
            "1.0.0",
            "1.0.2",
            "out-of-date",
            releases=(
                ReleaseNote("1.0.1", "https://example/1.0.1", "first"),
                ReleaseNote("1.0.2", "https://example/1.0.2", ""),
            ),
            release_notes_error="one release had no notes",
        ),
    )

    assert main(["version"]) == 0

    output = capsys.readouterr().out
    assert output.index("1.0.1:") < output.index("1.0.2:")
    assert "first" in output
    assert "changes_error: one release had no notes" in output


def test_repo_commands_use_configured_repo_type_by_default():
    parser = cli_module.build_parser()
    config = Config(repo_type="dataset")
    repo_commands = [
        ["mirror", "org/data"],
        ["remove", "org/data"],
        ["verify", "org/data"],
        ["repair", "org/data"],
        ["upgrade", "org/data"],
        ["torrent", "publish", "org/data"],
        ["offline", "org/data"],
        ["online", "org/data"],
    ]

    for command in repo_commands:
        args = parser.parse_args(command)
        assert args.repo_type is None
        cli_module.apply_configured_repo_type(args, config)
        assert args.repo_type == "dataset"

    explicit = parser.parse_args(["verify", "--repo-type", "space", "org/app"])
    cli_module.apply_configured_repo_type(explicit, config)
    assert explicit.repo_type == "space"

    archive_status = parser.parse_args(["status"])
    cli_module.apply_configured_repo_type(archive_status, config)
    assert archive_status.repo_type is None


def test_help_command_prints_full_help(capsys):
    assert main(["help"]) == 0

    output = capsys.readouterr().out
    assert "usage: model-mirror" in output
    assert "show help" in output
    assert "status (list)" in output
    assert "\n    list " not in output


def test_help_command_prints_subcommand_help(capsys):
    assert main(["help", "list"]) == 0

    output = capsys.readouterr().out
    assert "usage: model-mirror status" in output
    assert "detailed last-known local state" in output
    assert "deprecated 'list' command" in output
    assert "--repo-type" in output


def test_help_command_rejects_unknown_topic():
    with pytest.raises(SystemExit):
        main(["help", "unknown"])


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
        ("download-workers", "4", "download_workers", 4),
        ("stall-timeout", "0", "stall_timeout_seconds", 0),
        ("stall-timeout-seconds", "0", "stall_timeout_seconds", 0),
        ("stall-retries", "5", "stall_retries", 5),
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


def test_list_command_prints_summary_when_directory_missing(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "missing"
    config_path.write_text(yaml.safe_dump({"directory": str(archive)}), encoding="utf-8")

    assert main(["--config", str(config_path), "list"]) == 0
    output = capsys.readouterr().out
    assert f"archive: {archive}  mirrors=0  recorded_payload=0 B" in output
    assert "TYPE  REPOSITORY  FILES  SIZE  COMMIT  CHECKED  EXCEPTIONS" in output


def test_verify_cached_command_succeeds_with_injected_hub(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    assert rc == 0
    assert "verified (cached)" in capsys.readouterr().out


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

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    assert rc == 0
    output = capsys.readouterr().out
    assert "upstream=changed" in output
    assert "update changed upstream: model-mirror repair --update org/model" in output
    state = read_verification_state(archive)
    assert state.resolved_commit == "oldcommit"
    assert state.upstream_commit == "newcommit"


def test_verify_failure_reports_changed_upstream(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
        ),
    )
    hub = MovingBranchHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "verification failed: org/model upstream=changed" in output
    assert "next: model-mirror repair org/model" in output
    assert "update changed upstream: model-mirror repair --update org/model" in output


def test_print_verification_next_steps_can_show_only_update_hint(capsys):
    cli_module.print_verification_next_steps(
        "org/model",
        VerificationState(status="dirty", repo_id="org/model", upstream_status="changed"),
    )

    output = capsys.readouterr().out
    assert "next: model-mirror repair org/model" not in output
    assert "update changed upstream: model-mirror repair --update org/model" in output


def test_verify_command_reports_failure(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    assert rc == 1
    output = capsys.readouterr().out
    assert "verification failed" in output
    assert "checked: files=0 hashes=0" in output
    assert "missing (1):\n  missing.bin" in output
    assert "next: model-mirror repair org/model" in output


def test_verify_treats_model_formats_as_opaque_bytes(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "gguf"
    archive.mkdir(parents=True)
    (archive / "config.json").write_bytes(b"not json")
    hub = FakeHub([FakeFile("config.json", 8)])

    rc = main(["--config", str(config_path), "verify", "--cached", "org/gguf"], hub=hub)

    assert rc == 0
    assert "verified (cached): org/gguf" in capsys.readouterr().out
    assert read_verification_state(archive).status == "clean"


def test_verification_detail_caps_long_non_model_issue_lists(capsys):
    result = RemoteVerifyResult(missing=[f"missing-{index}.bin" for index in range(21)])

    cli_module.print_verification_details("org/data", result)

    output = capsys.readouterr().out
    assert "missing (21):" in output
    assert "missing-19.bin" in output
    assert "missing-20.bin" not in output
    assert "... 1 more; run model-mirror status --verbose org/data" in output


def test_verify_cached_reports_incomplete_when_manifest_hash_is_missing(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3, lfs_sha256=digest)])

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    output = capsys.readouterr().out
    state = read_verification_state(archive)
    assert rc == 1
    assert "cached verification incomplete: org/model" in output
    assert "run full verification: model-mirror verify org/model" in output
    assert "next: model-mirror repair org/model" not in output
    assert state.status == "incomplete"
    assert state.repair_paths == []


def test_verify_cached_incomplete_reports_changed_upstream_hint(tmp_path, capsys):
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
    hub = MovingBranchHub(
        [FakeFile("config.json", 2), FakeFile("file.bin", 3, lfs_sha256=hashlib.sha256(b"abc").hexdigest())]
    )

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "cached verification incomplete: org/model upstream=changed" in output
    assert "update changed upstream: model-mirror repair --update org/model" in output


def test_verify_dataset_uses_same_opaque_payload_contract(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "datasets" / "org" / "data"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(
        ["--config", str(config_path), "verify", "--repo-type", "dataset", "--cached", "org/data"],
        hub=hub,
    )

    assert rc == 0
    assert "verified (cached)" in capsys.readouterr().out


def test_verify_all_checks_every_model(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    for repo in ("one/model", "two/model"):
        archive = tmp_path / "models" / repo
        archive.mkdir(parents=True)
        (archive / "config.json").write_text("{}", encoding="utf-8")
        (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--cached", "--all"], hub=hub)

    assert rc == 0
    assert capsys.readouterr().out.count("verified (cached)") == 2


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

    rc = main(["--config", str(config_path), "verify", "--cached", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "verified (cached): ok/model" in output
    assert "verification failed: bad/model" in output
    assert "next: model-mirror repair --all" in output


def test_verify_all_reports_cached_incomplete_without_repair_hint(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("file.bin", 3, lfs_sha256=hashlib.sha256(b"abc").hexdigest())])

    rc = main(["--config", str(config_path), "verify", "--cached", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "cached verification incomplete: org/model" in output
    assert "cached verification incomplete: run full verification with model-mirror verify --all" in output
    assert "next: model-mirror repair --all" not in output


def test_verify_all_reports_changed_upstream_update_hint(tmp_path, capsys):
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

    rc = main(["--config", str(config_path), "verify", "--cached", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 0
    assert "verified (cached): org/model upstream=changed" in output
    assert "update changed upstreams: model-mirror repair --all --update" in output


def test_verify_all_skips_busy_model_and_continues(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    busy_archive = tmp_path / "models" / "busy" / "model"
    busy_archive.mkdir(parents=True)
    ok_archive = tmp_path / "models" / "ok" / "model"
    ok_archive.mkdir(parents=True)
    (ok_archive / "config.json").write_text("{}", encoding="utf-8")
    (ok_archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3)])

    with ModelLock(busy_archive, "mirror", "busy/model"):
        rc = main(["--config", str(config_path), "verify", "--cached", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "skipped busy: busy/model" in output
    assert "command=mirror" in output
    assert "verified (cached): ok/model" in output


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
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            checked_at_utc=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds"),
        ),
    )
    write_snapshot_plan(
        archive,
        HubSnapshot("org/model", "model", "main", "a" * 40, [HubFile("config.json", 2)]),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert f"archive: {tmp_path}  mirrors=1  recorded_payload=2 B" in output
    assert "TYPE   REPOSITORY" in output
    assert "model  org/model" in output
    assert "2 B" in output
    assert "3h" in output
    assert "clean" not in output


def test_list_command_uses_recorded_payload_and_reports_stale_mirror_cache(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"xx")
    cache_blob = archive / ".cache" / "huggingface" / "download"
    cache_blob.parent.mkdir(parents=True)
    cache_blob.write_bytes(b"x" * 100)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))
    write_snapshot_plan(
        archive,
        HubSnapshot("org/model", "model", "main", "a" * 40, [HubFile("file.bin", 2)]),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "model  org/model" in output
    assert "2 B" in output
    assert "stale-cache" in output
    assert "legacy-mirror-cache model:org/model" in output
    assert "model-mirror clean-cache --force" in output


def test_status_reports_untracked_archive_cache_without_sizing_it(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    (tmp_path / ".model-mirror" / "cache" / "hub").mkdir(parents=True)
    (tmp_path / ".model-mirror" / "cache" / "hub" / "blob").write_bytes(b"x" * 5)
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "status"])

    assert rc == 0
    output = capsys.readouterr().out
    assert f"archive: {tmp_path}  mirrors=0  recorded_payload=0 B" in output
    assert "untracked-cache: runtime-cache-without-active-operation" in output
    assert "model-mirror clean-cache --force" in output
    assert "5 B" not in output


def test_cache_issue_selection_filters_repository_and_type_but_keeps_archive_issues(tmp_path):
    global_issue = CacheIssue("untracked", "global", "cache", tmp_path)
    model_issue = CacheIssue(
        "stale",
        "model",
        "cache",
        tmp_path,
        repo_id="org/model",
        repo_type="model",
    )
    dataset_issue = CacheIssue(
        "stale",
        "dataset",
        "cache",
        tmp_path,
        repo_id="org/data",
        repo_type="dataset",
    )

    assert cli_module.select_cache_issues(
        [global_issue, model_issue, dataset_issue],
        repo_id="org/model",
        repo_type="model",
    ) == [global_issue, model_issue]
    assert cli_module.select_cache_issues(
        [global_issue, model_issue, dataset_issue],
        repo_type="dataset",
    ) == [global_issue, dataset_issue]


def test_status_reports_interrupted_download_with_resume_and_discard_commands(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path)
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    commit = "a" * 40
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", commit, [HubFile("file.bin", 3)]),
    )
    write_verification_state(
        root,
        VerificationState(
            status="in_progress",
            repo_id="org/model",
            repo_type="model",
            requested_revision="main",
            resolved_commit=commit,
        ),
    )
    staging = download_staging_dir(config, "org/model", "model", commit, None)
    write_download_record(
        staging,
        repo_id="org/model",
        repo_type="model",
        requested_revision="main",
        resolved_commit=commit,
        destination=root,
        allow_patterns=None,
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "status", "org/model"]) == 0
    output = capsys.readouterr().out

    assert "Attention     in-progress, stale-cache" in output
    assert "stale-cache: interrupted-download model:org/model target=aaaaaaaaaaaa" in output
    assert "resume: model-mirror mirror --repo-type model --revision main org/model" in output
    assert "remove: model-mirror clean-cache --force" in output

    assert main(["--config", str(config_path), "status", "--json", "org/model"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["cache"][0]["status"] == "stale"
    assert document["cache"][0]["reason"] == "interrupted-download"
    assert document["repositories"][0]["cache"] == document["cache"]
    assert "stale-cache" in document["repositories"][0]["exceptions"]


def test_status_omits_cache_output_for_active_download(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path)
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    commit = "b" * 40
    write_verification_state(
        root,
        VerificationState(
            status="in_progress",
            repo_id="org/model",
            resolved_commit=commit,
        ),
    )
    staging = download_staging_dir(config, "org/model", "model", commit, None)
    write_download_record(
        staging,
        repo_id="org/model",
        repo_type="model",
        requested_revision="main",
        resolved_commit=commit,
        destination=root,
        allow_patterns=None,
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    with ModelLock(root, "mirror", "org/model"):
        assert main(["--config", str(config_path), "status", "org/model"]) == 0
        output = capsys.readouterr().out
        assert "Cache attention" not in output
        assert "stale-cache" not in output

        assert main(["--config", str(config_path), "clean-cache", "--force"]) == 1
        output = capsys.readouterr().out
        assert "cleanup blocked by active repository: model:org/model" in output
        assert staging.exists()


def test_clean_cache_force_refuses_runtime_cache_in_use(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    cache = tmp_path / ".model-mirror" / "cache"
    cache.mkdir(parents=True)
    (cache / "blob").write_bytes(b"x")

    with RuntimeCacheLock(Config(directory=tmp_path), exclusive=False):
        assert main(["--config", str(config_path), "clean-cache", "--force"]) == 1

    output = capsys.readouterr().out
    assert "cleanup blocked: runtime cache is in use by an active operation" in output
    assert cache.exists()


def test_status_and_list_repo_print_detailed_last_known_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    (root / "file.bin").write_bytes(b"abc")
    commit = "a" * 40
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", commit, [HubFile("file.bin", 3)]),
    )
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit=commit,
            upstream_commit=commit,
            upstream_status="current",
            checked_at_utc="2026-01-01T00:00:00+00:00",
        ),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "status", "org/model"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("org/model\n")
    assert "Verification  clean" in output
    assert "Commit        aaaaaaaaaaaa · main" in output
    assert "Payload       1 file · 3 B" in output
    assert "Upstream      current when last checked" in output
    assert "Torrent       not published" in output
    assert "resolved_commit:" not in output

    assert main(["--config", str(config_path), "status", "--verbose", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "repository: org/model" in output
    assert "repo_type: model" in output
    assert f"path: {root}" in output
    assert "status: clean" in output
    assert "exceptions: none" in output
    assert "last_checked: 2026-01-01T00:00:00+00:00" in output
    assert "requested_revision: main" in output
    assert f"resolved_commit: {commit}" in output
    assert f"snapshot_commit: {commit}" in output
    assert f"upstream_commit: {commit}" in output
    assert "upstream_status: current" in output
    assert "offline_only: false" in output
    assert "payload_files: 1" in output
    assert "payload_size: 3 B (3 bytes)" in output
    assert "payload_source: snapshot" in output
    assert "expected_files: 1 recorded" in output
    assert "lock: none" in output
    assert "progress: none" in output
    assert "torrent: none" in output
    assert "repair_paths: none" in output
    assert "issues: none" in output

    assert main(["--config", str(config_path), "list", "org/model"]) == 0
    assert capsys.readouterr().out.startswith("org/model\n")


def test_status_json_is_complete_and_verbose_is_a_noop(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    commit = "a" * 40
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", commit, [HubFile("file.bin", 3)]),
    )
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id="org/model",
            resolved_commit=commit,
            upstream_commit=commit,
            upstream_status="current",
            checked_at_utc="2026-01-01T00:00:00+00:00",
        ),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "status", "--json", "--verbose", "org/model"]) == 0
    document = json.loads(capsys.readouterr().out)

    assert document["schema"] == "model-mirror-status"
    assert document["version"] == 1
    assert document["errors"] == []
    assert document["cache"] == []
    assert len(document["repositories"]) == 1
    record = document["repositories"][0]
    assert record["repo_id"] == "org/model"
    assert record["resolved_commit"] == commit
    assert record["last_verification"]["status"] == "clean"
    assert record["payload"] == {"bytes": 3, "files": 1, "source": "snapshot"}
    assert record["snapshot"] == {
        "commit": commit,
        "expected_files": 1,
        "stale": False,
    }
    assert record["upstream"]["recorded"] == {"commit": commit, "status": "current"}
    assert record["upstream"]["live"] is None
    assert record["cache"] == []


def test_status_check_upstream_is_live_and_does_not_write_metadata(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    old_commit = "oldcommit"
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", old_commit, [HubFile("file.bin", 3)]),
    )
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit=old_commit,
            upstream_commit=old_commit,
            upstream_status="current",
            checked_at_utc="2026-01-01T00:00:00+00:00",
        ),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    state_path = root / ".verification"
    snapshot_path = root / ".model-mirror" / "snapshot.json"
    before = {
        state_path: (state_path.read_bytes(), state_path.stat().st_mtime_ns),
        snapshot_path: (snapshot_path.read_bytes(), snapshot_path.stat().st_mtime_ns),
    }

    hub = MovingBranchHub([FakeFile("file.bin", 3)])
    assert main(
        ["--config", str(config_path), "status", "--check-upstream", "--json", "org/model"],
        hub=hub,
    ) == 0
    document = json.loads(capsys.readouterr().out)

    live = document["repositories"][0]["upstream"]["live"]
    assert live["status"] == "changed"
    assert live["commit"] == "newcommit"
    assert live["observed_at"]
    assert live["error"] is None
    for path, (contents, modified) in before.items():
        assert path.read_bytes() == contents
        assert path.stat().st_mtime_ns == modified


def test_status_archive_live_table_and_json_modes(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", "oldcommit", [HubFile("file.bin", 3)]),
    )
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
        ),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = MovingBranchHub([FakeFile("file.bin", 3)])

    assert main(["--config", str(config_path), "status", "--check-upstream"], hub=hub) == 0
    output = capsys.readouterr().out
    assert "UPSTREAM NOW" in output
    assert "changed:newcommit" in output

    assert main(["--config", str(config_path), "list", "--check-upstream", "--json"], hub=hub) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["repositories"][0]["upstream"]["live"]["status"] == "changed"


def test_status_live_upstream_available_current_and_unavailable_human_output(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    hub = MovingBranchHub([FakeFile("file.bin", 3)])
    assert main(
        ["--config", str(config_path), "status", "--check-upstream", "org/model"],
        hub=hub,
    ) == 0
    assert "Upstream      available now · newcommit" in capsys.readouterr().out

    write_verification_state(
        root,
        VerificationState(status="clean", repo_id="org/model", resolved_commit="newcommit"),
    )
    assert main(
        ["--config", str(config_path), "status", "--check-upstream", "org/model"],
        hub=hub,
    ) == 0
    assert "Upstream      current now · newcommit" in capsys.readouterr().out

    assert main(
        [
            "--config",
            str(config_path),
            "status",
            "--check-upstream",
            "--verbose",
            "org/model",
        ],
        hub=hub,
    ) == 0
    assert "live_upstream: current commit=newcommit" in capsys.readouterr().out

    assert main(
        ["--config", str(config_path), "status", "--check-upstream", "org/model"],
        hub=UnavailableHub(),
    ) == 0
    output = capsys.readouterr().out
    assert "Upstream      unavailable now · repository not found" in output

    assert main(
        [
            "--config",
            str(config_path),
            "status",
            "--check-upstream",
            "--verbose",
            "org/model",
        ],
        hub=UnavailableHub(),
    ) == 0
    output = capsys.readouterr().out
    assert "live_upstream: unavailable commit=unknown" in output
    assert "error=repository not found" in output


def test_status_never_uses_recursive_filesystem_scans(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", "a" * 40, [HubFile("file.bin", 3)]),
    )
    write_verification_state(root, VerificationState(status="clean", repo_id="org/model"))
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    def fail_rglob(*_args, **_kwargs):
        raise AssertionError("status must not recursively scan the archive")

    monkeypatch.setattr(Path, "rglob", fail_rglob)

    assert main(["--config", str(config_path), "status"]) == 0
    assert "org/model" in capsys.readouterr().out


def test_status_json_reports_missing_repository_as_structured_error(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "status", "--json", "org/missing"]) == 1
    document = json.loads(capsys.readouterr().out)

    assert document["repositories"] == []
    assert document["errors"][0]["code"] == "mirror-not-found"
    assert document["errors"][0]["repo_id"] == "org/missing"


def test_status_repo_reports_unverified_and_missing_repositories(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "unverified"
    root.mkdir(parents=True)
    (root / "file.bin").write_bytes(b"x")
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "status", "org/unverified"]) == 0
    output = capsys.readouterr().out
    assert "Verification  unverified" in output
    assert "Commit        unknown · unknown" in output
    assert "Payload       unknown" in output
    assert "Attention     unverified" in output
    assert "Next          model-mirror verify org/unverified" in output

    assert main(["--config", str(config_path), "status", "--verbose", "org/unverified"]) == 0
    verbose = capsys.readouterr().out
    assert "payload_size: unknown" in verbose
    assert "expected_files: unknown" in verbose

    assert main(["--config", str(config_path), "status", "org/missing"]) == 1
    assert "mirror not found: model:org/missing" in capsys.readouterr().out


def test_status_recommends_update_or_verify_from_recorded_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    write_verification_state(
        root,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            upstream_status="changed",
        ),
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "status", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "model-mirror repair --update org/model" in output

    write_verification_state(
        root,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            issues=["verification skipped"],
            upstream_status="changed",
        ),
    )
    assert main(["--config", str(config_path), "status", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "model-mirror verify org/model" in output
    assert "repair --update" not in output


def test_status_repo_surfaces_stale_snapshot_issues_lock_and_progress(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    root = tmp_path / "models" / "org" / "model"
    root.mkdir(parents=True)
    (root / "old.bin").write_bytes(b"x")
    (root / "extra.bin").write_bytes(b"y")
    old_commit = "a" * 40
    new_commit = "b" * 40
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", old_commit, [HubFile("old.bin", 1)]),
    )
    write_verification_state(
        root,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit=new_commit,
            upstream_commit=new_commit,
            upstream_status="current",
            repair_paths=["bad.bin"],
            issues=["missing: bad.bin"],
        ),
    )
    progress_file = progress_path(root)
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    progress_file.write_text(
        """{
  "schema": "model-mirror-progress",
  "version": 1,
  "active_files": {
    "bad.bin": {
      "path": "bad.bin",
      "stage": "downloading",
      "bytes_done": 1,
      "bytes_total": 2,
      "updated_at_utc": "2000-01-01T00:00:00+00:00",
      "rate_bytes_per_second": 1
    }
  }
}
""",
        encoding="utf-8",
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    with ModelLock(root, "repair", "org/model"):
        assert main(["--config", str(config_path), "status", "org/model"]) == 0
        detail = capsys.readouterr().out
        assert "Attention     needs-repair, busy, stalled, snapshot-stale" in detail
        assert "Commit        bbbbbbbbbbbb · main" in detail
        assert "Payload       unknown" in detail
        assert "Lock          command=repair" in detail
        assert "Activity      active=1 path=bad.bin" in detail
        assert "Repair paths\n  bad.bin" in detail
        assert "Issues\n  missing: bad.bin" in detail

        assert main(["--config", str(config_path), "list"]) == 0
        table = capsys.readouterr().out
        row = next(line for line in table.splitlines() if "org/model" in line)
        assert "snapshot-stale" in row
        assert "needs-repair,busy,stalled" in row
        assert " ? " in row

        assert main(["--config", str(config_path), "status", "--json", "org/model"]) == 0
        document = json.loads(capsys.readouterr().out)
        activity = document["repositories"][0]["activity"]
        assert activity[0]["path"] == "bad.bin"
        assert activity[0]["bytes_done"] == 1
        assert activity[0]["stalled"] is True


def test_list_repo_type_filters_table_and_selects_dataset_detail(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    model = tmp_path / "models" / "org" / "model"
    dataset = tmp_path / "datasets" / "org" / "data"
    model.mkdir(parents=True)
    dataset.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"m")
    (dataset / "data.bin").write_bytes(b"d")
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    assert main(["--config", str(config_path), "list", "--repo-type", "dataset"]) == 0
    output = capsys.readouterr().out
    assert "dataset  org/data" in output
    assert "org/model" not in output

    assert main(
        ["--config", str(config_path), "status", "--repo-type", "dataset", "org/data"]
    ) == 0
    detail = capsys.readouterr().out
    assert detail.startswith("org/data\n")


def test_clean_cache_dry_run_reports_reclaimable_space_without_deleting(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    control = tmp_path / ".model-mirror"
    (control / "cache" / "hub").mkdir(parents=True)
    (control / "cache" / "hub" / "blob").write_bytes(b"x" * 5)
    (control / "tmp").mkdir()
    (control / "tmp" / "partial").write_bytes(b"x" * 7)
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "legacy").write_bytes(b"x")
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "legacy").write_bytes(b"xx")
    mirror = tmp_path / "models" / "org" / "model"
    mirror.mkdir(parents=True)
    (mirror / "file.bin").write_bytes(b"xx")
    (mirror / ".cache" / "huggingface").mkdir(parents=True)
    (mirror / ".cache" / "huggingface" / "meta").write_bytes(b"x" * 3)
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "clean-cache"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "cleanup mode: dry-run" in output
    assert "reclaimable=18 B" in output
    assert "would-remove: archive-cache" in output
    assert "would-remove: archive-tmp" in output
    assert "would-remove: legacy-archive-cache" in output
    assert "would-remove: legacy-archive-tmp" in output
    assert "would-remove: mirror-cache" in output
    assert (control / "cache").exists()
    assert (control / "tmp").exists()
    assert (tmp_path / ".cache").exists()
    assert (tmp_path / ".tmp").exists()
    assert (mirror / ".cache").exists()
    assert (mirror / "file.bin").exists()


def test_clean_cache_force_removes_cache_and_tmp_but_keeps_payload(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    control = tmp_path / ".model-mirror"
    (control / "cache" / "hub").mkdir(parents=True)
    (control / "cache" / "hub" / "blob").write_bytes(b"x" * 5)
    (control / "tmp").mkdir()
    (control / "tmp" / "partial").write_bytes(b"x" * 7)
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "legacy").write_bytes(b"x")
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "legacy").write_bytes(b"xx")
    mirror = tmp_path / "models" / "org" / "model"
    mirror.mkdir(parents=True)
    (mirror / "file.bin").write_bytes(b"xx")
    (mirror / ".cache" / "huggingface").mkdir(parents=True)
    (mirror / ".cache" / "huggingface" / "meta").write_bytes(b"x" * 3)
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "clean-cache", "--force"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "cleanup mode: force" in output
    assert "removed: archive-cache" in output
    assert "removed: archive-tmp" in output
    assert "removed: legacy-archive-cache" in output
    assert "removed: legacy-archive-tmp" in output
    assert "removed: mirror-cache" in output
    assert control.exists()
    assert not (control / "cache").exists()
    assert not (control / "tmp").exists()
    assert not (tmp_path / ".cache").exists()
    assert not (tmp_path / ".tmp").exists()
    assert not (mirror / ".cache").exists()
    assert (mirror / "file.bin").read_bytes() == b"xx"


def test_clean_cache_refuses_unsafe_configured_targets(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    outside_tmp = tmp_path.parent / f"{tmp_path.name}-outside" / ".tmp"
    (tmp_path / "payload.bin").write_bytes(b"xx")
    config_path.write_text(
        yaml.safe_dump(
            {
                "directory": str(tmp_path),
                "cache_dir": str(tmp_path),
                "tmp_dir": str(outside_tmp),
            }
        ),
        encoding="utf-8",
    )

    rc = main(["--config", str(config_path), "clean-cache", "--force"])

    assert rc == 1
    output = capsys.readouterr().out
    assert f"refusing unsafe cleanup target: {tmp_path}" in output
    assert f"refusing unsafe cleanup target: {outside_tmp}" in output
    assert (tmp_path / "payload.bin").exists()


def test_directory_size_returns_zero_for_missing_path(tmp_path):
    assert cli_module.directory_size(tmp_path / "missing") == 0


def test_archive_cache_usage_supports_default_and_configured_paths(tmp_path):
    control = tmp_path / ".model-mirror"
    (control / "cache").mkdir(parents=True)
    (control / "cache" / "one").write_bytes(b"1")
    (control / "tmp").mkdir()
    (control / "tmp" / "two").write_bytes(b"22")
    root = tmp_path / "models" / "org" / "model" / ".cache"
    root.mkdir(parents=True)
    (root / "three").write_bytes(b"333")

    usage = cli_module.archive_cache_usage(Config(directory=tmp_path))
    assert usage.total == 6

    configured_cache = tmp_path / "custom" / ".cache"
    configured_tmp = tmp_path / "custom" / ".tmp"
    configured_cache.mkdir(parents=True)
    configured_tmp.mkdir(parents=True)
    (configured_cache / "four").write_bytes(b"4444")
    (configured_tmp / "five").write_bytes(b"55555")
    configured = cli_module.archive_cache_usage(
        Config(directory=tmp_path, cache_dir=configured_cache, tmp_dir=configured_tmp)
    )
    assert configured.archive_cache == 4
    assert configured.archive_tmp == 5
    assert configured.mirror_cache == 3


def test_cleanup_targets_deduplicates_explicit_legacy_paths(tmp_path):
    targets = cli_module.cleanup_targets(
        Config(
            directory=tmp_path,
            cache_dir=tmp_path / ".cache",
            tmp_dir=tmp_path / ".tmp",
        )
    )

    assert targets.count((tmp_path / ".cache", "archive-cache")) == 1
    assert targets.count((tmp_path / ".tmp", "archive-tmp")) == 1
    assert all(not label.startswith("legacy-") for _path, label in targets)


def test_list_command_prints_busy_lock(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    with ModelLock(archive, "mirror", "org/model"):
        rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "model  org/model" in output
    assert "busy" in output
    assert "clean" not in output
    assert "command=mirror" not in output


def test_list_command_prints_stalled_progress(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="in_progress", repo_id="org/model"))
    path = progress_path(archive)
    path.parent.mkdir(parents=True)
    path.write_text(
        """{
  "schema": "model-mirror-progress",
  "version": 1,
  "active_files": {
    "file.bin": {
      "path": "file.bin",
      "stage": "downloading",
      "bytes_done": 5,
      "bytes_total": 10,
      "updated_at_utc": "2000-01-01T00:00:00+00:00",
      "rate_bytes_per_second": 2
    }
  }
}
""",
        encoding="utf-8",
    )
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path), "stall_timeout_seconds": 600}), encoding="utf-8")

    with ModelLock(archive, "mirror", "org/model"):
        rc = main(["--config", str(config_path), "list"])

    assert rc == 0
    output = capsys.readouterr().out
    assert "in-progress,busy,stalled" in output
    assert "ACTIVITY" in output
    assert "active=1 path=file.bin stage=downloading bytes=5 B/10 B(50.0%) rate=2 B/s" in output
    assert "stalled=1" in output


def test_list_state_tags_cover_multi_tag_states():
    tags = cli_module.list_state_tags(
        VerificationState(
            status="dirty",
            repo_id="org/model",
            offline_only=True,
            repair_paths=["bad.bin"],
            upstream_status="changed",
        ),
        {"command": "verify"},
    )
    unavailable_tags = cli_module.list_state_tags(
        VerificationState(status="unavailable", repo_id="org/model"),
        None,
    )
    incomplete_tags = cli_module.list_state_tags(
        VerificationState(status="incomplete", repo_id="org/model"),
        None,
    )
    manifest_incomplete_tags = cli_module.list_state_tags(
        VerificationState(status="incomplete", repo_id="org/model", issues=["cached_hash_missing: file.bin"]),
        None,
    )
    stalled = ProgressSnapshot(
        entries=[
            ProgressEntry(
                "file.bin",
                "downloading",
                1,
                2,
                "",
                1,
                True,
                "heartbeat",
            )
        ],
        source="heartbeat",
    )

    assert tags == ["needs-repair", "offline", "upstream-changed", "busy"]
    assert unavailable_tags == ["upstream-unavailable"]
    assert incomplete_tags == ["incomplete"]
    assert manifest_incomplete_tags == ["manifest-incomplete"]
    assert cli_module.list_exception_tags(None, {"command": "mirror"}, stalled) == [
        "unverified",
        "busy",
        "stalled",
    ]
    assert cli_module.list_exception_tags(
        VerificationState(status="clean", repo_id="org/model"),
        None,
    ) == []


def test_list_format_helpers_cover_display_branches(capsys):
    assert cli_module.format_bytes(0) == "0 B"
    assert cli_module.format_bytes(1536) == "1.5 KiB"
    assert cli_module.format_bytes(1024**5 * 2) == "2.0 PiB"

    assert cli_module.format_lock_detail(None) == "lock held"
    assert cli_module.format_lock_detail({"command": "", "pid": None}) == "lock held"
    assert cli_module.format_lock_detail({"command": "verify", "pid": 123}) == "command=verify pid=123"

    assert cli_module.primary_state_tag(
        VerificationState(status="dirty", repo_id="org/model", issues=["verification skipped"])
    ) == "unverified"
    assert cli_module.primary_state_tag(VerificationState(status="dirty", repo_id="org/model")) == "dirty"
    assert cli_module.primary_state_tag(VerificationState(status="in_progress", repo_id="org/model")) == "in-progress"
    assert cli_module.primary_state_tag(VerificationState(status="", repo_id="org/model")) == "unknown"

    assert cli_module.parse_nonnegative_int_arg("0") == 0
    with pytest.raises(argparse.ArgumentTypeError):
        cli_module.parse_nonnegative_int_arg("-1")

    assert cli_module.format_progress_detail(ProgressSnapshot(entries=[], source="heartbeat")) is None
    assert cli_module.format_upstream_observation(None) == "-"
    assert cli_module.format_upstream_observation(
        cli_module.UpstreamObservation("unavailable", None, "2026-01-01T00:00:00+00:00")
    ) == "unavailable"
    long_path = "nested/" + ("x" * 80) + ".bin"
    detail = cli_module.format_progress_detail(
        ProgressSnapshot(
            entries=[
                ProgressEntry(
                    path=long_path,
                    stage="partial",
                    bytes_done=5,
                    bytes_total=None,
                    updated_at_utc="",
                    idle_seconds=None,
                    stalled=False,
                    source="partial-file",
                    rate_bytes_per_second=None,
                )
            ],
            source="partial-file",
        )
    )
    assert "bytes=5 B" in detail
    assert "source=partial-file" in detail
    assert "rate=" not in detail
    assert "path=..." in detail

    selected = cli_module.selected_progress_entry(
        [
            ProgressEntry("b.bin", "downloading", 1, 2, "", 0, False, "heartbeat"),
            ProgressEntry("a.bin", "downloading", 1, 2, "", 0, False, "heartbeat"),
        ]
    )
    assert selected.path == "a.bin"

    cli_module.print_detail_values("values", ["one", "two"])
    assert capsys.readouterr().out == "values:\n  - one\n  - two\n"


class FakeChild:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def stalled_snapshot(path="file.bin", idle_seconds=601, updated_at_utc=None):
    return ProgressSnapshot(
        entries=[
            ProgressEntry(
                path=path,
                stage="downloading",
                bytes_done=1,
                bytes_total=2,
                updated_at_utc=updated_at_utc or datetime.now(timezone.utc).isoformat(),
                idle_seconds=idle_seconds,
                stalled=True,
                source="heartbeat",
            )
        ],
        source="heartbeat",
    )


def test_supervise_child_restarts_on_stalled_progress(tmp_path, monkeypatch, capsys):
    child = FakeChild()
    monkeypatch.setattr(cli_module, "progress_snapshot", lambda root, stall_timeout_seconds: stalled_snapshot())

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 3, {})

    output = capsys.readouterr().out
    assert restart is True
    assert rc == 0
    assert child.terminated is True
    assert "stall detected for file.bin" in output


def test_supervise_child_fails_when_stall_retry_limit_is_exceeded(tmp_path, monkeypatch, capsys):
    child = FakeChild()
    monkeypatch.setattr(cli_module, "progress_snapshot", lambda root, stall_timeout_seconds: stalled_snapshot())

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 0, {})

    output = capsys.readouterr().out
    assert restart is False
    assert rc == 1
    assert child.terminated is True
    assert "stall retry limit exceeded for file.bin" in output


def test_supervise_child_waits_for_fresh_progress_when_existing_status_is_stale(tmp_path, monkeypatch, capsys):
    child = FakeChild()
    times = iter([0.0, 601.0])
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        cli_module,
        "progress_snapshot",
        lambda root, stall_timeout_seconds: stalled_snapshot(updated_at_utc="2000-01-01T00:00:00+00:00"),
    )

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 3, {})

    output = capsys.readouterr().out
    assert restart is True
    assert rc == 0
    assert child.terminated is True
    assert "stall detected before progress was reported" in output


def test_fresh_progress_entries_handles_invalid_and_naive_timestamps():
    cutoff = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    entries = [
        ProgressEntry("invalid.bin", "downloading", 0, 1, "not-a-date", 0, False, "heartbeat"),
        ProgressEntry("old.bin", "downloading", 0, 1, "2025-12-31T23:59:59", 0, False, "heartbeat"),
        ProgressEntry("fresh.bin", "downloading", 0, 1, "2026-01-01T00:00:00", 0, False, "heartbeat"),
    ]

    fresh = cli_module.fresh_progress_entries(entries, cutoff)

    assert [entry.path for entry in fresh] == ["fresh.bin"]


def test_supervise_child_restarts_when_no_progress_is_reported(tmp_path, monkeypatch, capsys):
    child = FakeChild()
    times = iter([0.0, 601.0])
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli_module, "progress_snapshot", lambda root, stall_timeout_seconds: ProgressSnapshot([], "heartbeat"))

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 3, {})

    output = capsys.readouterr().out
    assert restart is True
    assert rc == 0
    assert child.terminated is True
    assert "stall detected before progress was reported" in output


def test_supervise_child_fails_when_no_progress_retry_limit_is_exceeded(tmp_path, monkeypatch, capsys):
    child = FakeChild()
    times = iter([0.0, 601.0])
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli_module, "progress_snapshot", lambda root, stall_timeout_seconds: ProgressSnapshot([], "heartbeat"))

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 0, {})

    output = capsys.readouterr().out
    assert restart is False
    assert rc == 1
    assert child.terminated is True
    assert "stall retry limit exceeded before progress was reported" in output


def test_supervise_child_sleeps_when_child_is_active_without_stall(tmp_path, monkeypatch):
    child = FakeChild()
    polls = iter([None, 0])
    sleeps = []
    child.poll = lambda: next(polls)
    monkeypatch.setattr(cli_module, "progress_snapshot", lambda root, stall_timeout_seconds: ProgressSnapshot([], "heartbeat"))
    monkeypatch.setattr(cli_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(cli_module.time, "sleep", sleeps.append)

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 3, {})

    assert restart is False
    assert rc == 0
    assert sleeps == [30.0]


def test_supervise_child_returns_child_exit_code(tmp_path):
    child = FakeChild(returncode=2)

    restart, rc = cli_module.supervise_child(child, tmp_path, 600, 3, {})

    assert restart is False
    assert rc == 2


def test_terminate_child_kills_after_timeout(monkeypatch):
    child = FakeChild()

    def raise_timeout(timeout=None):
        if not child.killed:
            raise cli_module.subprocess.TimeoutExpired("cmd", timeout)
        return child.returncode

    child.wait = raise_timeout

    cli_module.terminate_child(child)

    assert child.terminated is True
    assert child.killed is True


def test_run_supervised_mirror_spawns_child_with_env(tmp_path, monkeypatch):
    spawned = {}
    args = SimpleNamespace(model="org/model", repo_type=None, stall_timeout=None)
    config = Config(directory=tmp_path, stall_timeout_seconds=600)

    class FakePopenChild(FakeChild):
        def __init__(self, command, env):
            super().__init__(returncode=0)
            spawned["command"] = command
            spawned["env"] = env

    monkeypatch.setattr(cli_module.subprocess, "Popen", FakePopenChild)
    monkeypatch.setattr(cli_module, "supervise_child", lambda child, root, timeout, retry_limit, retry_counts: (False, 0))

    rc = cli_module.run_supervised_mirror(["mirror", "org/model"], args, config)

    assert rc == 0
    assert spawned["command"][-2:] == ["mirror", "org/model"]
    assert spawned["env"]["MODEL_MIRROR_SUPERVISED_CHILD"] == "1"


def test_run_supervised_mirror_restarts_child(tmp_path, monkeypatch):
    calls = []
    args = SimpleNamespace(model="org/model", repo_type=None, stall_timeout=None)
    config = Config(directory=tmp_path, stall_timeout_seconds=600)

    class FakePopenChild(FakeChild):
        def __init__(self, command, env):
            super().__init__(returncode=0)
            calls.append(command)

    supervise_results = iter([(True, 0), (False, 0)])
    monkeypatch.setattr(cli_module.subprocess, "Popen", FakePopenChild)
    monkeypatch.setattr(
        cli_module,
        "supervise_child",
        lambda child, root, timeout, retry_limit, retry_counts: next(supervise_results),
    )

    rc = cli_module.run_supervised_mirror(["mirror", "org/model"], args, config)

    assert rc == 0
    assert len(calls) == 2


def test_verify_writes_dirty_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)

    assert rc == 1
    state = read_verification_state(tmp_path / "models" / "org" / "model")
    assert state.status == "dirty"
    assert "missing.bin" in state.repair_paths


def test_verify_then_repair_repairs_dirty_archive(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    verify_rc = main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=hub)
    repair_rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    assert verify_rc == 1
    assert repair_rc == 0
    assert (archive / "missing.bin").read_bytes() == b"xxx"
    assert "repaired: org/model" in capsys.readouterr().out
    assert read_verification_state(archive).status == "clean"


def test_verify_rejects_repair_option(tmp_path):
    config_path = tmp_path / "config.yaml"

    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "verify", "--repair", "org/model"])


def test_full_verify_detects_tampering_when_checksums_already_exist(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    write_checksums(archive)
    (archive / "file.bin").write_bytes(b"abd")
    hub = FakeHub([FakeFile("file.bin", 3, lfs_sha256=hashlib.sha256(b"abc").hexdigest())])

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=hub)

    assert rc == 1
    output = capsys.readouterr().out
    assert "verification failed" in output
    assert "hash mismatch (1):\n  file.bin" in output
    state = read_verification_state(archive)
    assert state.status == "dirty"
    assert state.repair_paths == ["file.bin"]


def test_full_verify_refreshes_when_existing_checksums_are_clean(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    payload = b"abc"
    (archive / "file.bin").write_bytes(payload)
    write_checksums(archive)
    hub = FakeHub([FakeFile("file.bin", 3, lfs_sha256=hashlib.sha256(payload).hexdigest())])

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=hub)

    assert rc == 0
    assert "verified (full)" in capsys.readouterr().out


def test_verify_then_repair_hashes_unchanged_files_once(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "good.bin").write_bytes(b"good")
    (archive / "bad.bin").write_bytes(b"bad!")
    hub = FakeHub(
        [
            FakeFile("config.json", 2),
            FakeFile("good.bin", 4, lfs_sha256=hashlib.sha256(b"good").hexdigest()),
            FakeFile("bad.bin", 4, lfs_sha256=hashlib.sha256(b"xxxx").hexdigest()),
        ]
    )
    calls = []
    real_hashes = checksums_module.file_hashes

    def tracking_hashes(path):
        calls.append(path.name)
        return real_hashes(path)

    monkeypatch.setattr(checksums_module, "file_hashes", tracking_hashes)

    verify_rc = main(["--config", str(config_path), "verify", "org/model"], hub=hub)
    repair_rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    assert verify_rc == 1
    assert repair_rc == 0
    assert "repaired: org/model" in capsys.readouterr().out
    assert calls.count("good.bin") == 1
    assert calls.count("bad.bin") == 2


def test_full_verify_can_hash_directly_when_checksums_are_disabled(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path), "checksum": False}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "file.bin").write_bytes(b"abc")
    hub = FakeHub([FakeFile("file.bin", 3, lfs_sha256=hashlib.sha256(b"abc").hexdigest())])

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=hub)

    assert rc == 0
    assert "verified (full)" in capsys.readouterr().out
    assert not (archive / ".manifest").exists()


def test_repair_command_uses_existing_verification_state_and_reports_age(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["missing.bin"],
            checked_at_utc=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"),
        ),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 0
    assert "verification age: 2d" in output
    assert "warning: verification is older than 24h" in output
    assert "repaired: org/model" in output
    assert (archive / "missing.bin").exists()


def test_repair_command_reports_missing_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "verification age: unavailable" in output
    assert "run verify first: model-mirror verify org/model" in output
    assert "verify-required" in output


def test_repair_command_reports_incomplete_verification_data(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "good.bin").write_bytes(b"good")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["missing.bin"],
        ),
    )
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("good.bin", 4, lfs_sha256="sha"), FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "could not fully repair org/model; missing verification data" in output
    assert "model-mirror verify org/model && model-mirror repair org/model" in output
    assert "verification-incomplete" in output
    assert hub.downloads == []


def test_repair_force_partial_warns_and_attempts_download(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    (archive / "good.bin").write_bytes(b"good")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            repair_paths=["missing.bin"],
        ),
    )
    hub = FakeHub([FakeFile("config.json", 2), FakeFile("good.bin", 4, lfs_sha256="sha"), FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "--force-partial", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "warning: --force-partial can leave the repository inconsistent" in output
    assert "verification-incomplete" in output
    assert hub.downloads == ["org/model"]


def test_repair_command_fails_for_offline_only_model(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit="main",
            offline_only=True,
            repair_paths=["missing.bin"],
        ),
    )

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=UnavailableHub())

    output = capsys.readouterr().out
    assert rc == 1
    assert "cannot repair offline-only model org/model" in output
    assert "model-mirror online org/model" in output
    assert "offline-only: org/model" in output


def test_repair_command_reports_changed_upstream_from_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        archive,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
            repair_paths=["missing.bin"],
        ),
    )
    hub = MovingBranchHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 0
    assert "upstream changed: org/model local=oldcommit upstream=newcommit not_applied" in output
    assert hub.download_revisions == ["oldcommit"]


def test_repair_discards_obsolete_recorded_path_not_in_pinned_snapshot(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", resolved_commit="main", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([])

    rc = main(["--config", str(config_path), "repair", "org/model"], hub=hub)

    assert rc == 0
    assert "repaired" in capsys.readouterr().out
    assert hub.downloads == []


def test_repair_all_repairs_each_model_with_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    for repo in ("one/model", "two/model"):
        archive = tmp_path / "models" / repo
        archive.mkdir(parents=True)
        (archive / "config.json").write_text("{}", encoding="utf-8")
        write_verification_state(
            archive,
            VerificationState(status="dirty", repo_id=repo, resolved_commit="main", repair_paths=["missing.bin"]),
        )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 0
    assert "repaired: one/model" in output
    assert "repaired: two/model" in output
    assert (tmp_path / "models" / "one" / "model" / "missing.bin").exists()
    assert (tmp_path / "models" / "two" / "model" / "missing.bin").exists()


def test_repair_all_skips_busy_model_and_continues(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    busy_archive = tmp_path / "models" / "busy" / "model"
    busy_archive.mkdir(parents=True)
    write_verification_state(
        busy_archive,
        VerificationState(status="dirty", repo_id="busy/model", resolved_commit="main", repair_paths=["missing.bin"]),
    )
    ok_archive = tmp_path / "models" / "ok" / "model"
    ok_archive.mkdir(parents=True)
    (ok_archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        ok_archive,
        VerificationState(status="dirty", repo_id="ok/model", resolved_commit="main", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    with ModelLock(busy_archive, "mirror", "busy/model"):
        rc = main(["--config", str(config_path), "repair", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "skipped busy: busy/model" in output
    assert "command=mirror" in output
    assert "repaired: ok/model" in output


def test_repair_all_returns_failure_when_any_model_needs_verify(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    missing_state_archive = tmp_path / "models" / "missing" / "state"
    missing_state_archive.mkdir(parents=True)
    ok_archive = tmp_path / "models" / "ok" / "model"
    ok_archive.mkdir(parents=True)
    (ok_archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        ok_archive,
        VerificationState(status="dirty", repo_id="ok/model", resolved_commit="main", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 1
    assert "verify-required: missing/state" in output
    assert "repaired: ok/model" in output


def test_repair_all_skips_offline_only_models_without_failing(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    offline_archive = tmp_path / "models" / "offline" / "model"
    offline_archive.mkdir(parents=True)
    write_verification_state(
        offline_archive,
        VerificationState(
            status="dirty",
            repo_id="offline/model",
            resolved_commit="main",
            offline_only=True,
            repair_paths=["missing.bin"],
        ),
    )
    ok_archive = tmp_path / "models" / "ok" / "model"
    ok_archive.mkdir(parents=True)
    (ok_archive / "config.json").write_text("{}", encoding="utf-8")
    write_verification_state(
        ok_archive,
        VerificationState(status="dirty", repo_id="ok/model", resolved_commit="main", repair_paths=["missing.bin"]),
    )
    hub = FakeHub([FakeFile("missing.bin", 3)])

    rc = main(["--config", str(config_path), "repair", "--all"], hub=hub)

    output = capsys.readouterr().out
    assert rc == 0
    assert "skipped offline-only: offline/model" in output
    assert "repaired: ok/model" in output
    assert hub.downloads == ["ok/model"]


def test_repair_rejects_missing_or_conflicting_targets(tmp_path):
    config_path = tmp_path / "config.yaml"

    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "repair"])
    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "repair", "--all", "org/model"])


def test_repair_update_applies_changed_upstream_from_verification_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            requested_revision="main",
            resolved_commit="oldcommit",
            upstream_commit="newcommit",
            upstream_status="changed",
        ),
    )
    hub = FakeHub(
        [FakeFile("file.bin", 3)],
        metadata_by_revision={"newcommit": [FakeFile("config.json", 2), FakeFile("file.bin", 4)]},
    )

    rc = main(["--config", str(config_path), "repair", "--update", "org/model"], hub=hub)

    assert rc == 0
    assert hub.downloads == ["org/model"]
    assert hub.download_revisions == ["newcommit"]
    assert "updated: org/model" in capsys.readouterr().out


def test_update_command_is_removed(tmp_path):
    config_path = tmp_path / "config.yaml"

    with pytest.raises(SystemExit):
        main(["--config", str(config_path), "update", "org/model"])


def test_offline_and_online_commands_toggle_state_and_list_tags(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(status="dirty", repo_id="org/model", repair_paths=["bad.bin"]),
    )

    assert main(["--config", str(config_path), "offline", "org/model"]) == 0
    assert read_verification_state(archive).offline_only is True
    assert main(["--config", str(config_path), "list"]) == 0
    list_output = capsys.readouterr().out
    assert "offline-only enabled: org/model" in list_output
    assert "needs-repair,offline" in list_output

    assert main(["--config", str(config_path), "online", "org/model"]) == 0
    assert read_verification_state(archive).offline_only is False
    assert "offline-only disabled: org/model" in capsys.readouterr().out


def test_offline_command_reports_missing_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")

    rc = main(["--config", str(config_path), "offline", "org/model"])

    output = capsys.readouterr().out
    assert rc == 1
    assert "verification state unavailable: org/model" in output
    assert "run verify first: model-mirror verify org/model" in output


def test_verify_unavailable_upstream_suggests_offline_and_preserves_clean_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=UnavailableHub())

    output = capsys.readouterr().out
    state = read_verification_state(archive)
    assert rc == 1
    assert state.status == "clean"
    assert "upstream unavailable: repository not found" in state.issues
    assert "upstream repository unavailable: repository not found" in output
    assert "run: model-mirror offline org/model" in output

    assert main(["--config", str(config_path), "list"]) == 0
    list_output = capsys.readouterr().out
    assert "upstream-unavailable" in list_output
    assert "clean" not in list_output

    assert main(["--config", str(config_path), "offline", "org/model"]) == 0
    state = read_verification_state(archive)
    assert state.offline_only is True
    assert state.status == "clean"
    assert state.issues == []

    assert main(["--config", str(config_path), "verify", "--cached", "org/model"], hub=UnavailableHub()) == 0
    assert "verified (offline-only cached)" in capsys.readouterr().out


def test_verify_all_max_age_does_not_skip_upstream_unavailable_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(
            status="clean",
            repo_id="org/model",
            issues=["upstream unavailable: repository not found"],
            checked_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )

    rc = main(["--config", str(config_path), "verify", "--all", "--max-age", "7d"], hub=UnavailableHub())

    output = capsys.readouterr().out
    assert rc == 1
    assert "skipped recent clean verification" not in output
    assert "run: model-mirror offline org/model" in output


def test_verify_unavailable_upstream_without_state_writes_unavailable_state(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=UnavailableHub())

    output = capsys.readouterr().out
    state = read_verification_state(archive)
    assert rc == 1
    assert state.status == "unavailable"
    assert "run: model-mirror offline org/model" in output

    assert main(["--config", str(config_path), "offline", "org/model"]) == 0
    state = read_verification_state(archive)
    assert state.offline_only is True
    assert state.status == "incomplete"
    assert state.issues == ["local verification required"]


def test_verify_unavailable_stored_commit_suggests_offline(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(
        archive,
        VerificationState(status="clean", repo_id="org/model", requested_revision="main", resolved_commit="oldcommit"),
    )

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=MissingStoredCommitHub())

    output = capsys.readouterr().out
    state = read_verification_state(archive)
    assert rc == 1
    assert state.status == "clean"
    assert state.issues == ["upstream unavailable: stored commit not found"]
    assert "upstream repository unavailable: stored commit not found" in output
    assert "run: model-mirror offline org/model" in output


def test_offline_only_verify_uses_local_manifest_without_hub(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    write_checksums(archive)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model", offline_only=True))

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=UnavailableHub())

    assert rc == 0
    assert "verified (offline-only full): org/model" in capsys.readouterr().out
    assert read_verification_state(archive).offline_only is True


def test_offline_only_verify_without_manifest_is_incomplete(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model", offline_only=True))

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=UnavailableHub())

    output = capsys.readouterr().out
    state = read_verification_state(archive)
    assert rc == 1
    assert "offline-only verification incomplete: org/model missing .manifest" in output
    assert state.status == "incomplete"
    assert state.issues == [".manifest missing"]


def test_offline_full_verify_requires_manifest_even_when_checksums_disabled(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path), "checksum": False}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))

    rc = main(["--config", str(config_path), "verify", "--offline", "org/model"])

    output = capsys.readouterr().out
    assert rc == 1
    assert "offline verification incomplete: org/model missing .manifest" in output


def test_offline_only_verify_failure_does_not_suggest_repair(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "file.bin").write_bytes(b"abc")
    write_checksums(archive)
    (archive / "file.bin").write_bytes(b"changed")
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model", offline_only=True))

    rc = main(["--config", str(config_path), "verify", "org/model"], hub=UnavailableHub())

    output = capsys.readouterr().out
    assert rc == 1
    assert "offline-only verification failed: org/model" in output
    assert "repair unavailable for offline-only model: org/model" in output
    assert "next: model-mirror repair" not in output


def test_offline_full_verify_strict_reports_extra_files(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / "tracked.bin").write_bytes(b"abc")
    write_checksums(archive)
    (archive / "extra.bin").write_bytes(b"extra")
    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))

    rc = main(["--config", str(config_path), "verify", "--offline", "--strict", "org/model"])

    output = capsys.readouterr().out
    state = read_verification_state(archive)
    assert rc == 1
    assert "offline verification failed: org/model" in output
    assert state.repair_paths == []
    assert "extras: extra.bin" in state.issues


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

    rc = main(["--config", str(config_path), "verify", "--all", "--cached", "--max-age", "7d"], hub=hub)

    assert rc == 0
    assert "skipped recent clean verification" in capsys.readouterr().out


def test_offline_verify_modes(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"directory": str(tmp_path)}), encoding="utf-8")
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)

    assert main(["--config", str(config_path), "verify", "--offline", "--cached", "org/model"]) == 1
    assert "offline verification unavailable" in capsys.readouterr().out

    write_verification_state(archive, VerificationState(status="clean", repo_id="org/model"))
    assert main(["--config", str(config_path), "verify", "--offline", "--cached", "org/model"]) == 0
    assert "verified (offline cached)" in capsys.readouterr().out


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
    assert format_age_seconds(None) == "unknown"


def test_verification_age_falls_back_to_state_file_mtime(tmp_path, capsys):
    archive = tmp_path / "models" / "org" / "model"
    archive.mkdir(parents=True)
    (archive / ".verification").write_text(
        yaml.safe_dump({"status": "dirty", "repo_id": "org/model", "checked_at_utc": "invalid"}),
        encoding="utf-8",
    )

    print_verification_age(Config(directory=tmp_path), "org/model", "model")

    assert "verification age: 0s" in capsys.readouterr().out
