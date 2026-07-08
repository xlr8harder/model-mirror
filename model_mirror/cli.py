from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .audit import audit_model
from .card import download_card_assets
from .checksums import MANIFEST, iter_payload_files, write_checksums, verify_checksums
from .config import (
    Config,
    REPO_TYPE_DIRS,
    archive_path,
    load_config,
    parse_bool,
    parse_nonnegative_int,
    parse_positive_int,
    save_config,
)
from .hub import HuggingFaceHub, get_snapshot
from .lock import ModelBusyError, ModelLock, lock_label, read_active_lock
from .mirror import mirror
from .progress import ProgressEntry, ProgressSnapshot, progress_snapshot
from .repair import repair
from .state import (
    VerificationState,
    read_verification_state,
    state_from_results,
    verification_state_path,
    write_verification_state,
)
from .verify import RemoteVerifyResult, merge_checksum_result, verify_remote


CONFIG_OPTIONS = [
    (
        "directory",
        "MODEL_MIRROR_DIRECTORY",
        "Archive root. Repos are stored below models/, datasets/, or spaces/ under this directory.",
    ),
    ("repo_type", "MODEL_MIRROR_REPO_TYPE", "Default Hugging Face repo type: model, dataset, or space."),
    ("revision", "MODEL_MIRROR_REVISION", "Default revision to mirror or verify, usually main."),
    ("checksum", None, "Whether mirror/repair writes local hash manifest records."),
    (
        "checksum_workers",
        "MODEL_MIRROR_CHECKSUM_WORKERS",
        "Number of files to hash concurrently. Use 1 for HDD-friendly sequential reads.",
    ),
    (
        "download_workers",
        "MODEL_MIRROR_DOWNLOAD_WORKERS",
        "Number of files to download concurrently. Default 1 is HDD-friendly; increase for SSD/NVMe.",
    ),
    (
        "stall_timeout_seconds",
        "MODEL_MIRROR_STALL_TIMEOUT",
        "Abort and retry a file when no local byte progress occurs for this many seconds. "
        "Default 600; set 0 to disable.",
    ),
    (
        "stall_retries",
        "MODEL_MIRROR_STALL_RETRIES",
        "Number of stall-triggered resume retries per file before failing the mirror.",
    ),
    ("verify_after_mirror", None, "Whether mirror runs verification after download unless --no-verify is passed."),
    (
        "hf_xet_high_performance",
        "MODEL_MIRROR_HF_XET_HIGH_PERFORMANCE",
        "Sets HF_XET_HIGH_PERFORMANCE=1. Off by default; use only on high-bandwidth machines "
        "with fast disks and ample memory, typically 64 GB RAM or more.",
    ),
    (
        "hf_xet_reconstruct_write_sequentially",
        "MODEL_MIRROR_HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY",
        "Sets HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY=1 for HDD-friendly Xet reconstruction writes.",
    ),
    (
        "hf_xet_num_concurrent_range_gets",
        "MODEL_MIRROR_HF_XET_NUM_CONCURRENT_RANGE_GETS",
        "Sets HF_XET_NUM_CONCURRENT_RANGE_GETS. Leave unset to use Hugging Face defaults.",
    ),
    (
        "token_path",
        "MODEL_MIRROR_TOKEN_PATH",
        "Path to a Hugging Face token file. If unset, model-mirror checks HF_TOKEN_PATH, "
        "HF_HOME/token, ~/.cache/huggingface/token, and ~/.huggingface/token. Token contents are never printed.",
    ),
    ("cache_dir", None, "Overrides the Hugging Face cache root; defaults to DIRECTORY/.cache."),
    ("tmp_dir", None, "Overrides temporary file directory; defaults to DIRECTORY/.tmp."),
]


