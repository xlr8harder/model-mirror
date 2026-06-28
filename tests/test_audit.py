import json
import sys
from types import SimpleNamespace

import pytest

import model_mirror.audit as audit_module
from model_mirror.audit import audit_model, validate_safetensors_file


def write_safetensors(path, header, payload):
    encoded = json.dumps(header).encode("utf-8")
    path.write_bytes(len(encoded).to_bytes(8, "little") + encoded + payload)


def test_validate_safetensors_file_checks_offsets_shape_and_dtype(tmp_path):
    shard = tmp_path / "model.safetensors"
    write_safetensors(
        shard,
        {"weight": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]}},
        b"12345678",
    )

    result = validate_safetensors_file(shard)

    assert result.tensor_count == 1
    assert result.tensor_bytes == 8
    assert result.tensor_names == {"weight"}


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        (0).to_bytes(8, "little"),
        (10).to_bytes(8, "little") + b"{}",
    ],
)
def test_validate_safetensors_file_rejects_invalid_headers(tmp_path, payload):
    shard = tmp_path / "bad.safetensors"
    shard.write_bytes(payload)

    with pytest.raises(ValueError):
        validate_safetensors_file(shard)


def test_validate_safetensors_file_rejects_declared_header_larger_than_file(tmp_path, monkeypatch):
    shard = tmp_path / "bad.safetensors"
    shard.write_bytes(b"stub")
    monkeypatch.setattr(audit_module, "read_safetensors_header", lambda path: ({}, 10, 10))

    with pytest.raises(ValueError, match="smaller than its declared"):
        validate_safetensors_file(shard)


@pytest.mark.parametrize(
    "header,payload",
    [
        ({"w": []}, b""),
        ({"w": {"dtype": "F32", "shape": "bad", "data_offsets": [0, 0]}}, b""),
        ({"w": {"dtype": "F32", "shape": [1], "data_offsets": [0]}}, b""),
        ({"w": {"dtype": "F32", "shape": [1], "data_offsets": [-1, 0]}}, b""),
        ({"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}, b""),
        ({"w": {"dtype": "F32", "shape": [2], "data_offsets": [0, 4]}}, b"1234"),
        (
            {
                "a": {"dtype": "U8", "shape": [4], "data_offsets": [0, 4]},
                "b": {"dtype": "U8", "shape": [4], "data_offsets": [2, 6]},
            },
            b"123456",
        ),
    ],
)
def test_validate_safetensors_file_rejects_invalid_tensor_metadata(tmp_path, header, payload):
    shard = tmp_path / "bad.safetensors"
    write_safetensors(shard, header, payload)

    with pytest.raises(ValueError):
        validate_safetensors_file(shard)


def test_validate_safetensors_file_reports_nonfatal_warnings(tmp_path):
    shard = tmp_path / "warn.safetensors"
    write_safetensors(
        shard,
        {
            "a": {"dtype": "UNKNOWN", "shape": [1], "data_offsets": [0, 1]},
            "b": {"dtype": "U8", "shape": [1], "data_offsets": [2, 3]},
        },
        b"1234",
    )

    result = validate_safetensors_file(shard)

    assert result.tensor_count == 2
    assert len(result.warnings) == 3


def test_audit_model_validates_index_headers_and_config(tmp_path):
    (tmp_path / "config.json").write_text('{"model_type": "test"}', encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {"total_size": 8},
                "weight_map": {"weight": "model-00001-of-00001.safetensors"},
            }
        ),
        encoding="utf-8",
    )
    write_safetensors(
        tmp_path / "model-00001-of-00001.safetensors",
        {"weight": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]}},
        b"12345678",
    )

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is True
    assert result.checked_safetensors == 1
    assert result.indexed_tensors == 1


def test_audit_model_accepts_index_with_non_mapping_metadata(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"metadata": [], "weight_map": {"weight": "model.safetensors"}}),
        encoding="utf-8",
    )
    write_safetensors(
        tmp_path / "model.safetensors",
        {"weight": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}},
        b"x",
    )

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is True


def test_audit_model_validates_unindexed_safetensors_without_index(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    write_safetensors(
        tmp_path / "model.safetensors",
        {"weight": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}},
        b"x",
    )

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is True
    assert result.checked_safetensors == 1


def test_audit_model_fails_when_index_shard_is_missing(tmp_path):
    (tmp_path / "config.json").write_text('{"model_type": "test"}', encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {"total_size": 8},
                "weight_map": {"weight": "missing.safetensors"},
            }
        ),
        encoding="utf-8",
    )

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is False
    assert "missing.safetensors" in result.missing_files


def test_audit_model_reports_invalid_config_and_index(tmp_path):
    (tmp_path / "config.json").write_text("{not-json}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text('{"weight_map": {}}', encoding="utf-8")

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is False
    assert any("config.json" in failure for failure in result.failures)
    assert any("missing or empty weight_map" in failure for failure in result.failures)


def test_audit_model_reports_invalid_weight_map_entry(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"w": 123}}),
        encoding="utf-8",
    )

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is False
    assert any("not a file name" in failure for failure in result.failures)


def test_audit_model_strict_fails_on_unindexed_tensor_and_total_mismatch(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {"total_size": 99},
                "weight_map": {"indexed": "model.safetensors"},
            }
        ),
        encoding="utf-8",
    )
    write_safetensors(
        tmp_path / "model.safetensors",
        {
            "indexed": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]},
            "extra": {"dtype": "U8", "shape": [1], "data_offsets": [1, 2]},
        },
        b"12",
    )

    result = audit_model(tmp_path, skip_transformers=True, strict=True)

    assert result.ok is False
    assert any("unindexed" in failure for failure in result.failures)
    assert any("tensor byte total mismatch" in failure for failure in result.failures)


def test_audit_model_reports_missing_indexed_tensor_and_warns_on_extra_tensor(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"indexed": "model.safetensors"}}),
        encoding="utf-8",
    )
    write_safetensors(
        tmp_path / "model.safetensors",
        {"extra": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]}},
        b"x",
    )

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is False
    assert any("indexed tensor(s) missing" in failure for failure in result.failures)
    assert any("unindexed tensor(s)" in warning for warning in result.warnings)


def test_audit_model_reports_invalid_safetensors_file(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "broken.safetensors").write_bytes(b"not-valid")

    result = audit_model(tmp_path, skip_transformers=True)

    assert result.ok is False
    assert any("broken.safetensors" in failure for failure in result.failures)


def test_audit_model_can_use_mocked_transformers_metadata_loader(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    class FakeAutoConfig:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            return object()

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("no tokenizer")

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoConfig=FakeAutoConfig, AutoTokenizer=FakeAutoTokenizer),
    )

    result = audit_model(tmp_path, trust_remote_code=True)

    assert result.ok is True
    assert any("AutoTokenizer" in warning for warning in result.warnings)


def test_audit_model_warns_when_transformers_metadata_loader_fails(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    class FakeAutoConfig:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("bad config")

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoConfig=FakeAutoConfig, AutoTokenizer=object()),
    )

    result = audit_model(tmp_path)

    assert result.ok is True
    assert any("Transformers metadata audit" in warning for warning in result.warnings)
