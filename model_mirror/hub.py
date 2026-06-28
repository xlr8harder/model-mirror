from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from .config import Config, apply_hf_environment


@dataclass(slots=True)
class HubFile:
    path: str
    size: int | None
    lfs_sha256: str | None = None


@dataclass(slots=True)
class HubSnapshot:
    repo_id: str
    repo_type: str
    requested_revision: str
    resolved_commit: str
    files: list[HubFile]


class HuggingFaceHub:
    def __init__(self, config: Config):
        self.config = config

    def files(self, repo_id: str, repo_type: str, revision: str) -> list[HubFile]:
        return self.snapshot(repo_id, repo_type, revision).files

    def snapshot(self, repo_id: str, repo_type: str, revision: str) -> HubSnapshot:
        with patch.dict(os.environ, apply_hf_environment(self.config), clear=False):
            from huggingface_hub import HfApi

            api = HfApi()
            info = api.repo_info(repo_id, repo_type=repo_type, revision=revision, files_metadata=True)
        return HubSnapshot(
            repo_id=repo_id,
            repo_type=repo_type,
            requested_revision=revision,
            resolved_commit=getattr(info, "sha", revision),
            files=[self._file_from_sibling(sibling) for sibling in getattr(info, "siblings", []) or []],
        )

    def snapshot_download(
        self,
        repo_id: str,
        repo_type: str,
        revision: str,
        local_dir: Path,
        allow_patterns: list[str] | None = None,
    ) -> Path:
        local_dir.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, apply_hf_environment(self.config), clear=False):
            from huggingface_hub import snapshot_download

            path = snapshot_download(
                repo_id=repo_id,
                repo_type=repo_type,
                revision=revision,
                local_dir=str(local_dir),
                allow_patterns=allow_patterns,
            )
        return Path(path)

    @staticmethod
    def _file_from_sibling(sibling) -> HubFile:
        lfs = getattr(sibling, "lfs", None)
        lfs_sha256 = getattr(lfs, "sha256", None) if lfs is not None else None
        return HubFile(
            path=getattr(sibling, "rfilename"),
            size=getattr(sibling, "size", None),
            lfs_sha256=lfs_sha256,
        )


def get_snapshot(hub, repo_id: str, repo_type: str, revision: str) -> HubSnapshot:
    if hasattr(hub, "snapshot"):
        return hub.snapshot(repo_id, repo_type, revision)
    files = hub.files(repo_id, repo_type, revision)
    return HubSnapshot(
        repo_id=repo_id,
        repo_type=repo_type,
        requested_revision=revision,
        resolved_commit=revision,
        files=files,
    )
