# model-mirror

Mirror Hugging Face repositories into local bulk storage and verify that the
files remain complete.

`model-mirror` downloads directly into one archive directory, avoiding payload
files being left behind in the default Hugging Face cache. It records the exact
Hub commit mirrored, writes a local hash manifest, and keeps verification state
beside each model. If you already use `hf auth login`, model-mirror will try to
find that token automatically.

Online operations resolve the requested Hugging Face revision, usually `main`,
to a specific Hub commit before downloading, verifying, or repairing. The local
mirror stays tied to that commit so files are not mixed across upstream updates.
If the Hub repo later moves to a newer commit, `verify` reports the upstream
change without modifying local files.

## Quick Start

```bash
uv sync

uv run model-mirror config directory /mnt/big-drive/huggingface
uv run model-mirror config set hf-xet-reconstruct-write-sequentially true  # useful for HDDs
# Optional if token autodetection does not find your Hugging Face token:
uv run model-mirror config set token-path ~/.cache/huggingface/token

uv run model-mirror mirror org/model
uv run model-mirror list
uv run model-mirror verify org/model
uv run model-mirror repair org/model  # if verify reports repair paths
```

For periodic maintenance of the whole archive:

```bash
uv run model-mirror verify --all --max-age 30d || true
uv run model-mirror repair --all
```

Mirrors are stored by repo type:

```text
/mnt/big-drive/huggingface/models/org/model/
/mnt/big-drive/huggingface/datasets/org/data/
/mnt/big-drive/huggingface/spaces/org/space/
```

Run `model-mirror --help` or `model-mirror COMMAND --help` for the full CLI
reference. Run `model-mirror config options` for every supported config key.
Commands exit non-zero for dirty, incomplete, busy, or invalid states where that
matters; see each subcommand's help for exact exit-status behavior.

`model-mirror list` summarizes mirrors with tags such as
`state=[offline,needs-repair]`.

## Verification

`mirror` verifies by default. A clean mirror has:

- all expected Hub files present
- expected file sizes
- local SHA-256 and Git blob SHA-1 hashes in versioned `.manifest`
- LFS file hashes compared with Hub LFS SHA-256 metadata
- regular Git files compared with Hub Git blob ids
- `.verification` with `status: clean`

Useful verification commands:

```bash
model-mirror verify org/model
model-mirror verify --cached org/model
model-mirror verify --offline org/model
model-mirror verify --all
model-mirror verify --all --max-age 7d
```

`--cached` checks presence, sizes, and Hub-provided hashes from current
`.manifest` rows without rehashing payload files. If cached hash data is missing
or stale, cached verification exits non-zero and tells you to run full
verification. `--offline` does not contact the Hub, so it does not detect
whether the upstream repo has moved to a newer commit. Full offline verification
requires an existing `.manifest`; `--offline --cached` only reports the current
`.verification` state. `--max-age` is useful for periodic jobs that should skip
recently verified clean mirrors.

If the upstream repository is unavailable, online verification exits non-zero
and prints the command to mark the local mirror offline-only:

```bash
model-mirror offline org/model
```

Offline-only mirrors use local verification only and do not check whether the
Hub repo has moved or disappeared. Use `model-mirror online org/model` to
re-enable Hub checks.

If one repo is already locked, a single-repo `verify` exits non-zero. With
`verify --all`, locked repos are reported as skipped, remaining repos are still
checked, and the final exit status is non-zero.

Verification records missing or corrupt files as repair paths in
`.verification`. Repairs redownload only those paths:

```bash
model-mirror verify org/model
model-mirror repair org/model
```

`repair org/model` consumes the existing `.verification` state. If no
verification state exists, it tells you to run `verify` first. It prints how old
the verification result is, warns after 24 hours, updates manifest rows for
repaired files, and runs a final cached verification. In a `verify` then
`repair` workflow, the initial full verify hashes existing files once; repaired
files are hashed again after download, but unchanged large files are not
rehashed a second time.

