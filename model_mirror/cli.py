from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from .audit import audit_model
from .checksums import CHECKSUMS
from .config import Config, archive_path, load_config, save_config, parse_bool, parse_positive_int
from .hub import HuggingFaceHub, get_snapshot
from .lock import ModelBusyError, ModelLock, lock_label, read_active_lock
from .mirror import mirror
from .repair import repair
from .state import read_verification_state, state_from_results, write_verification_state
from .verify import verify_remote


CONFIG_OPTIONS = [
    (
        "directory",
        "MODEL_MIRROR_DIRECTORY",
        "Archive root. Repos are stored below models/, datasets/, or spaces/ under this directory.",
    ),
    ("repo_type", "MODEL_MIRROR_REPO_TYPE", "Default Hugging Face repo type: model, dataset, or space."),
    ("revision", "MODEL_MIRROR_REVISION", "Default revision to mirror or verify, usually main."),
    ("checksum", None, "Whether mirror/repair writes local SHA-256 checksum records."),
    (
        "checksum_workers",
        "MODEL_MIRROR_CHECKSUM_WORKERS",
        "Number of files to hash concurrently. Use 1 for HDD-friendly sequential reads.",
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
    ("token_path", "MODEL_MIRROR_TOKEN_PATH", "Path to a Hugging Face token file. Token contents are never printed."),
    ("cache_dir", None, "Overrides the Hugging Face cache root; defaults to DIRECTORY/.cache."),
    ("tmp_dir", None, "Overrides temporary file directory; defaults to DIRECTORY/tmp."),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-mirror",
        description="Mirror Hugging Face repositories into local bulk storage and verify their integrity.",
        epilog="Run 'model-mirror COMMAND --help' for command-specific options.",
    )
    parser.add_argument("--config", help="path to config file; defaults to ~/.model-mirror.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mirror_parser = subparsers.add_parser(
        "mirror",
        help="mirror a Hugging Face repo",
        description="Download a repo at a resolved Hub commit and verify it unless --no-verify is used.",
    )
    mirror_parser.add_argument("model", metavar="repo", help="Hugging Face repo id, e.g. org/model")
    mirror_parser.add_argument("--repo-type", choices=["model", "dataset", "space"], help="repo kind to mirror")
    add_revision_options(mirror_parser)
    mirror_parser.add_argument("--force", action="store_true", help="download even if the local copy looks complete")
    mirror_parser.add_argument("--no-verify", action="store_true", help="skip verification after download")

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify mirrored archives",
        description="Check local files against Hub metadata, local checksums, and model metadata.",
    )
    verify_parser.add_argument("model", metavar="repo", nargs="?", help="repo id to verify unless --all is used")
    verify_parser.add_argument("--quick", action="store_true", help="skip full SHA-256 checks")
    verify_parser.add_argument("--all", action="store_true", help="verify every mirrored model")
    verify_parser.add_argument(
        "--repo-type", choices=["model", "dataset", "space"], default="model", help="repo kind to verify"
    )
    add_revision_options(verify_parser)
    verify_parser.add_argument("--strict", action="store_true", help="fail on extra local files")
    verify_parser.add_argument("--repair", action="store_true", help="repair dirty archives after verification")
    verify_parser.add_argument("--max-age", help="with --all, skip clean archives verified within this age, e.g. 7d")
    verify_parser.add_argument("--offline", action="store_true", help="verify only against local checksum state")

    repair_parser = subparsers.add_parser(
        "repair",
        help="repair a mirrored archive using its .verification state",
        description="Redownload only files listed as repair paths, then run a final verification.",
    )
    repair_parser.add_argument("model", metavar="repo", help="repo id to repair")
    repair_parser.add_argument(
        "--repo-type", choices=["model", "dataset", "space"], default="model", help="repo kind to repair"
    )
    add_revision_options(repair_parser)
    repair_parser.add_argument("--force-verify", action="store_true", help="ignore existing .verification and verify first")

    update_parser = subparsers.add_parser(
        "update",
        help="explicitly update a mirror to the latest requested revision",
        description="Force a mirror refresh. This is the explicit path for moving to a newer upstream commit.",
    )
    update_parser.add_argument("model", metavar="repo", help="repo id to update")
    update_parser.add_argument("--repo-type", choices=["model", "dataset", "space"], help="repo kind to update")
    add_revision_options(update_parser)
    update_parser.add_argument("--no-verify", action="store_true", help="skip verification after update")

    subparsers.add_parser("list", help="list mirrored models", description="Show mirrored models and verification age.")

    config_parser = subparsers.add_parser(
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

    return parser


def add_revision_options(parser: argparse.ArgumentParser) -> None:
    revision_group = parser.add_mutually_exclusive_group()
    revision_group.add_argument("--revision", help="branch, tag, or commit to use; defaults to config revision")
    revision_group.add_argument("--commit", help="commit SHA to use")


def selected_revision_arg(args) -> str | None:
    return args.commit or args.revision


def main(argv: list[str] | None = None, *, hub=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser() if args.config else None
    config = load_config(config_path)

    try:
        if args.command == "config":
            return handle_config(args, config, config_path)
        if args.command == "list":
            return handle_list(config)
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
            )
            print(f"{result.status}: {args.model} -> {result.path}")
            return mirror_exit_code(result.status)
        if args.command == "verify":
            return handle_verify(args, config, hub=hub)
        if args.command == "repair":
            selected_hub = hub or HuggingFaceHub(config)
            result = repair(
                config,
                args.model,
                hub=selected_hub,
                repo_type=args.repo_type,
                revision=selected_revision_arg(args),
                force_audit=args.force_verify,
            )
            print(f"{result.status}: {args.model} -> {result.path}")
            return 0 if result.status in {"complete", "repaired"} else 1
        if args.command == "update":
            selected_hub = hub or HuggingFaceHub(config)
            result = mirror(
                config,
                args.model,
                hub=selected_hub,
                repo_type=args.repo_type,
                revision=selected_revision_arg(args),
                force=True,
                verify_after=config.verify_after_mirror and not args.no_verify,
            )
            print(f"updated: {args.model} -> {result.path}")
            return mirror_exit_code(result.status)
    except ModelBusyError as exc:
        print(str(exc))
        return 1

    parser.error(f"Unhandled command: {args.command}")
    return 2


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
    models_root = Path(config.directory) / "models"
    if not models_root.exists():
        return 0
    for owner in sorted(path for path in models_root.iterdir() if path.is_dir()):
        for model in sorted(path for path in owner.iterdir() if path.is_dir()):
            state = read_verification_state(model)
            suffix = ""
            if state is not None:
                suffix = f"  verification={state.status} age={verification_age_label(state.checked_at_utc)}"
            active_lock = read_active_lock(model)
            if active_lock is not None:
                suffix += f"  busy=({lock_label(active_lock)})"
            print(f"{owner.name}/{model.name}{suffix}")
    return 0


def handle_verify(args, config: Config, *, hub=None) -> int:
    if args.all:
        failures = 0
        for repo_id in list_model_ids(config):
            if args.max_age and should_skip_recent_clean(config, repo_id, args.repo_type, args.max_age):
                print(f"skipped recent clean verification: {repo_id}")
                continue
            if verify_one(config, repo_id, args, hub=hub) != 0:
                failures += 1
        return 1 if failures else 0

    if not args.model:
        raise SystemExit("verify requires a model id unless --all is used")
    return verify_one(config, args.model, args, hub=hub)


def verify_one(config: Config, repo_id: str, args, *, hub=None) -> int:
    selected_hub = hub or HuggingFaceHub(config)
    root = archive_path(config, repo_id, args.repo_type)
    with ModelLock(root, "verify", repo_id, args.repo_type):
        rc, needs_repair, requested_revision = verify_one_locked(config, repo_id, args, selected_hub, root)
    if needs_repair:
        repair_result = repair(
            config,
            repo_id,
            hub=selected_hub,
            repo_type=args.repo_type,
            revision=requested_revision,
        )
        print(f"{repair_result.status}: {repo_id}")
        return 0 if repair_result.status in {"complete", "repaired"} else 1
    return rc


def verify_one_locked(config: Config, repo_id: str, args, selected_hub, root: Path) -> tuple[int, bool, str]:
    existing_state = read_verification_state(root)
    requested_revision = selected_revision_arg(args) or (
        existing_state.requested_revision if existing_state else config.revision
    )

    if args.offline:
        return verify_one_offline(root, repo_id, args, existing_state), False, requested_revision

    upstream_snapshot = get_snapshot(selected_hub, repo_id, args.repo_type, requested_revision)
    resolved_commit = existing_state.resolved_commit if existing_state and existing_state.resolved_commit else upstream_snapshot.resolved_commit
    snapshot = upstream_snapshot if resolved_commit == upstream_snapshot.resolved_commit else get_snapshot(
        selected_hub, repo_id, args.repo_type, resolved_commit
    )
    metadata = snapshot.files
    from_checksums = not args.quick and (root / CHECKSUMS).exists()
    result = verify_remote(
        root,
        metadata,
        quick=args.quick,
        from_checksums=from_checksums,
        strict=args.strict,
    )
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

    if args.repair and not state.clean:
        return 0, True, requested_revision

    if audit is not None and not audit.ok:
        print(f"verification failed: {repo_id}")
        return 1, False, requested_revision
    if result.ok:
        mode = "quick" if args.quick else "full"
        stale = " upstream=changed" if state.upstream_status == "changed" else ""
        print(f"verified ({mode}): {repo_id}{stale}")
        return 0, False, requested_revision
    print(f"verification failed: {repo_id}")
    return 1, False, requested_revision


def verify_one_offline(root: Path, repo_id: str, args, existing_state) -> int:
    from .checksums import verify_checksums

    if existing_state is None:
        print(f"offline verification unavailable: {repo_id}")
        return 1
    if args.quick:
        print(f"verified (offline quick): {repo_id} state={existing_state.status}")
        return 0 if existing_state.clean else 1
    result = verify_checksums(root)
    if result.ok:
        print(f"verified (offline full): {repo_id}")
        return 0
    print(f"offline verification failed: {repo_id}")
    return 1


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


def should_skip_recent_clean(config: Config, repo_id: str, repo_type: str, max_age: str) -> bool:
    state = read_verification_state(archive_path(config, repo_id, repo_type))
    if state is None or not state.clean:
        return False
    age = verification_age_seconds(state.checked_at_utc)
    return age is not None and age <= parse_age(max_age)


if __name__ == "__main__":
    raise SystemExit(main())
