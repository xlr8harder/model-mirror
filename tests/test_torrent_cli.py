import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import model_mirror.torrent_import as import_module
import model_mirror.torrent_seed as seed_module
import model_mirror.torrent_coverage as coverage_module
from model_mirror.checksums import write_checksums
from model_mirror.cli import (
    build_parser,
    handle_torrent_locked,
    list_torrent_status,
    main,
    print_join_progress,
    print_upgrade_progress,
    torrent_coverage_status,
)
from model_mirror.config import Config
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.state import VerificationState, write_verification_state
from model_mirror.torrent_import import ImportResult, parse_publication_metainfo
from model_mirror.torrent_publication import create_publication, update_observed_backend


COMMIT = "a" * 40


def test_torrent_and_upgrade_help_are_experimental(capsys):
    parser = build_parser()
    parser.print_help()
    main_help = capsys.readouterr().out
    assert "EXPERIMENTAL: add complete torrent hash coverage" in main_help
    assert "EXPERIMENTAL: publish, seed, inspect" in main_help

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["upgrade", "--help"])
    assert exc.value.code == 0
    assert "EXPERIMENTAL: torrent interfaces and metadata formats" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["torrent", "join", "--help"])
    assert exc.value.code == 0
    assert "EXPERIMENTAL: torrent interfaces and metadata formats" in capsys.readouterr().out


def archive(tmp_path, *, repo_id="org/model"):
    root = tmp_path / "models" / Path(repo_id)
    root.mkdir(parents=True)
    payload = b"payload"
    (root / "file.bin").write_bytes(payload)
    item = HubFile("file.bin", len(payload), lfs_sha256=hashlib.sha256(payload).hexdigest())
    write_checksums(root)
    write_snapshot_plan(root, HubSnapshot(repo_id, "model", "main", COMMIT, [item]))
    write_verification_state(
        root,
        VerificationState("clean", repo_id, resolved_commit=COMMIT, upstream_commit=COMMIT),
    )
    return root


def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"directory": str(tmp_path)}))
    return path


def test_upgrade_cli_dry_run_one_and_all(tmp_path, capsys):
    config = config_file(tmp_path)
    archive(tmp_path)
    archive(tmp_path, repo_id="other/model")

    assert main(["--config", str(config), "upgrade", "--dry-run", "org/model"]) == 0
    assert "would-hash_files=1" in capsys.readouterr().out
    assert main(["--config", str(config), "upgrade", "org/model"]) == 0
    assert "hashed: org/model:file.bin" in capsys.readouterr().out
    assert main(["--config", str(config), "upgrade", "--all", "--dry-run"]) == 0
    assert "other/model" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="accepts"):
        main(["--config", str(config), "upgrade", "--all", "org/model"])
    with pytest.raises(SystemExit, match="requires"):
        main(["--config", str(config), "upgrade"])

    missing = tmp_path / "models" / "missing" / "snapshot"
    missing.mkdir(parents=True)
    assert main(["--config", str(config), "upgrade", "missing/snapshot"]) == 1
    assert "no pinned snapshot" in capsys.readouterr().out

    (tmp_path / "models" / "other" / "model" / ".model-mirror" / "snapshot.json").write_text("{")
    assert main(["--config", str(config), "upgrade", "other/model"]) == 1
    assert "upgrade failed" in capsys.readouterr().out


def test_upgrade_incomplete_result_and_nonterminal_progress_branch(tmp_path, capsys, monkeypatch):
    config = config_file(tmp_path)
    archive(tmp_path)
    monkeypatch.setattr(
        coverage_module,
        "upgrade_coverage",
        lambda *args, **kwargs: SimpleNamespace(
            covered_files=0,
            total_files=1,
            hashed_files=0,
            hashed_bytes=0,
            complete=False,
            path=tmp_path / "coverage",
        ),
    )
    assert main(["--config", str(config), "upgrade", "org/model"]) == 1
    print_upgrade_progress("org/model", "file.bin", 1, 2)
    assert "hashed:" not in capsys.readouterr().out


