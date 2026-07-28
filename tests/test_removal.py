import json
from types import SimpleNamespace

import pytest
import yaml

import model_mirror.cli as cli_module
import model_mirror.removal as removal_module
import model_mirror.torrent_publication as publication_module
from model_mirror.cli import confirm_removal, main
from model_mirror.config import Config, archive_path
from model_mirror.hub import HubFile, HubSnapshot, write_snapshot_plan
from model_mirror.removal import (
    REMOVAL_RECORD,
    RemovalRecord,
    complete_removal,
    read_removal_record,
    removal_path,
    removal_record_path,
    prune_empty_removal_parents,
    stage_removal,
    write_removal_record,
)
from model_mirror.state import VerificationState, write_verification_state


COMMIT = "a" * 40


def removal_record(root, *, repo_id="org/model", repo_type="model"):
    return RemovalRecord(
        repo_id=repo_id,
        repo_type=repo_type,
        original_path=str(root),
        status="clean",
        exceptions="none",
        resolved_commit=COMMIT,
        checked_at_utc="2026-07-28T00:00:00+00:00",
        check_age="1h",
        payload_files=1,
        payload_size=3,
    )


def write_test_mirror(root, *, repo_id="org/model", repo_type="model"):
    root.mkdir(parents=True)
    (root / "file.bin").write_bytes(b"abc")
    write_snapshot_plan(
        root,
        HubSnapshot(
            repo_id,
            repo_type,
            "main",
            COMMIT,
            [HubFile("file.bin", 3, lfs_sha256="b" * 64)],
        ),
    )
    write_verification_state(
        root,
        VerificationState(
            status="clean",
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision="main",
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
            upstream_status="current",
            checked_at_utc="2026-07-28T00:00:00+00:00",
        ),
    )


def write_config(path, directory):
    path.write_text(yaml.safe_dump({"directory": str(directory)}), encoding="utf-8")


def test_removal_record_round_trip_and_validation(tmp_path):
    root = tmp_path / "mirror"
    root.mkdir()
    record = removal_record(root)

    assert read_removal_record(root) is None
    path = write_removal_record(root, record)
    loaded = read_removal_record(root)

    assert loaded.repo_id == "org/model"
    assert loaded.started_at_utc
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == "model-mirror-removal"
    assert removal_path(Config(directory=tmp_path), "org/model", "model") == (
        tmp_path / ".model-mirror-removals" / "models" / "org" / "model"
    )

    with pytest.raises(ValueError, match="Unsupported removal record"):
        RemovalRecord.from_dict({}, source=path)
    with pytest.raises(ValueError, match="Malformed removal record"):
        RemovalRecord.from_dict(
            {"schema": "model-mirror-removal", "version": 1},
            source=path,
        )
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed removal record"):
        read_removal_record(root)

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    with pytest.raises(FileExistsError, match="interrupted removal"):
        stage_removal(root, occupied, record)


