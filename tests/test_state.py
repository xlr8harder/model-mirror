from types import SimpleNamespace

import yaml

from model_mirror.state import (
    VerificationState,
    audit_state_path,
    read_verification_state,
    repair_paths_from_results,
    state_from_results,
    upstream_status,
    verification_state_path,
    write_verification_state,
)


def test_verification_state_lives_directly_inside_model_directory(tmp_path):
    state = VerificationState(
        status="clean",
        repo_id="org/model",
        offline_only=True,
        checked_at_utc="2026-01-01T00:00:00+00:00",
    )

    path = write_verification_state(tmp_path, state)

    assert path == tmp_path / ".verification"
    assert verification_state_path(tmp_path) == tmp_path / ".verification"
    assert audit_state_path(tmp_path) == tmp_path / ".verification"
    loaded = read_verification_state(tmp_path)
    assert loaded is not None
    assert loaded.clean is True
    assert loaded.repo_id == "org/model"
    assert loaded.offline_only is True


def test_verification_state_rejects_non_mapping_yaml(tmp_path):
    (tmp_path / ".verification").write_text("- bad\n", encoding="utf-8")

    try:
        read_verification_state(tmp_path)
    except ValueError as exc:
        assert "YAML mapping" in str(exc)
    else:
        raise AssertionError("invalid state should fail")


def test_state_from_results_collects_repair_paths_and_issues():
    remote = SimpleNamespace(
        ok=False,
        missing=["missing.bin"],
        size_mismatches=["wrong-size.bin"],
        hash_mismatches=["wrong-hash.bin"],
        cached_hash_missing=["no-checksum.bin"],
        extras=["extra.bin"],
    )
    verify = SimpleNamespace(ok=False, missing_files=["missing.safetensors"], failures=["config.json: invalid"])

    state = state_from_results("org/model", "model", "main", remote, verify)

    assert state.status == "dirty"
    assert state.repair_paths == [
        "config.json",
        "missing.bin",
        "missing.safetensors",
        "wrong-hash.bin",
        "wrong-size.bin",
    ]
    assert "cached_hash_missing: no-checksum.bin" in state.issues
    assert "extras: extra.bin" in state.issues


def test_state_from_cached_hash_missing_only_is_incomplete_not_repairable():
    remote = SimpleNamespace(
        ok=False,
        missing=[],
        size_mismatches=[],
        hash_mismatches=[],
        cached_hash_missing=["file.bin"],
        extras=[],
    )

    state = state_from_results("org/model", "model", "main", remote)

    assert state.status == "incomplete"
    assert state.repair_paths == []
    assert state.issues == ["cached_hash_missing: file.bin"]


def test_repair_paths_from_clean_results_is_empty():
    remote = SimpleNamespace(
        missing=[],
        size_mismatches=[],
        hash_mismatches=[],
        cached_hash_missing=[],
    )

    assert repair_paths_from_results(remote) == []


def test_repair_paths_ignore_non_file_like_audit_failures():
    remote = SimpleNamespace(missing=[], size_mismatches=[], hash_mismatches=[], cached_hash_missing=[])
    audit = SimpleNamespace(missing_files=[], failures=["runtime failure without a path"])

    assert repair_paths_from_results(remote, audit) == []


def test_upstream_status_is_unknown_without_resolved_commit():
    assert upstream_status("", "upstream") == "unknown"