If repair sees incomplete cached verification data for untouched files, it stops
before downloading and tells you to run full `verify`. `repair --force-partial`
overrides that safety check, but may leave the repository inconsistent and still
exits non-zero if final cached verification cannot prove the result.

An offline-only mirror cannot be repaired because there is no upstream source to
repair against. A direct `repair org/model` exits non-zero with that explanation;
`repair --all` logs a warning and skips offline-only mirrors.

## Periodic Jobs

For alert-only checks, run verification periodically and let its non-zero exit
status trigger normal alerting:

```bash
model-mirror verify --all --max-age 30d
```

For a repair pass after verification:

```bash
model-mirror verify --all --max-age 30d || true
model-mirror repair --all
```

Do not chain verification to repair with `&&`: `verify` exits non-zero when it
finds repairable damage. `verify --all` skips recently verified clean mirrors
when `--max-age` is set. Busy mirrors are reported and skipped, the rest of the
archive is still checked, and the final exit status is non-zero if any mirror is
dirty, failed, or busy.

## Upstream Updates

Every online operation resolves the requested revision, usually `main`, to a
specific Hub commit. The local mirror is tied to that commit.

If `verify` sees that upstream `main` now points at a different commit, it marks
the mirror with `upstream_status: changed` but does not overwrite local files.
`repair` consumes that verification state, repairs the recorded commit, and
reports that the upstream change was not applied. Updating is explicit:

```bash
model-mirror repair --update org/model
model-mirror repair --all --update
model-mirror mirror --commit abc123 org/model
```

Use `--commit` when you want a reproducible archive of an exact Hub revision.

## Common Commands

```bash
model-mirror mirror org/model              # download and verify
model-mirror mirror --no-verify org/model  # download without final verification
model-mirror verify org/model              # full verification
model-mirror verify --cached org/model     # use current .manifest hashes
model-mirror repair org/model              # redownload paths from .verification
model-mirror repair --all                  # repair all mirrors with recorded repair paths
model-mirror repair --update org/model     # apply a changed upstream commit recorded by verify
model-mirror offline org/model             # local verification only; no Hub checks
model-mirror online org/model              # re-enable Hub checks
model-mirror list                          # show mirrors, state tags, and verification age
```

Datasets and Spaces are supported with `--repo-type dataset` or
`--repo-type space`.

## Key Configuration

```bash
model-mirror config show
model-mirror config options
model-mirror config directory /mnt/big-drive/huggingface
model-mirror config set checksum-workers 1
model-mirror config set hf-xet-reconstruct-write-sequentially true
model-mirror config set hf-xet-num-concurrent-range-gets 1
```

Important configuration options:

- `directory`: archive root
- `repo_type`: default repo type, usually `model`
- `revision`: default branch, tag, or commit, usually `main`
- `checksum`: whether mirror/repair writes local hash manifest records
- `checksum_workers`: checksum hashing concurrency; `1` is HDD-friendly
- `verify_after_mirror`: run verification after `mirror`
- `token_path`: Hugging Face token file path; optional when autodetection finds
  `HF_TOKEN_PATH`, `HF_HOME/token`, `~/.cache/huggingface/token`, or
  `~/.huggingface/token`. If no token is found during Hub access,
  model-mirror warns and prints the config command to set this path. Token
  contents are never printed.
- `hf_xet_reconstruct_write_sequentially`: HDD-friendly Xet reconstruction
  writes; uses the current `HF_XET_RECONSTRUCTION_USE_VECTORED_WRITE=false`
  knob when supported
- `hf_xet_num_concurrent_range_gets`: Xet internal download concurrency.
  Default `1` is HDD-friendly; increase for SSD/NVMe.
- `hf_xet_high_performance`: enable Xet high-performance mode. This is off by
  default; use only on high-bandwidth machines with fast disks and ample memory,
  typically 64 GB RAM or more.

## Notes

`model-mirror` uses `huggingface_hub` snapshot downloads, so Hugging Face Xet is
used automatically when available. `model-mirror` keeps Hugging Face cache and
temporary directories under the configured archive root so large downloads do
not spill into your default home cache.

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for implementation details, testing, and
future design notes.
