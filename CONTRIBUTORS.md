# Contributors

This document holds implementation notes and development details. User-facing
usage belongs in `README.md`.

## Development

```bash
uv sync
uv run coverage run -m pytest
uv run coverage report -m
```

The test suite is expected to hold 100% statement and branch coverage. The
coverage gate is enforced in `pyproject.toml`.

## State Files

Each mirrored repo owns its state inside the repo directory:

- `.verification`: YAML status, timestamps, commit metadata, offline-only flag,
  issues, repair paths
- `.manifest`: versioned JSONL records with local size, mtime, SHA-256, and Git blob SHA-1
- `.verification.lock`: advisory lock metadata while an operation is active
- `.model-mirror/snapshot.json`: authoritative commit-pinned expected file list
- `.model-mirror/torrent/coverage/`: atomic, resumable profile-specific piece
  and Merkle hash coverage
- `.model-mirror/torrent/publications/`: torrent artifacts, recovery records,
  and durable publication/desired-seed state
- `.model-mirror/torrent/fence.json`: the active commit update fence

Deleting a model directory deletes its verification state with it. There is no
global model state database.

## Commit Handling

Online operations resolve the requested revision to a concrete Hub commit before
downloading or verifying. A clean local mirror is trusted for its resolved
commit. If upstream moves, verification records `upstream_status: changed` but
does not mutate local files. `repair --update` is the explicit command for
moving to the new upstream commit recorded by verification.

If upstream is unavailable, verification exits non-zero and preserves the local
verification status when one already exists. `offline` sets `offline_only: true`
for that repo, clears the upstream-unavailable issue, and makes future
verification local-only until `online` clears the flag.

## Checksums

Manifest writes are incremental. After each file is hashed, `.manifest` is
atomically rewritten with a schema/version header and one record per payload
file. Each file is read once while both SHA-256 and Git blob SHA-1 are computed.
Later runs skip files whose size, mtime, and hash fields match the manifest
record. This makes interrupted verification cheaper to resume, although it does
not make Hugging Face/Xet's own transfer stage fully stream-verifiable.

## Locking

Mutating mirror, verification, repair, upgrade, and torrent control-plane
transitions take an advisory lock on `.verification.lock` for the target repo.
The first mirror operation writes `.verification` with `status: in_progress`
before downloading. The seeder acquires the lock only for short reconciliation
transitions, never for its lifetime. `list` does not block; it reports lock
metadata when a repo is busy.

## Hugging Face And Xet

`model-mirror` delegates transfer to `huggingface_hub.snapshot_download`.
Environment setup is derived from model-mirror config and is intentionally
authoritative: if a config boolean is false, inherited Xet environment variables
for that feature are removed before importing or using `huggingface_hub`.

The conservative default is:

- high-performance Xet mode off
- range-get concurrency `1`
- optional sequential reconstruction writes for HDD-backed archives

This keeps the default path usable on lower-memory machines and lets power users
tune throughput explicitly.

## Design Documents

- [Torrent Distribution And Archive Upgrade Requirements](docs/torrent-distribution-requirements.md)
- [Torrent Backend Spike](docs/torrent-backend-spike.md)
