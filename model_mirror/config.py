from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


DEFAULT_CONFIG_PATH = Path("~/.model-mirror.yaml").expanduser()
TOKEN_SETUP_HINT = "model-mirror config set token-path /path/to/huggingface/token"
REPO_TYPE_DIRS = {
    "model": "models",
    "dataset": "datasets",
    "space": "spaces",
}


@dataclass(slots=True)
class Config:
    directory: Path | None = None
    repo_type: str = "model"
    revision: str = "main"
    checksum: bool = True
    checksum_workers: int = 1
    verify_after_mirror: bool = True
    hf_xet_high_performance: bool = False
    hf_xet_reconstruct_write_sequentially: bool = False
    hf_xet_num_concurrent_range_gets: int | None = None
    token_path: Path | None = None
    cache_dir: Path | None = None
    tmp_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.directory is None:
            self.directory = Path.home() / ".local" / "share" / "model-mirror"
        self.directory = Path(self.directory).expanduser()
        if self.token_path is not None:
            self.token_path = Path(self.token_path).expanduser()
        if self.cache_dir is not None:
            self.cache_dir = Path(self.cache_dir).expanduser()
        if self.tmp_dir is not None:
            self.tmp_dir = Path(self.tmp_dir).expanduser()


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def load_config(path: Path | str | None = None, environ: Mapping[str, str] | None = None) -> Config:
    config_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    environ = os.environ if environ is None else environ
    data: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded:
            if not isinstance(loaded, dict):
                raise ValueError(f"Config file must contain a YAML mapping: {config_path}")
            data = loaded

    config = Config(
        directory=_path_or_none(data.get("directory")),
        repo_type=data.get("repo_type", "model"),
        revision=data.get("revision", "main"),
        checksum=parse_bool(data.get("checksum", True)),
        checksum_workers=parse_positive_int(data.get("checksum_workers", 1), default=1),
        verify_after_mirror=parse_bool(
            data.get("verify_after_mirror", data.get("audit_after_mirror", True))
        ),
        hf_xet_high_performance=parse_bool(data.get("hf_xet_high_performance", False)),
        hf_xet_reconstruct_write_sequentially=parse_bool(
            data.get("hf_xet_reconstruct_write_sequentially", False)
        ),
        hf_xet_num_concurrent_range_gets=parse_optional_positive_int(
            data.get("hf_xet_num_concurrent_range_gets")
        ),
        token_path=_path_or_none(data.get("token_path")),
        cache_dir=_path_or_none(data.get("cache_dir")),
        tmp_dir=_path_or_none(data.get("tmp_dir")),
    )

    if environ.get("MODEL_MIRROR_DIRECTORY"):
        config.directory = Path(environ["MODEL_MIRROR_DIRECTORY"]).expanduser()
    if environ.get("MODEL_MIRROR_REVISION"):
        config.revision = environ["MODEL_MIRROR_REVISION"]
    if environ.get("MODEL_MIRROR_REPO_TYPE"):
        config.repo_type = environ["MODEL_MIRROR_REPO_TYPE"]
    if environ.get("MODEL_MIRROR_TOKEN_PATH"):
        config.token_path = Path(environ["MODEL_MIRROR_TOKEN_PATH"]).expanduser()
    if environ.get("MODEL_MIRROR_HF_XET_HIGH_PERFORMANCE"):
        config.hf_xet_high_performance = parse_bool(environ["MODEL_MIRROR_HF_XET_HIGH_PERFORMANCE"])
    if environ.get("MODEL_MIRROR_HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"):
        config.hf_xet_reconstruct_write_sequentially = parse_bool(
            environ["MODEL_MIRROR_HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"]
        )
    if environ.get("MODEL_MIRROR_HF_XET_NUM_CONCURRENT_RANGE_GETS"):
        config.hf_xet_num_concurrent_range_gets = parse_positive_int(
            environ["MODEL_MIRROR_HF_XET_NUM_CONCURRENT_RANGE_GETS"], default=16
        )
    if environ.get("MODEL_MIRROR_CHECKSUM_WORKERS"):
        config.checksum_workers = parse_positive_int(environ["MODEL_MIRROR_CHECKSUM_WORKERS"], default=1)

    return config


def parse_positive_int(value: object, *, default: int) -> int:
    if value in {None, ""}:
        return default
    parsed = int(value)
    if parsed < 1:
        raise ValueError("value must be a positive integer")
    return parsed


def parse_optional_positive_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    return parse_positive_int(value, default=1)