def test_torrent_create_publish_show_stop_retire_and_errors(tmp_path, capsys):
    config = config_file(tmp_path)
    root = archive(tmp_path)

    assert main(["--config", str(config), "torrent", "create", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "torrent created" in output and "magnet:" in output and "external client data location" in output
    assert "desired=stopped" in list_torrent_status(root)
    assert torrent_coverage_status(root) == "complete"
    assert torrent_coverage_status(tmp_path / "missing") == "unavailable"
    assert main(["--config", str(config), "status"]) == 0
    status_output = capsys.readouterr().out
    assert "TORRENT" in status_output
    assert "published,managed" in status_output
    assert main(["--config", str(config), "torrent", "show", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "feature_stability: experimental" in output
    assert "desired_seed: false" in output and "upstream_provenance: upstream-verified" in output

    update_observed_backend(root, state="error", detail="backend detail")
    state = VerificationState(
        "clean",
        "org/model",
        resolved_commit=COMMIT,
        upstream_commit="b" * 40,
        upstream_status="changed",
    )
    write_verification_state(root, state)
    assert main(["--config", str(config), "torrent", "show", "org/model"]) == 0
    detail_output = capsys.readouterr().out
    assert "observed_detail: backend detail" in detail_output
    assert "update_available: true" in detail_output
    assert "update-available" in list_torrent_status(root)

    coverage_path = next((root / ".model-mirror" / "torrent" / "coverage").glob("*.json"))
    coverage_path.unlink()
    assert torrent_coverage_status(root) == "partial"
    coverage_path.write_text("{")
    assert "coverage=metadata-error" in list_torrent_status(root)
    coverage_path.unlink()
    assert main(["--config", str(config), "upgrade", "org/model"]) == 0
    capsys.readouterr()

    assert main(["--config", str(config), "torrent", "publish", "org/model"]) == 0
    assert "desired seed: enabled" in capsys.readouterr().out
    assert main(["--config", str(config), "torrent", "stop", "org/model"]) == 0
    assert "fence retained" in capsys.readouterr().out
    assert main(["--config", str(config), "torrent", "retire", "org/model"]) == 0
    assert "cannot be revoked" in capsys.readouterr().out
    assert main(["--config", str(config), "torrent", "retire", "org/model"]) == 1
    assert "retire failed" in capsys.readouterr().out
    assert main(["--config", str(config), "torrent", "show", "org/model"]) == 0
    assert "unpublished" in capsys.readouterr().out
    assert main(["--config", str(config), "torrent", "stop", "org/model"]) == 1
    assert "failed" in capsys.readouterr().out

    assert main(["--config", str(config), "torrent"]) == 2
    assert "requires a subcommand" in capsys.readouterr().out

    # External mode is an ordinary client handoff and does not claim runtime ownership.
    assert main(["--config", str(config), "torrent", "publish", "--external", "org/model"]) == 0
    assert "client mode: external" in capsys.readouterr().out

    with pytest.raises(ValueError, match="unsupported"):
        handle_torrent_locked(
            SimpleNamespace(torrent_command="unknown"),
            root,
        )
    assert list_torrent_status(tmp_path / "missing") is None
    (root / ".model-mirror" / "torrent" / "fence.json").write_text("{")
    assert list_torrent_status(root) == "metadata-error"


def test_torrent_handoff_and_import_cli(tmp_path, capsys):
    source_base = tmp_path / "source"
    source = archive(source_base)
    publication = create_publication(source, repo_id="org/model", repo_type="model")
    destination = tmp_path / "destination"
    destination.mkdir(parents=True)
    config = config_file(destination)

    assert main(["--config", str(config), "torrent", "handoff", str(publication.torrent_path)]) == 0
    output = capsys.readouterr().out
    assert "after download:" in output and "torrent import" in output
    parsed = parse_publication_metainfo(publication.torrent_path.read_bytes())
    payload_root = destination / ".torrent-staging" / "manual" / parsed.root_name
    payload_root.mkdir(parents=True)
    shutil.copyfile(source / "file.bin", payload_root / "file.bin")

    assert main(
        [
            "--config",
            str(config),
            "torrent",
            "import",
            "--seed",
            str(publication.torrent_path),
            str(payload_root),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "torrent imported" in output and "desired seed: enabled" in output

    assert main(["--config", str(config), "torrent", "handoff", "missing.torrent"]) == 1
    assert "failed" in capsys.readouterr().out


def test_torrent_join_and_serve_cli_dispatch(tmp_path, capsys, monkeypatch):
    config_path = config_file(tmp_path)
    root = archive(tmp_path)
    publication = create_publication(root, repo_id="org/model", repo_type="model")
    called = {}

    def fake_join(config, source, **kwargs):
        called["source"] = source
        called["seed"] = kwargs["seed"]
        kwargs["on_progress"](
            SimpleNamespace(
                name="model",
                progress=0.5,
                download_payload_rate=1024,
                num_peers=2,
            )
        )
        return ImportResult(root, publication.record, 0, 0)

    monkeypatch.setattr(import_module, "join_torrent", fake_join)
    assert main(
        ["--config", str(config_path), "torrent", "join", "--seed", "magnet:?xt=urn:btih:test"]
    ) == 0
    assert called == {"source": "magnet:?xt=urn:btih:test", "seed": True}
    assert "50.0%" in capsys.readouterr().out

    def failed_join(*args, **kwargs):
        raise RuntimeError("join failed")

    monkeypatch.setattr(import_module, "join_torrent", failed_join)
    assert main(["--config", str(config_path), "torrent", "join", "bad"]) == 1
    assert "join failed" in capsys.readouterr().out

    monkeypatch.setattr(seed_module, "serve", lambda config, **kwargs: called.update(kwargs))
    assert main(["--config", str(config_path), "torrent", "serve", "--once"]) == 0
    assert called["once"] is True
    assert "single reconciliation" in capsys.readouterr().out

    monkeypatch.setattr(
        seed_module,
        "serve",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("backend unavailable")),
    )
    assert main(["--config", str(config_path), "torrent", "serve", "--once"]) == 1
    assert "backend unavailable" in capsys.readouterr().out


def test_print_join_progress_format(capsys):
    print_join_progress(
        SimpleNamespace(
            name="x",
            progress=0.25,
            download_payload_rate=2048,
            num_peers=3,
        )
    )
    assert "25.0%" in capsys.readouterr().out