@dataclass(slots=True)
class CacheUsage:
    archive_cache: int
    archive_tmp: int
    mirror_cache: int

    @property
    def total(self) -> int:
        return self.archive_cache + self.archive_tmp + self.mirror_cache


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-mirror",
        description="Mirror Hugging Face repositories into local bulk storage and verify their integrity.",
        epilog="Run 'model-mirror COMMAND --help' for command-specific options.",
    )
    parser.add_argument("--config", help="path to config file; defaults to ~/.model-mirror.yaml")
    subparsers = parser.add_subparsers(dest="command")
    command_parsers: dict[str, argparse.ArgumentParser] = {}

    def add_command_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        command_parser = subparsers.add_parser(name, **kwargs)
        command_parsers[name] = command_parser
        return command_parser

    mirror_parser = add_command_parser(
        "mirror",
        help="mirror a Hugging Face repo",
        description="Download a repo at a resolved Hub commit and verify it unless --no-verify is used.",
        epilog="Exit status: 0 when complete or downloaded cleanly; 1 when final verification is not clean.",
    )
    mirror_parser.add_argument("model", metavar="repo", help="Hugging Face repo id, e.g. org/model")
    mirror_parser.add_argument("--repo-type", choices=["model", "dataset", "space"], help="repo kind to mirror")
    add_revision_options(mirror_parser)
    mirror_parser.add_argument("--force", action="store_true", help="download even if the local copy looks complete")
    mirror_parser.add_argument("--no-verify", action="store_true", help="skip verification after download")
    mirror_parser.add_argument(
        "--stall-timeout",
        type=parse_nonnegative_int_arg,
        metavar="SECONDS",
        help="override stall timeout for this mirror; default 600 seconds, 0 disables timeout",
    )

    card_parser = add_command_parser(
        "card",
        help="download a repo card and local assets",
        description="Download README.md plus repository-local images referenced by the README.",
        epilog="Exit status: 0 when card files are downloaded; 1 when README.md is missing.",
    )
    card_parser.add_argument("repo", help="Hugging Face repo id, e.g. org/model")
    card_parser.add_argument("--repo-type", choices=["model", "dataset", "space"], help="repo kind to download")
    add_revision_options(card_parser)

    verify_parser = add_command_parser(
        "verify",
        help="verify mirrored archives",
        description="Check local files against Hub metadata, local checksums, and model metadata.",
        epilog=(
            "Exit status: 0 when verification is clean; 1 when files are missing, corrupt, extra, busy, "
            "the upstream repository is unavailable, or cached verification data is missing/stale; "
            "2 for command-line errors."
        ),
    )
    verify_parser.add_argument("model", metavar="repo", nargs="?", help="repo id to verify unless --all is used")
    verify_parser.add_argument("--cached", action="store_true", help="verify Hub hashes from cached .manifest rows")
    verify_parser.add_argument("--all", action="store_true", help="verify every mirrored model")
    verify_parser.add_argument(
        "--repo-type", choices=["model", "dataset", "space"], default="model", help="repo kind to verify"
    )
    add_revision_options(verify_parser)
    verify_parser.add_argument("--strict", action="store_true", help="fail on extra local files")
    verify_parser.add_argument("--max-age", help="with --all, skip clean archives verified within this age, e.g. 7d")
    verify_parser.add_argument("--offline", action="store_true", help="verify only against local checksum state")

    repair_parser = add_command_parser(
        "repair",
        help="repair a mirrored archive",
        description=(
            "Redownload files listed in existing .verification repair paths, "
            "then run a final verification. Run verify first."
        ),
        epilog=(
            "Exit status: 0 when complete, repaired, or updated cleanly; 1 when verification state is missing, "
            "repair is incomplete, cached verification data is incomplete, or a model is busy; "
            "2 for command-line errors."
        ),
    )
    repair_parser.add_argument("model", metavar="repo", nargs="?", help="repo id to repair unless --all is used")
    repair_parser.add_argument("--all", action="store_true", help="repair every mirrored model with verification state")
    repair_parser.add_argument(
        "--update",
        action="store_true",
        help="apply upstream commit changes recorded by verify before repairing",
    )
    repair_parser.add_argument(
        "--force-partial",
        action="store_true",
        help=(
            "attempt repair even when cached verification data for untouched files is incomplete; "
            "may leave the repository inconsistent"
        ),
    )
    repair_parser.add_argument(
        "--repo-type", choices=["model", "dataset", "space"], default="model", help="repo kind to repair"
    )

    offline_parser = add_command_parser(
        "offline",
        help="mark a mirror offline-only",
        description=(
            "Disable Hub checks for one mirrored repo. Use this when the upstream repo is gone "
            "and you want verification to use local state only."
        ),
        epilog="Exit status: 0 when the mirror is marked offline-only; 1 when local verification state is missing or busy.",
    )
    offline_parser.add_argument("model", metavar="repo", help="repo id to mark offline-only")
    offline_parser.add_argument(
        "--repo-type", choices=["model", "dataset", "space"], default="model", help="repo kind to update"
    )

    online_parser = add_command_parser(
        "online",
        help="re-enable Hub checks for a mirror",
        description="Clear offline-only mode so verify and repair contact the Hub again.",
        epilog="Exit status: 0 when the mirror is marked online; 1 when local verification state is missing or busy.",
    )
    online_parser.add_argument("model", metavar="repo", help="repo id to mark online")
    online_parser.add_argument(
        "--repo-type", choices=["model", "dataset", "space"], default="model", help="repo kind to update"
    )

    add_command_parser("list", help="list mirrored models", description="Show mirrored models and verification age.")
    add_command_parser("status", help="show archive status", description="Show mirrors, verification age, and cache usage.")

    clean_cache_parser = add_command_parser(
        "clean-cache",
        help="remove Hugging Face cache and temporary files",
        description=(
            "Remove archive cache, temporary files, and per-mirror Hugging Face metadata caches "
            "without touching mirrored payload files. Without --force, only reports reclaimable space."
        ),
        epilog="Exit status: 0 when cleanup succeeds; 1 when a configured cleanup target is unsafe.",
    )
    clean_cache_parser.add_argument("--force", action="store_true", help="delete cache and temporary files")

    config_parser = add_command_parser(
        "config",
        help="show or change configuration",
        description="Print configuration, describe supported keys, or persist configuration changes.",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser("show", help="print the resolved configuration")
    config_subparsers.add_parser("options", help="print supported configuration keys with descriptions")
    directory_parser = config_subparsers.add_parser("directory", help="get or set archive directory")
    directory_parser.add_argument("path", nargs="?", help="new archive directory; omit to print current value")
    set_parser = config_subparsers.add_parser("set", help="set a supported configuration key")
    set_parser.add_argument("key", help="configuration key; see 'model-mirror config options'")
    set_parser.add_argument("value", help="new value")

    help_parser = add_command_parser(
        "help",
        help="show help",
        description="Show full help or command-specific help.",
    )
    help_parser.add_argument("topic", nargs="?", help="optional command to show help for")
    parser.command_parsers = command_parsers

    return parser


def add_revision_options(parser: argparse.ArgumentParser) -> None:
    revision_group = parser.add_mutually_exclusive_group()
    revision_group.add_argument("--revision", help="branch, tag, or commit to use; defaults to config revision")
    revision_group.add_argument("--commit", help="commit SHA to use")


def parse_nonnegative_int_arg(value: str) -> int:
    try:
        return parse_nonnegative_int(value, default=0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def selected_revision_arg(args) -> str | None:
    return args.commit or args.revision


def main(argv: list[str] | None = None, *, hub=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "help":
        return handle_help(parser, args.topic)
    config_path = Path(args.config).expanduser() if args.config else None
    config = load_config(config_path)

    if should_supervise_mirror(args, config, hub):
        return run_supervised_mirror(raw_argv, args, config)

    try:
        if args.command == "config":
            return handle_config(args, config, config_path)
        if args.command in {"list", "status"}:
            return handle_list(config)
        if args.command == "clean-cache":
            return handle_clean_cache(config, force=args.force)
        if args.command == "mirror":
            selected_hub = hub or HuggingFaceHub(config)
            result = mirror(
                config,
                args.model,
                hub=selected_hub,
                repo_type=args.repo_type,
                revision=selected_revision_arg(args),
                force=args.force,
                verify_after=config.verify_after_mirror and not args.no_verify,
                stall_timeout_seconds=args.stall_timeout,
            )
            print(f"{result.status}: {args.model} -> {result.path}")
            return mirror_exit_code(result.status)
        if args.command == "card":
            selected_hub = hub or HuggingFaceHub(config)
            result = download_card_assets(
                config,
                args.repo,
                hub=selected_hub,
                repo_type=args.repo_type,
                revision=selected_revision_arg(args),
            )
            print(f"{result.status}: {args.repo} -> {result.path} ({len(result.paths)} files)")
            return 0 if result.status == "downloaded" else 1
        if args.command == "verify":
            return handle_verify(args, config, hub=hub)
        if args.command == "repair":
            return handle_repair(args, config, hub=hub)
        if args.command == "offline":
            return handle_offline_mode(args, config, offline_only=True)
        if args.command == "online":
            return handle_offline_mode(args, config, offline_only=False)
    except ModelBusyError as exc:
        print(str(exc))
        return 1

    parser.error(f"Unhandled command: {args.command}")
    return 2


def should_supervise_mirror(args, config: Config, hub) -> bool:
    if args.command != "mirror":
        return False
    if hub is not None:
        return False
    if os.environ.get("MODEL_MIRROR_SUPERVISED_CHILD") == "1":
        return False
    return effective_stall_timeout(args, config) > 0


def effective_stall_timeout(args, config: Config) -> int:
    selected = getattr(args, "stall_timeout", None)
    return config.stall_timeout_seconds if selected is None else selected


def run_supervised_mirror(raw_argv: list[str], args, config: Config) -> int:
    repo_type = args.repo_type or config.repo_type
    root = archive_path(config, args.model, repo_type)
    timeout = effective_stall_timeout(args, config)
    retry_limit = config.stall_retries
    retry_counts: dict[str, int] = {}
    command = [sys.executable, "-m", "model_mirror.cli", *raw_argv]
    env = dict(os.environ)
    env["MODEL_MIRROR_SUPERVISED_CHILD"] = "1"

    while True:
        child = subprocess.Popen(command, env=env)
        restart, returncode = supervise_child(child, root, timeout, retry_limit, retry_counts)
        if restart:
            continue
        return returncode


def supervise_child(
    child,
    root: Path,
    timeout: int,
    retry_limit: int,
    retry_counts: dict[str, int],
) -> tuple[bool, int]:
    started = time.monotonic()
    started_at_utc = datetime.now(timezone.utc)
    poll_interval = min(30.0, max(1.0, timeout / 10))
    while True:
        returncode = child.poll()
        if returncode is not None:
            return False, returncode

        snapshot = progress_snapshot(root, stall_timeout_seconds=timeout)
        fresh_entries = fresh_progress_entries(snapshot.entries, started_at_utc)
        stalled_entries = [entry for entry in fresh_entries if entry.stalled]
        if stalled_entries:
            entry = selected_progress_entry(stalled_entries)
            path = entry.path
            retry_counts[path] = retry_counts.get(path, 0) + 1
            if retry_counts[path] > retry_limit:
                print(
                    f"stall retry limit exceeded for {path}: "
                    f"{retry_counts[path] - 1}/{retry_limit}; terminating mirror child"
                )
                terminate_child(child)
                return False, 1
            print(
                f"stall detected for {path}: idle={format_age_seconds(entry.idle_seconds)}; "
                f"restarting mirror child ({retry_counts[path]}/{retry_limit})"
            )
            terminate_child(child)
            return True, 0

        if not fresh_entries and time.monotonic() - started >= timeout:
            retry_counts["<no-progress>"] = retry_counts.get("<no-progress>", 0) + 1
            if retry_counts["<no-progress>"] > retry_limit:
                print("stall retry limit exceeded before progress was reported; terminating mirror child")
                terminate_child(child)
                return False, 1
            print(
                "stall detected before progress was reported; "
                f"restarting mirror child ({retry_counts['<no-progress>']}/{retry_limit})"
            )
            terminate_child(child)
            return True, 0

        time.sleep(poll_interval)


def fresh_progress_entries(entries: list[ProgressEntry], started_at_utc: datetime) -> list[ProgressEntry]:
    return [entry for entry in entries if progress_entry_updated_after(entry, started_at_utc)]


def progress_entry_updated_after(entry: ProgressEntry, cutoff: datetime) -> bool:
    try:
        updated = datetime.fromisoformat(entry.updated_at_utc)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return updated >= cutoff


def terminate_child(child) -> None:
    child.terminate()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=30)


def handle_help(parser: argparse.ArgumentParser, topic: str | None) -> int:
    if topic is None:
        parser.print_help()
        return 0
    command_parser = parser.command_parsers.get(topic)
    if command_parser is None:
        parser.error(f"Unknown help topic: {topic}")
    command_parser.print_help()
    return 0


def mirror_exit_code(status: str) -> int:
    return 0 if status in {"complete", "downloaded"} else 1


def handle_config(args, config: Config, config_path: Path | None) -> int:
    if args.config_command in {None, "options"}:
        return handle_config_options(config)

    if args.config_command == "show":
        print(f"directory: {config.directory}")
        print(f"repo_type: {config.repo_type}")
        print(f"revision: {config.revision}")
        print(f"checksum: {config.checksum}")
        print(f"checksum_workers: {config.checksum_workers}")
        print(f"download_workers: {config.download_workers}")
        print(f"stall_timeout_seconds: {config.stall_timeout_seconds}")
        print(f"stall_retries: {config.stall_retries}")
        print(f"verify_after_mirror: {config.verify_after_mirror}")
        print(f"hf_xet_high_performance: {config.hf_xet_high_performance}")
        print(f"hf_xet_reconstruct_write_sequentially: {config.hf_xet_reconstruct_write_sequentially}")
        if config.hf_xet_num_concurrent_range_gets is not None:
            print(f"hf_xet_num_concurrent_range_gets: {config.hf_xet_num_concurrent_range_gets}")
        if config.token_path:
            print(f"token_path: {config.token_path}")
        return 0

    if args.config_command == "directory":
        if args.path is None:
            print(config.directory)
            return 0
        config.directory = Path(args.path).expanduser()
        config.directory.mkdir(parents=True, exist_ok=True)
        save_config(config, config_path)
        print(f"directory: {config.directory}")
        return 0

    if args.config_command == "set":
        set_config_value(config, args.key, args.value)
        save_config(config, config_path)
        print(f"{args.key}: {args.value}")
        return 0

    return 2


def handle_config_options(config: Config) -> int:
    values = {
        "directory": config.directory,
        "repo_type": config.repo_type,
        "revision": config.revision,
        "checksum": config.checksum,
        "checksum_workers": config.checksum_workers,
        "download_workers": config.download_workers,
        "stall_timeout_seconds": config.stall_timeout_seconds,
        "stall_retries": config.stall_retries,
        "verify_after_mirror": config.verify_after_mirror,
        "hf_xet_high_performance": config.hf_xet_high_performance,
        "hf_xet_reconstruct_write_sequentially": config.hf_xet_reconstruct_write_sequentially,
        "hf_xet_num_concurrent_range_gets": config.hf_xet_num_concurrent_range_gets,
        "token_path": config.token_path,
        "cache_dir": config.cache_dir,
        "tmp_dir": config.tmp_dir,
    }
    for key, env_var, description in CONFIG_OPTIONS:
        print(f"{key}: {values[key]}")
        if env_var:
            print(f"  env: {env_var}")
        print(f"  {description}")
    return 0


def set_config_value(config: Config, key: str, value: str) -> None:
    normalized = key.replace("-", "_")
    if normalized == "directory":
        config.directory = Path(value).expanduser()
    elif normalized == "repo_type":
        config.repo_type = value
    elif normalized == "revision":
        config.revision = value
    elif normalized == "checksum":
        config.checksum = parse_bool(value)
    elif normalized == "checksum_workers":
        config.checksum_workers = parse_positive_int(value, default=1)
    elif normalized == "download_workers":
        config.download_workers = parse_positive_int(value, default=1)
    elif normalized in {"stall_timeout", "stall_timeout_seconds"}:
        config.stall_timeout_seconds = parse_nonnegative_int(value, default=600)
    elif normalized == "stall_retries":
        config.stall_retries = parse_nonnegative_int(value, default=3)
    elif normalized in {"verify_after_mirror", "audit_after_mirror"}:
        config.verify_after_mirror = parse_bool(value)
    elif normalized == "hf_xet_high_performance":
        config.hf_xet_high_performance = parse_bool(value)
    elif normalized == "hf_xet_reconstruct_write_sequentially":
        config.hf_xet_reconstruct_write_sequentially = parse_bool(value)
    elif normalized == "hf_xet_num_concurrent_range_gets":
        config.hf_xet_num_concurrent_range_gets = parse_positive_int(value, default=16)
    elif normalized == "token_path":
        config.token_path = Path(value).expanduser()
    elif normalized == "cache_dir":
        config.cache_dir = Path(value).expanduser()
    elif normalized == "tmp_dir":
        config.tmp_dir = Path(value).expanduser()
    else:
        raise SystemExit(f"Unsupported config key: {key}")


def handle_list(config: Config) -> int:
    archive_root = Path(config.directory)
    print(f"archive root: {archive_root}")
    for type_dir in REPO_TYPE_DIRS.values():
        print(f"{type_dir} root: {archive_root / type_dir}")

    entries = []
    for type_dir in REPO_TYPE_DIRS.values():
        repo_root = archive_root / type_dir
        if not repo_root.exists():
            continue
        for owner in sorted(path for path in repo_root.iterdir() if path.is_dir()):
            for repo in sorted(path for path in owner.iterdir() if path.is_dir()):
                state = read_verification_state(repo)
                active_lock = read_active_lock(repo)
                total_size = mirror_payload_size(repo)
                progress = progress_snapshot(repo, stall_timeout_seconds=config.stall_timeout_seconds)
                entries.append((repo, state, active_lock, total_size, progress))
    entries.sort(key=lambda entry: entry[0].relative_to(archive_root).as_posix())

    total_size = sum(entry[3] for entry in entries)
    cache_usage = archive_cache_usage(config)
    print(f"mirrors: {len(entries)}  total_size={format_bytes(total_size)}")
    print(
        "cache: "
        f"total={format_bytes(cache_usage.total)}  "
        f"archive={format_bytes(cache_usage.archive_cache)}  "
        f"tmp={format_bytes(cache_usage.archive_tmp)}  "
        f"mirror_metadata={format_bytes(cache_usage.mirror_cache)}"
    )

    for model, state, active_lock, total_size, progress in entries:
        rel_path = model.relative_to(archive_root).as_posix()
        fields = [
            rel_path,
            f"size={format_bytes(total_size)}",
        ]
        if state is not None:
            fields.extend(
                [
                    f"state={','.join(list_state_tags(state, active_lock, progress))}",
                    f"last_check={verification_age_label(state.checked_at_utc)}",
                ]
            )
        else:
            fields.extend(["state=unverified", "last_check=unknown"])
        print("  ".join(fields))
        if active_lock is not None:
            print(f"  lock: {format_lock_detail(active_lock)}")
        progress_detail = format_progress_detail(progress)
        if progress_detail is not None:
            print(f"  progress: {progress_detail}")
    return 0


def archive_cache_usage(config: Config) -> CacheUsage:
    archive_root = Path(config.directory)
    cache_root = Path(config.cache_dir) if config.cache_dir is not None else archive_root / ".cache"
    tmp_root = Path(config.tmp_dir) if config.tmp_dir is not None else archive_root / ".tmp"
    return CacheUsage(
        archive_cache=directory_size(cache_root),
        archive_tmp=directory_size(tmp_root),
        mirror_cache=sum(directory_size(path) for path in mirror_cache_dirs(archive_root)),
    )


def handle_clean_cache(config: Config, *, force: bool) -> int:
    archive_root = Path(config.directory)
    targets = cleanup_targets(config)
    unsafe = [path for path, label in targets if not is_safe_cleanup_target(path, archive_root, label)]
    if unsafe:
        for path in unsafe:
            print(f"refusing unsafe cleanup target: {path}")
        return 1

    sized_targets = [(path, label, directory_size(path)) for path, label in targets if path.exists()]
    total_size = sum(size for _path, _label, size in sized_targets)
    print(f"cleanup mode: {'force' if force else 'dry-run'}")
    print(f"reclaimable={format_bytes(total_size)}")
    for path, label, size in sized_targets:
        if force:
            shutil.rmtree(path)
            action = "removed"
        else:
            action = "would-remove"
        print(f"{action}: {label} {path} size={format_bytes(size)}")
    return 0


def cleanup_targets(config: Config) -> list[tuple[Path, str]]:
    archive_root = Path(config.directory)
    cache_root = Path(config.cache_dir) if config.cache_dir is not None else archive_root / ".cache"
    tmp_root = Path(config.tmp_dir) if config.tmp_dir is not None else archive_root / ".tmp"
    targets = [(cache_root, "archive-cache"), (tmp_root, "archive-tmp")]
    targets.extend((path, "mirror-cache") for path in mirror_cache_dirs(archive_root))
    return targets


def mirror_cache_dirs(archive_root: Path) -> list[Path]:
    targets: list[Path] = []
    for type_dir in REPO_TYPE_DIRS.values():
        for repo in sorted((archive_root / type_dir).glob("*/*")):
            targets.append(repo / ".cache")
    return targets


def is_safe_cleanup_target(path: Path, archive_root: Path, label: str) -> bool:
    resolved_path = path.resolve(strict=False)
    resolved_archive = archive_root.resolve(strict=False)
    protected = {resolved_archive, *(resolved_archive / type_dir for type_dir in REPO_TYPE_DIRS.values())}
    expected_name = ".tmp" if label == "archive-tmp" else ".cache"
    return (
        resolved_path.name == expected_name
        and resolved_path.is_relative_to(resolved_archive)
        and resolved_path not in protected
    )


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total_size = 0
    for child in path.rglob("*"):
        if child.is_file():
            total_size += child.stat().st_size
    return total_size


def mirror_payload_size(root: Path) -> int:
    total_size = 0
    for path in iter_payload_files(root):
        total_size += path.stat().st_size
    return total_size


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):  # pragma: no branch
        if abs(value) < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024


def format_lock_detail(info: dict | None) -> str:
    if not info:
        return "lock held"
    parts = []
    for key in ("command", "pid", "host", "started_at_utc"):
        value = info.get(key)
        if value:
            parts.append(f"{key}={value}")
    return " ".join(parts) if parts else "lock held"


def format_progress_detail(progress: ProgressSnapshot) -> str | None:
    if not progress.active:
        return None
    entry = selected_progress_entry(progress.entries)
    parts = [
        f"active={len(progress.entries)}",
        f"path={shorten_path(entry.path)}",
        f"stage={entry.stage}",
        f"bytes={format_progress_bytes(entry)}",
    ]
    rate = format_rate(entry.rate_bytes_per_second)
    if rate is not None:
        parts.append(f"rate={rate}")
    if entry.idle_seconds is not None:
        parts.append(f"idle={format_age_seconds(entry.idle_seconds)}")
    if progress.stalled_count:
        parts.append(f"stalled={progress.stalled_count}")
    if entry.source != "heartbeat":
        parts.append(f"source={entry.source}")
    return " ".join(parts)


def selected_progress_entry(entries: list[ProgressEntry]) -> ProgressEntry:
    stalled = [entry for entry in entries if entry.stalled]
    if stalled:
        return max(stalled, key=lambda entry: entry.idle_seconds or 0)
    return sorted(entries, key=lambda entry: entry.path)[0]


def format_progress_bytes(entry: ProgressEntry) -> str:
    if entry.bytes_total:
        percent = (entry.bytes_done / entry.bytes_total) * 100
        return f"{format_bytes(entry.bytes_done)}/{format_bytes(entry.bytes_total)}({percent:.1f}%)"
    return format_bytes(entry.bytes_done)


def format_rate(rate: float | None) -> str | None:
    if rate is None:
        return None
    return f"{format_bytes(int(rate))}/s"


def shorten_path(path: str, *, max_length: int = 48) -> str:
    if len(path) <= max_length:
        return path
    return f"...{path[-(max_length - 3):]}"


def list_state_tags(state, active_lock: dict | None, progress: ProgressSnapshot | None = None) -> list[str]:
    tags = [primary_state_tag(state)]
    if state.offline_only:
        append_unique(tags, "offline")
    if state.upstream_status == "changed":
        append_unique(tags, "upstream-changed")
    if state_has_upstream_unavailable(state):
        append_unique(tags, "upstream-unavailable")
    if active_lock is not None:
        append_unique(tags, "busy")
    if progress is not None and progress.any_stalled:
        append_unique(tags, "stalled")
    return tags


def primary_state_tag(state) -> str:
    if state.status == "unavailable":
        return "upstream-unavailable"
    if state.status == "incomplete":
        if state_has_manifest_incomplete(state):
            return "manifest-incomplete"
        return "incomplete"
    if state.status == "dirty":
        if state.repair_paths:
            return "needs-repair"
        if any(str(issue) == "verification skipped" for issue in state.issues):
            return "unverified"
        return "dirty"
    if state.status == "in_progress":
        return "in-progress"
    return state.status or "unknown"


def append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def state_has_manifest_incomplete(state) -> bool:
    return state_has_cached_hash_missing(state) or any(".manifest missing" in str(issue) for issue in state.issues)


def handle_verify(args, config: Config, *, hub=None) -> int:
    if args.all:
        failures = 0
        changed = 0
        repair_needed = 0
        cache_incomplete = 0
        for repo_id in list_model_ids(config):
            if args.max_age and should_skip_recent_clean(config, repo_id, args.repo_type, args.max_age):
                print(f"skipped recent clean verification: {repo_id}")
                continue
            try:
                rc = verify_one(config, repo_id, args, hub=hub)
            except ModelBusyError as exc:
                print(f"skipped busy: {repo_id} -> {exc.root} ({lock_label(exc.info)})")
                failures += 1
                continue
            state = read_verification_state(archive_path(config, repo_id, args.repo_type))
            if state is not None and state.upstream_status == "changed":
                changed += 1
            if rc != 0:
                failures += 1
                if state is not None and state.repair_paths:
                    repair_needed += 1
                if state is not None and state_has_cached_hash_missing(state):
                    cache_incomplete += 1
        if repair_needed:
            print("next: model-mirror repair --all")
        if cache_incomplete:
            print("cached verification incomplete: run full verification with model-mirror verify --all")
        if changed:
            print("update changed upstreams: model-mirror repair --all --update")
        return 1 if failures else 0

    if not args.model:
        raise SystemExit("verify requires a model id unless --all is used")
    return verify_one(config, args.model, args, hub=hub)


def handle_repair(args, config: Config, *, hub=None) -> int:
    if args.all and args.model:
        raise SystemExit("repair accepts a model id or --all, not both")
    if args.all:
        failures = 0
        for repo_id in list_model_ids(config):
            state = read_verification_state(archive_path(config, repo_id, args.repo_type))
            if state is not None and state.offline_only:
                print(f"skipped offline-only: {repo_id}; repair requires an upstream repository")
                continue
            try:
                rc = repair_one(config, repo_id, args, hub=hub)
            except ModelBusyError as exc:
                print(f"skipped busy: {repo_id} -> {exc.root} ({lock_label(exc.info)})")
                failures += 1
                continue
            if rc != 0:
                failures += 1
        return 1 if failures else 0

    if not args.model:
        raise SystemExit("repair requires a model id unless --all is used")
    return repair_one(config, args.model, args, hub=hub)


def state_has_cached_hash_missing(state) -> bool:
    return any(str(issue).startswith("cached_hash_missing:") for issue in state.issues)


def state_has_upstream_unavailable(state) -> bool:
    return state.status == "unavailable" or any(
        str(issue).startswith("upstream unavailable:") for issue in state.issues
    )


def repair_one(config: Config, repo_id: str, args, *, hub=None) -> int:
    selected_hub = hub or HuggingFaceHub(config)
    print_verification_age(config, repo_id, args.repo_type)
    if args.force_partial:
        print(
            "warning: --force-partial can leave the repository inconsistent when verification data is incomplete"
        )
    result = repair(
        config,
        repo_id,
        hub=selected_hub,
        repo_type=args.repo_type,
        update=args.update,
        force_partial=args.force_partial,
    )
    if result.status == "verify-required":
        print(f"run verify first: model-mirror verify {repo_id}")
    if result.status == "verification-incomplete":
        print(
            f"could not fully repair {repo_id}; missing verification data for some files. "
            f"Run full verify and repair again: model-mirror verify {repo_id} && model-mirror repair {repo_id}"
        )
    if result.status == "offline-only":
        print(
            f"cannot repair offline-only model {repo_id}; upstream link is disabled. "
            f"Run model-mirror online {repo_id} to re-enable Hub-backed repair."
        )
    print_repair_commit_notice(repo_id, result)
    print(f"{result.status}: {repo_id} -> {result.path}")
    return 0 if result.status in {"complete", "repaired", "updated"} else 1


def verify_one(config: Config, repo_id: str, args, *, hub=None) -> int:
    selected_hub = hub or HuggingFaceHub(config)
    root = archive_path(config, repo_id, args.repo_type)
    with ModelLock(root, "verify", repo_id, args.repo_type):
        return verify_one_locked(config, repo_id, args, selected_hub, root)


def verify_one_locked(config: Config, repo_id: str, args, selected_hub, root: Path) -> int:
    existing_state = read_verification_state(root)
    requested_revision = selected_revision_arg(args) or (
        existing_state.requested_revision if existing_state else config.revision
    )

    if args.offline:
        return verify_one_offline(config, root, repo_id, args, existing_state, mode="offline")
    if existing_state is not None and existing_state.offline_only:
        return verify_one_offline(config, root, repo_id, args, existing_state, mode="offline-only")

    try:
        upstream_snapshot = get_snapshot(selected_hub, repo_id, args.repo_type, requested_revision)
    except Exception as exc:
        return handle_upstream_unavailable(root, repo_id, args, existing_state, requested_revision, exc)
    resolved_commit = existing_state.resolved_commit if existing_state and existing_state.resolved_commit else upstream_snapshot.resolved_commit
    try:
        snapshot = upstream_snapshot if resolved_commit == upstream_snapshot.resolved_commit else get_snapshot(
            selected_hub, repo_id, args.repo_type, resolved_commit
        )
    except Exception as exc:
        return handle_upstream_unavailable(root, repo_id, args, existing_state, requested_revision, exc)
    metadata = snapshot.files
    checksum_result = None
    manifest_verified = False
    if not args.cached and config.checksum:
        if (root / MANIFEST).exists():
            checksum_result = verify_checksums(root, strict=args.strict)
            if checksum_result.ok:
                write_checksums(root, max_workers=config.checksum_workers)
                manifest_verified = True
        else:
            write_checksums(root, max_workers=config.checksum_workers)
            manifest_verified = True
    from_manifest = args.cached or manifest_verified
    result = verify_remote(
        root,
        metadata,
        cached=args.cached,
        from_manifest=from_manifest,
        strict=args.strict,
    )
    if checksum_result is not None:
        merge_checksum_result(result, checksum_result)
    if args.repo_type == "model":
        audit = audit_model(root, skip_transformers=True)
    else:
        audit = None
    state = state_from_results(
        repo_id,
        args.repo_type,
        requested_revision,
        result,
        audit,
        resolved_commit=resolved_commit,
        upstream_commit=upstream_snapshot.resolved_commit,
    )
    write_verification_state(root, state)

    if state.status == "incomplete":
        print(f"cached verification incomplete: {repo_id}{upstream_change_suffix(state)}")
        print(f"run full verification: model-mirror verify {repo_id}")
        if state.upstream_status == "changed":
            print_update_next_step(repo_id)
        return 1
    if audit is not None and not audit.ok:
        print(f"verification failed: {repo_id}{upstream_change_suffix(state)}")
        print_verification_next_steps(repo_id, state)
        return 1
    if result.ok:
        mode = "cached" if args.cached else "full"
        print(f"verified ({mode}): {repo_id}{upstream_change_suffix(state)}")
        if state.upstream_status == "changed":
            print_update_next_step(repo_id)
        return 0
    print(f"verification failed: {repo_id}{upstream_change_suffix(state)}")
    print_verification_next_steps(repo_id, state)
    return 1


def upstream_change_suffix(state) -> str:
    return " upstream=changed" if state.upstream_status == "changed" else ""


def print_verification_next_steps(repo_id: str, state) -> None:
    if state.repair_paths:
        print(f"next: model-mirror repair {repo_id}")
    if state.upstream_status == "changed":
        print_update_next_step(repo_id)


def print_update_next_step(repo_id: str) -> None:
    print(f"update changed upstream: model-mirror repair --update {repo_id}")


def print_repair_commit_notice(repo_id: str, result) -> None:
    if result.upstream_status == "changed":
        print(
            f"upstream changed: {repo_id} local={result.resolved_commit} "
            f"upstream={result.upstream_commit} not_applied"
        )


def verify_one_offline(config: Config, root: Path, repo_id: str, args, existing_state, *, mode: str) -> int:
    if existing_state is None:
        print(f"{mode} verification unavailable: {repo_id}")
        return 1
    if args.cached:
        print(f"verified ({mode} cached): {repo_id} state={existing_state.status}")
        return 0 if existing_state.clean else 1
    if not (root / MANIFEST).exists():
        state = local_incomplete_state(repo_id, args.repo_type, existing_state, f"{MANIFEST} missing")
        write_verification_state(root, state)
        print(f"{mode} verification incomplete: {repo_id} missing {MANIFEST}")
        return 1
    result = verify_checksums(root, strict=args.strict)
    remote_result = RemoteVerifyResult(
        missing=result.missing,
        hash_mismatches=result.failures,
        extras=result.extras,
    )
    state = state_from_results(
        repo_id,
        args.repo_type,
        existing_state.requested_revision,
        remote_result,
        resolved_commit=existing_state.resolved_commit,
        upstream_commit=existing_state.upstream_commit,
        offline_only=existing_state.offline_only,
    )
    write_verification_state(root, state)
    if result.ok:
        print(f"verified ({mode} full): {repo_id}")
        return 0
    print(f"{mode} verification failed: {repo_id}")
    if existing_state.offline_only:
        print(f"repair unavailable for offline-only model: {repo_id}")
    else:
        print_verification_next_steps(repo_id, state)
    return 1


def local_incomplete_state(repo_id: str, repo_type: str, existing_state, issue: str) -> VerificationState:
    return VerificationState(
        status="incomplete",
        repo_id=repo_id,
        repo_type=repo_type,
        requested_revision=existing_state.requested_revision,
        resolved_commit=existing_state.resolved_commit,
        upstream_commit=existing_state.upstream_commit,
        upstream_status=existing_state.upstream_status,
        offline_only=existing_state.offline_only,
        repair_paths=[],
        issues=[issue],
    )


def handle_upstream_unavailable(
    root: Path,
    repo_id: str,
    args,
    existing_state,
    requested_revision: str,
    exc: Exception,
) -> int:
    issue = f"upstream unavailable: {exc}"
    if existing_state is not None:
        issues = [item for item in existing_state.issues if not str(item).startswith("upstream unavailable:")]
        issues.append(issue)
        state = VerificationState(
            status=existing_state.status,
            repo_id=repo_id,
            repo_type=args.repo_type,
            requested_revision=requested_revision,
            resolved_commit=existing_state.resolved_commit,
            upstream_commit=existing_state.upstream_commit,
            upstream_status=existing_state.upstream_status,
            offline_only=existing_state.offline_only,
            repair_paths=existing_state.repair_paths,
            issues=issues,
            checked_at_utc=existing_state.checked_at_utc,
        )
    else:
        state = VerificationState(
            status="unavailable",
            repo_id=repo_id,
            repo_type=args.repo_type,
            requested_revision=requested_revision,
            issues=[issue],
        )
    write_verification_state(root, state)
    print(f"verification failed: {repo_id} upstream repository unavailable: {exc}")
    print(
        f"if the source repository is no longer available and you want to keep this local mirror, "
        f"run: model-mirror offline {repo_id}"
    )
    return 1


def handle_offline_mode(args, config: Config, *, offline_only: bool) -> int:
    command = "offline" if offline_only else "online"
    root = archive_path(config, args.model, args.repo_type)
    with ModelLock(root, command, args.model, args.repo_type):
        state = read_verification_state(root)
        if state is None:
            print(f"verification state unavailable: {args.model}")
            print(f"run verify first: model-mirror verify {args.model}")
            return 1
        state.offline_only = offline_only
        if offline_only:
            state.issues = [item for item in state.issues if not str(item).startswith("upstream unavailable:")]
            if state.status == "unavailable":
                state.status = "incomplete"
                state.issues.append("local verification required")
        write_verification_state(root, state)
    if offline_only:
        print(f"offline-only enabled: {args.model}")
    else:
        print(f"offline-only disabled: {args.model}")
    return 0


def list_model_ids(config: Config) -> list[str]:
    models_root = Path(config.directory) / "models"
    if not models_root.exists():
        return []
    result = []
    for owner in sorted(path for path in models_root.iterdir() if path.is_dir()):
        for model in sorted(path for path in owner.iterdir() if path.is_dir()):
            result.append(f"{owner.name}/{model.name}")
    return result


def parse_age(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    value = value.strip().lower()
    if not value:
        raise ValueError("empty age")
    unit = value[-1]
    if unit in units:
        return int(value[:-1]) * units[unit]
    return int(value)


def verification_age_seconds(checked_at_utc: str) -> int | None:
    if not checked_at_utc:
        return None
    try:
        checked = datetime.fromisoformat(checked_at_utc)
    except ValueError:
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - checked).total_seconds()))


def verification_age_label(checked_at_utc: str) -> str:
    seconds = verification_age_seconds(checked_at_utc)
    return format_age_seconds(seconds)


def format_age_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 120:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def print_verification_age(config: Config, repo_id: str, repo_type: str) -> None:
    root = archive_path(config, repo_id, repo_type)
    state = read_verification_state(root)
    if state is None:
        print("verification age: unavailable")
        return
    age = verification_age_seconds(state.checked_at_utc)
    if age is None:
        path = verification_state_path(root)
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - modified).total_seconds()))
    print(f"verification age: {format_age_seconds(age)}")
    if age is not None and age > 24 * 60 * 60:
        print("warning: verification is older than 24h; run verify again for fresh repair paths")


def should_skip_recent_clean(config: Config, repo_id: str, repo_type: str, max_age: str) -> bool:
    state = read_verification_state(archive_path(config, repo_id, repo_type))
    if state is None or not state.clean or state_has_upstream_unavailable(state):
        return False
    age = verification_age_seconds(state.checked_at_utc)
    return age is not None and age <= parse_age(max_age)


if __name__ == "__main__":
    raise SystemExit(main())
