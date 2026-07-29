# Configuration And Storage

This guide covers archive layout, configuration syntax, repository types,
Hugging Face authentication, HDD-oriented settings, and transient disk usage.
Start with the project [README](../README.md) for installation.

## Archive Layout

Set the archive root once:

```bash
model-mirror config directory /mnt/big-drive/huggingface
```

Mirrors are organized by Hugging Face repository type:

```text
/mnt/big-drive/huggingface/models/org/model/
/mnt/big-drive/huggingface/datasets/org/data/
/mnt/big-drive/huggingface/spaces/org/space/
/mnt/big-drive/huggingface/.model-mirror/cache/
/mnt/big-drive/huggingface/.model-mirror/tmp/
```

The archive has one top-level `.model-mirror/` control directory. Its `cache/`
and `tmp/` children contain disposable Hugging Face/Xet state and resumable
download staging. Each mirrored repository has its own `.model-mirror/`
directory for durable commit-scoped metadata. Payload directories do not
contain a model-mirror virtual environment or package-installation cache.

## Configuration Commands

```bash
model-mirror config show
model-mirror config options
model-mirror config directory /mnt/big-drive/huggingface
model-mirror config set checksum-workers 1
model-mirror config set hf-xet-reconstruct-write-sequentially true
model-mirror config set hf-xet-num-concurrent-range-gets 1
```

`config set` accepts kebab-case or snake_case keys. Saved YAML and
`config options` use canonical snake_case.

`config directory PATH` is a convenience form for the most common setting and
creates `PATH` immediately. `config set directory PATH` saves the same value
without creating the directory until it is needed.

Important options:

- `directory`: archive root
- `repo_type`: default Hugging Face repository type
- `revision`: default branch, tag, or commit, normally `main`
- `checksum`: whether mirror and repair write local hash manifest records
- `checksum_workers`: hashing concurrency; `1` is HDD-friendly
- `download_workers`: simultaneous payload files; `1` is HDD-friendly
- `stall_timeout_seconds`: seconds without local byte progress before retry
- `stall_retries`: stall-triggered resume attempts per file
- `verify_after_mirror`: run verification after `mirror`
- `token_path`: explicit Hugging Face token file
- `cache_dir`: archive-wide Hugging Face/Xet cache override; defaults to
  `DIRECTORY/.model-mirror/cache`
- `tmp_dir`: staging and temporary directory override; defaults to
  `DIRECTORY/.model-mirror/tmp`
- `hf_xet_reconstruct_write_sequentially`: HDD-friendly reconstruction writes
- `hf_xet_num_concurrent_range_gets`: Xet internal download concurrency
- `hf_xet_high_performance`: opt-in fast mode for suitable hardware

Use `model-mirror config options` for the authoritative full list and
environment-variable mappings.

## Repository Types

The initial configured default is `model`. Every repository-targeted command
uses that default when `--repo-type` is omitted:

```bash
model-mirror mirror org/model
model-mirror mirror --repo-type dataset org/data
model-mirror status --repo-type space org/app
```

Archive-wide `status` includes all repository types. Torrent join and import
read the type embedded in the publication descriptor.

## Hugging Face Authentication

If you already use `hf auth login`, model-mirror searches the standard token
locations. It checks, in order, explicit model-mirror configuration and common
Hugging Face environment or cache locations, including:

- `MODEL_MIRROR_TOKEN_PATH`
- `HF_TOKEN_PATH`
- `HF_HOME/token`
- `~/.cache/huggingface/token`
- `~/.huggingface/token`

Set an explicit path if autodetection does not find the intended token:

```bash
model-mirror config set token-path ~/.cache/huggingface/token
```

The token contents are never printed. Hub access without a token emits a
warning because gated repositories and throughput can be affected.

## HDD And Xet Settings

The conservative defaults favor bulk HDD archives:

- one download worker
- one checksum worker
- Xet high-performance mode disabled
- Xet range-get concurrency of one
- optional sequential reconstruction writes

Enable sequential writes for rotational storage:

```bash
model-mirror config set hf-xet-reconstruct-write-sequentially true
```

Increase download workers, checksum workers, or Xet concurrency only when the
archive is on SSD/NVMe and the machine has enough memory and bandwidth.
High-performance Xet mode is intended for high-bandwidth systems with fast
storage and ample memory, typically at least 64 GB.

Model-mirror translates its settings to the controls supported by the installed
Hugging Face/Xet transport and removes inherited feature environment variables
when the corresponding config value is disabled.

Hugging Face metadata selects HTTP or Xet transport automatically when Xet is
available for the requested payload.

## Disk Space During Transfers

Model-mirror does not stage a second complete snapshot. Each file streams
through HTTP or Xet into a sibling `NAME.incomplete` file while integrity and
enabled torrent hashes are accumulated. The partial file is the eventual
payload allocation and is atomically renamed to `NAME` after verification.

With the default `download_workers: 1`, only one payload file is incomplete at
a time. Same-commit repair removes a damaged file before redownloading it, so
it does not retain both damaged and repaired copies.

Allow space for:

- the final snapshot
- active partial files
- auxiliary transport state below `.model-mirror/tmp/downloads`

Auxiliary cache size varies with the HTTP/Xet implementation and is not a fixed
percentage. Model-mirror does not intentionally reconstruct a second complete
payload there.

Interrupted partial files and staging are retained for resume. Successful
operations remove their staging directory, leaving no runtime cache in steady
state. See [Archive Operations](archive-operations.md) for stale-cache
detection and cleanup.

## Per-Repository Metadata

Each mirror stores its durable state beside the payload:

- `.verification`: status, timestamps, commits, offline mode, issues, and
  repair paths
- `.manifest`: versioned local size, mtime, SHA-256, and Git blob SHA-1 records
- `.verification.lock`: advisory operation lock metadata
- `.model-mirror/snapshot.json`: authoritative commit-pinned expected file list
- `.model-mirror/torrent/`: optional coverage, publication, and fence state

Deleting payload directories manually also deletes this evidence. Prefer
`model-mirror remove REPO`, which provides confirmation and resumable cleanup.
