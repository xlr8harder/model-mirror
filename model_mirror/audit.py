from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


DTYPE_SIZES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "U16": 2,
    "I16": 2,
    "F16": 2,
    "BF16": 2,
    "U32": 4,
    "I32": 4,
    "F32": 4,
    "U64": 8,
    "I64": 8,
    "F64": 8,
}


@dataclass(slots=True)
class SafetensorsValidation:
    tensor_count: int
    tensor_bytes: int
    tensor_names: set[str]
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AuditResult:
    checked_safetensors: int = 0
    indexed_tensors: int = 0
    missing_files: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_files and not self.failures


def product(values: list[int]) -> int:
    result = 1
    for value in values:
        result *= value
    return result


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_safetensors_header(path: Path) -> tuple[dict, int, int]:
    with path.open("rb") as handle:
        size_bytes = handle.read(8)
        if len(size_bytes) != 8:
            raise ValueError("file is too small to contain a safetensors header")
        header_size = int.from_bytes(size_bytes, "little")
        if header_size <= 0:
            raise ValueError(f"invalid safetensors header size: {header_size}")
        header_bytes = handle.read(header_size)
        if len(header_bytes) != header_size:
            raise ValueError("file ended before the safetensors header was complete")
    return json.loads(header_bytes), header_size, path.stat().st_size


def validate_safetensors_file(path: Path) -> SafetensorsValidation:
    warnings = []
    header, header_size, file_size = read_safetensors_header(path)
    data_size = file_size - 8 - header_size
    if data_size < 0:
        raise ValueError("file is smaller than its declared safetensors header")

    tensors = {key: value for key, value in header.items() if key != "__metadata__"}
    intervals = []
    tensor_bytes = 0
    for tensor_name, spec in tensors.items():
        if not isinstance(spec, dict):
            raise ValueError(f"tensor {tensor_name!r} has invalid metadata")
        dtype = spec.get("dtype")
        shape = spec.get("shape")
        offsets = spec.get("data_offsets")
        if not isinstance(shape, list) or not all(isinstance(item, int) and item >= 0 for item in shape):
            raise ValueError(f"tensor {tensor_name!r} has invalid shape")
        if not isinstance(offsets, list) or len(offsets) != 2:
            raise ValueError(f"tensor {tensor_name!r} has invalid data_offsets")
        start, end = offsets
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
            raise ValueError(f"tensor {tensor_name!r} has invalid byte range")
        if end > data_size:
            raise ValueError(f"tensor {tensor_name!r} extends beyond file data")

        observed_bytes = end - start
        item_size = DTYPE_SIZES.get(dtype)
        if item_size is None:
            warnings.append(f"{path.name}: tensor {tensor_name!r} has unknown dtype {dtype!r}")
        else:
            expected_bytes = product(shape) * item_size
            if observed_bytes != expected_bytes:
                raise ValueError(
                    f"tensor {tensor_name!r} byte size mismatch: "
                    f"offsets={observed_bytes} shape*dtype={expected_bytes}"
                )
        tensor_bytes += observed_bytes
        intervals.append((start, end, tensor_name))

    intervals.sort()
    previous_end = 0
    for start, end, tensor_name in intervals:
        if start < previous_end:
            raise ValueError(f"tensor {tensor_name!r} overlaps previous tensor data")
        if start > previous_end:
            warnings.append(f"{path.name}: gap before tensor {tensor_name!r}")
        previous_end = end
    if previous_end < data_size:
        warnings.append(f"{path.name}: {data_size - previous_end} trailing byte(s) after tensor data")

    return SafetensorsValidation(
        tensor_count=len(tensors),
        tensor_bytes=tensor_bytes,
        tensor_names=set(tensors),
        warnings=warnings,
    )


def audit_model(
    root: Path,
    *,
    skip_transformers: bool = False,
    trust_remote_code: bool = False,
    strict: bool = False,
) -> AuditResult:
    result = AuditResult()
    config_path = root / "config.json"
    if not config_path.exists():
        result.missing_files.append("config.json")
    else:
        try:
            read_json(config_path)
        except Exception as exc:
            result.failures.append(f"config.json: {exc}")

    if not skip_transformers:
        try:
            from transformers import AutoConfig, AutoTokenizer

            AutoConfig.from_pretrained(root, local_files_only=True, trust_remote_code=trust_remote_code)
            try:
                AutoTokenizer.from_pretrained(root, local_files_only=True, trust_remote_code=trust_remote_code)
            except Exception as exc:
                result.warnings.append(f"AutoTokenizer metadata load failed: {exc}")
        except Exception as exc:
            result.warnings.append(f"Transformers metadata audit skipped/failed: {exc}")

    indexed_tensors: dict[str, str] = {}
    indexed_files: set[str] = set()
    expected_total_size: int | None = None
    for index_path in sorted(root.glob("*.safetensors.index.json")):
        try:
            index = read_json(index_path)
            weight_map = index.get("weight_map")
            metadata = index.get("metadata", {})
            if not isinstance(weight_map, dict) or not weight_map:
                raise ValueError("missing or empty weight_map")
            if expected_total_size is None and isinstance(metadata, dict):
                expected_total_size = metadata.get("total_size")
            for tensor_name, filename in weight_map.items():
                if not isinstance(filename, str):
                    raise ValueError(f"weight_map entry for {tensor_name!r} is not a file name")
                indexed_tensors[tensor_name] = filename
                indexed_files.add(filename)
        except Exception as exc:
            result.failures.append(f"{index_path.name}: {exc}")

    result.indexed_tensors = len(indexed_tensors)
    for filename in sorted(indexed_files):
        if not (root / filename).exists():
            result.missing_files.append(filename)

    actual_tensor_names = set()
    actual_tensor_bytes = 0
    for path in sorted(root.glob("*.safetensors")):
        try:
            validation = validate_safetensors_file(path)
            result.checked_safetensors += 1
            result.warnings.extend(validation.warnings)
            actual_tensor_names.update(validation.tensor_names)
            actual_tensor_bytes += validation.tensor_bytes

            if indexed_tensors:
                expected_for_shard = {
                    tensor for tensor, filename in indexed_tensors.items() if filename == path.name
                }
                missing = expected_for_shard - validation.tensor_names
                extra = validation.tensor_names - expected_for_shard
                if missing:
                    result.failures.append(f"{path.name}: {len(missing)} indexed tensor(s) missing")
                if extra:
                    message = f"{path.name}: {len(extra)} unindexed tensor(s)"
                    if strict:
                        result.failures.append(message)
                    else:
                        result.warnings.append(message)
        except Exception as exc:
            result.failures.append(f"{path.name}: {exc}")

    missing_from_headers = set(indexed_tensors) - actual_tensor_names
    if missing_from_headers:
        result.failures.append(f"{len(missing_from_headers)} indexed tensor(s) missing from safetensors headers")
    if expected_total_size is not None and actual_tensor_bytes != expected_total_size:
        result.failures.append(
            f"tensor byte total mismatch: index={expected_total_size} headers={actual_tensor_bytes}"
        )

    return result