def test_removal_is_resumable_and_deletes_payload_before_metadata(tmp_path, monkeypatch):
    source = tmp_path / "models" / "org" / "model"
    source.mkdir(parents=True)
    (source / "file.bin").write_bytes(b"abc")
    (source / ".verification").write_text("status: clean\n", encoding="utf-8")
    (source / ".verification.lock").touch()
    metadata = source / ".model-mirror"
    metadata.mkdir()
    (metadata / "snapshot.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"keep")
    (source / "linked-directory").symlink_to(outside, target_is_directory=True)
    staged = tmp_path / ".model-mirror-removals" / "models" / "org" / "model"
    stage_removal(source, staged, removal_record(source))

    removed = []
    real_unlink = removal_module.unlink_removal_path

    def interrupt_on_verification(path):
        removed.append(path.relative_to(staged).as_posix())
        if path.name == ".verification":
            raise OSError("simulated interruption")
        real_unlink(path)

    monkeypatch.setattr(removal_module, "unlink_removal_path", interrupt_on_verification)

    with pytest.raises(OSError, match="simulated interruption"):
        complete_removal(staged)

    assert removed.index("file.bin") < removed.index(".verification")
    assert removed.index("linked-directory") < removed.index(".verification")
    assert not (staged / "file.bin").exists()
    assert removal_record_path(staged).exists()
    assert (staged / ".verification.lock").exists()
    assert (outside / "keep.bin").exists()

    monkeypatch.setattr(removal_module, "unlink_removal_path", real_unlink)
    complete_removal(staged)

    assert not staged.exists()
    assert (outside / "keep.bin").exists()


def test_remove_prompts_with_details_and_requires_exact_repo_id(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    write_config(config_path, tmp_path)
    root = archive_path(Config(directory=tmp_path), "org/model", "model")
    write_test_mirror(root)

    monkeypatch.setattr("builtins.input", lambda prompt: "no")
    assert main(["--config", str(config_path), "remove", "org/model"]) == 0

    output = capsys.readouterr().out
    assert "mirror selected for permanent removal:" in output
    assert "repository: org/model" in output
    assert f"resolved_commit: {COMMIT}" in output
    assert "verification_age:" in output
    assert "payload_files: 1" in output
    assert "payload_size: 3 B (3 bytes)" in output
    assert "cancelled:" in output
    assert root.exists()

    monkeypatch.setattr("builtins.input", lambda prompt: "org/model")
    assert main(["--config", str(config_path), "remove", "org/model"]) == 0

    assert "removed: model:org/model (1 files, 3 B)" in capsys.readouterr().out
    assert not root.exists()
    assert not removal_path(Config(directory=tmp_path), "org/model", "model").exists()


def test_remove_blocks_publications_and_resumes_without_deleting_new_mirror(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path)
    write_config(config_path, tmp_path)
    root = archive_path(config, "org/model", "model")
    write_test_mirror(root)
    publication = SimpleNamespace(
        publication_id=f"huggingface:model:org/model@{COMMIT}",
        repo_id="org/model",
        repo_type="model",
    )
    monkeypatch.setattr(publication_module, "load_fenced_publication", lambda candidate: (publication, root))

    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "remove blocked by active torrent publication" in capsys.readouterr().out
    assert root.exists()

    monkeypatch.setattr(publication_module, "load_fenced_publication", lambda candidate: None)
    staged = removal_path(config, "org/model", "model")
    stage_removal(root, staged, removal_record(root))
    write_test_mirror(root)

    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "removed interrupted mirror" in output
    assert "a newer mirror still exists and was not removed" in output
    assert root.exists()
    assert not staged.exists()

    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 0
    assert not root.exists()
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "mirror not found" in capsys.readouterr().out


def test_remove_refuses_unsafe_paths_and_confirmation_handles_closed_input(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path)
    write_config(config_path, tmp_path)
    root = archive_path(config, "org/model", "model")
    outside = tmp_path / "outside"
    outside.mkdir()
    root.parent.mkdir(parents=True)
    root.symlink_to(outside, target_is_directory=True)

    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "unsafe mirror path" in capsys.readouterr().out
    assert outside.exists()

    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))
    assert confirm_removal("org/model") is False
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert confirm_removal("org/model") is False
    assert capsys.readouterr().out == "\n\n"