def _path_or_none(value: object) -> Path | None:
    if value in {None, ""}:
        return None
    return Path(str(value)).expanduser()


def token_path_candidates(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    if env.get("HF_TOKEN_PATH"):
        candidates.append(Path(env["HF_TOKEN_PATH"]).expanduser())
    if env.get("MODEL_MIRROR_TOKEN_PATH"):
        candidates.append(Path(env["MODEL_MIRROR_TOKEN_PATH"]).expanduser())
    if env.get("HF_HOME"):
        candidates.append(Path(env["HF_HOME"]).expanduser() / "token")
    candidates.extend(
        [
            Path.home() / ".cache" / "huggingface" / "token",
            Path.home() / ".huggingface" / "token",
        ]
    )

    deduped: list[Path] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def detect_token_path(config: Config, environ: Mapping[str, str] | None = None) -> Path | None:
    if config.token_path is not None:
        return config.token_path
    for candidate in token_path_candidates(environ):
        if candidate.is_file():
            return candidate
    return None


def hf_token_available(environ: Mapping[str, str]) -> bool:
    if environ.get("HF_TOKEN"):
        return True
    token_path = environ.get("HF_TOKEN_PATH")
    if not token_path:
        return False
    try:
        path = Path(token_path).expanduser()
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def save_config(config: Config, path: Path | str | None = None) -> None:
    config_path = Path(path).expanduser() if path is not None else DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "directory": str(config.directory),
        "repo_type": config.repo_type,
        "revision": config.revision,
        "checksum": config.checksum,
        "checksum_workers": config.checksum_workers,
        "verify_after_mirror": config.verify_after_mirror,
        "hf_xet_high_performance": config.hf_xet_high_performance,
        "hf_xet_reconstruct_write_sequentially": config.hf_xet_reconstruct_write_sequentially,
    }
    if config.hf_xet_num_concurrent_range_gets is not None:
        data["hf_xet_num_concurrent_range_gets"] = config.hf_xet_num_concurrent_range_gets
    if config.token_path is not None:
        data["token_path"] = str(config.token_path)
    if config.cache_dir is not None:
        data["cache_dir"] = str(config.cache_dir)
    if config.tmp_dir is not None:
        data["tmp_dir"] = str(config.tmp_dir)
    config_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


def safe_repo_path(repo_id: str) -> Path:
    parts = repo_id.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid repository id for local path: {repo_id!r}")
    if any("\\" in part or "\x00" in part for part in parts):
        raise ValueError(f"Invalid repository id for local path: {repo_id!r}")
    return Path(*parts)


def archive_path(config: Config, repo_id: str, repo_type: str | None = None) -> Path:
    selected_type = repo_type or config.repo_type
    try:
        type_dir = REPO_TYPE_DIRS[selected_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported repo type: {selected_type!r}") from exc
    return Path(config.directory) / type_dir / safe_repo_path(repo_id)


def apply_hf_environment(config: Config, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    base_env = dict(os.environ if environ is None else environ)
    token_path = detect_token_path(config, base_env)
    env = dict(base_env)
    cache_dir = config.cache_dir or Path(config.directory) / ".cache"
    tmp_dir = config.tmp_dir or Path(config.directory) / "tmp"

    env["HF_HOME"] = str(cache_dir)
    env["HF_HUB_CACHE"] = str(cache_dir / "hub")
    env["HF_ASSETS_CACHE"] = str(cache_dir / "assets")
    env["HF_XET_CACHE"] = str(cache_dir / "xet")
    env["TRANSFORMERS_CACHE"] = str(cache_dir / "transformers")
    env["XDG_CACHE_HOME"] = str(cache_dir / "xdg")
    env["TMPDIR"] = str(tmp_dir)
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    if token_path is not None:
        env["HF_TOKEN_PATH"] = str(token_path)

    if config.hf_xet_high_performance:
        env["HF_XET_HIGH_PERFORMANCE"] = "1"
    else:
        env.pop("HF_XET_HIGH_PERFORMANCE", None)
    if config.hf_xet_reconstruct_write_sequentially:
        env["HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"] = "1"
    else:
        env.pop("HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY", None)
    if config.hf_xet_num_concurrent_range_gets is not None:
        env["HF_XET_NUM_CONCURRENT_RANGE_GETS"] = str(config.hf_xet_num_concurrent_range_gets)
    else:
        env.pop("HF_XET_NUM_CONCURRENT_RANGE_GETS", None)

    return env