def test_remove_surfaces_corrupt_metadata_and_preserves_interrupted_work(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path)
    write_config(config_path, tmp_path)
    root = archive_path(config, "org/model", "model")
    root.mkdir(parents=True)
    (root / "file.bin").write_bytes(b"abc")
    (root / ".verification").write_text("{", encoding="utf-8")
    snapshot_path = root / ".model-mirror" / "snapshot.json"
    snapshot_path.parent.mkdir()
    snapshot_path.write_text("{", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert main(["--config", str(config_path), "remove", "org/model"]) == 0
    output = capsys.readouterr().out
    assert "verification-metadata-error,snapshot-metadata-error" in output

    write_verification_state(
        root,
        VerificationState(
            status="dirty",
            repo_id="org/model",
            resolved_commit=COMMIT,
            upstream_commit=COMMIT,
            repair_paths=["file.bin"],
        ),
    )
    write_snapshot_plan(
        root,
        HubSnapshot("org/model", "model", "main", "oldcommit", [HubFile("file.bin", 3)]),
    )
    write_removal_record(root, removal_record(root))
    assert main(["--config", str(config_path), "status", "org/model"]) == 0
    assert "removal-pending" in capsys.readouterr().out
    removal_record_path(root).unlink()

    assert main(["--config", str(config_path), "remove", "org/model"]) == 0
    assert "needs-repair,snapshot-stale" in capsys.readouterr().out

    def fail_publication_read(candidate):
        raise ValueError("bad fence")

    monkeypatch.setattr(publication_module, "load_fenced_publication", fail_publication_read)
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "torrent publication state is unreadable" in capsys.readouterr().out
    monkeypatch.setattr(publication_module, "load_fenced_publication", lambda candidate: None)

    real_stage = cli_module.stage_removal

    def fail_stage(source, target, record):
        write_removal_record(source, record)
        raise OSError("rename unavailable")

    monkeypatch.setattr(cli_module, "stage_removal", fail_stage)
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "removal interrupted" in capsys.readouterr().out
    assert root.exists() and removal_record_path(root).exists()

    monkeypatch.setattr(cli_module, "stage_removal", real_stage)
    assert main(["--config", str(config_path), "remove", "org/model"]) == 0
    assert "prepared interrupted removal found" in capsys.readouterr().out
    assert root.exists() and not removal_record_path(root).exists()

    removal_record_path(root).write_text("{", encoding="utf-8")
    assert main(["--config", str(config_path), "remove", "org/model"]) == 0
    assert "prepared interrupted removal found" in capsys.readouterr().out
    assert root.exists() and not removal_record_path(root).exists()

    def fail_completion(candidate):
        raise OSError("disk error")

    monkeypatch.setattr(cli_module, "complete_removal", fail_completion)
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    staged = removal_path(config, "org/model", "model")
    assert staged.exists() and not root.exists()
    assert "removal interrupted" in capsys.readouterr().out

    other = archive_path(config, "other/model", "model")
    write_test_mirror(other, repo_id="other/model")

    def fail_inspection(*args, **kwargs):
        raise ValueError("cannot inspect")

    monkeypatch.setattr(cli_module, "inspect_removal", fail_inspection)
    assert main(["--config", str(config_path), "remove", "--yes", "other/model"]) == 1
    assert "remove failed" in capsys.readouterr().out
    assert other.exists()


def test_remove_resume_handles_corrupt_records_repeated_failures_and_unsafe_tombstones(
    tmp_path,
    monkeypatch,
    capsys,
):
    config_path = tmp_path / "config.yaml"
    config = Config(directory=tmp_path)
    write_config(config_path, tmp_path)
    root = archive_path(config, "org/model", "model")
    write_test_mirror(root)
    staged = removal_path(config, "org/model", "model")
    stage_removal(root, staged, removal_record(root))
    write_removal_record(staged, removal_record(root, repo_id="other/model"))

    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "removal record identity is model:other/model" in capsys.readouterr().out
    assert staged.exists()

    removal_record_path(staged).write_text("{", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert main(["--config", str(config_path), "remove", "org/model"]) == 0
    assert "cancelled: interrupted removal remains" in capsys.readouterr().out
    assert staged.exists()

    def fail_completion(candidate):
        raise OSError("still busy")

    monkeypatch.setattr(cli_module, "complete_removal", fail_completion)
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "removal still incomplete" in capsys.readouterr().out

    class FailingLock:
        def __enter__(self):
            raise OSError("lock unavailable")

        def __exit__(self, exc_type, exc, tb):
            return False

    real_model_lock = cli_module.ModelLock
    monkeypatch.setattr(cli_module, "ModelLock", lambda *args, **kwargs: FailingLock())
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 1
    assert "remove failed" in capsys.readouterr().out

    monkeypatch.setattr(cli_module, "ModelLock", real_model_lock)
    monkeypatch.setattr(cli_module, "complete_removal", removal_module.complete_removal)
    assert main(["--config", str(config_path), "remove", "--yes", "org/model"]) == 0
    assert "removed interrupted mirror" in capsys.readouterr().out
    assert not root.exists() and not staged.exists()

    unsafe_root = archive_path(config, "unsafe/model", "model")
    unsafe_staged = removal_path(config, "unsafe/model", "model")
    outside = tmp_path / "outside-staged"
    outside.mkdir()
    unsafe_staged.parent.mkdir(parents=True)
    unsafe_staged.symlink_to(outside, target_is_directory=True)

    assert main(["--config", str(config_path), "remove", "--yes", "unsafe/model"]) == 1
    assert "unsafe interrupted-removal path" in capsys.readouterr().out
    assert outside.exists() and not unsafe_root.exists()

    nonempty_parent = tmp_path / "removal-parent" / "child"
    nonempty_parent.mkdir(parents=True)
    (nonempty_parent / "keep").touch()
    prune_empty_removal_parents(nonempty_parent, stop=tmp_path / "removal-parent")
    assert nonempty_parent.exists()
