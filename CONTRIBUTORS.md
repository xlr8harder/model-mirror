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

- `.verification`: YAML status, timestamps, commit metadata, issues, repair paths
- `.checksums`: local SHA-256 records
- `.manifest`: local size and mtime records for checksum skip/resume
- `.verification.lock`: advisory lock metadata while an operation is active

Deleting a model directory deletes its verification state with it. There is no
global model state database.

## Commit Handling

Online operations resolve the requested revision to a concrete Hub commit before
downloading or verifying. A clean local mirror is trusted for its resolved
commit. If upstream moves, verification records `upstream_status: changed` but
does not mutate local files. `update` is the explicit command for moving to the
new upstream commit.

## Checksums

Checksum writes are incremental. After each file is hashed, `.checksums` and
`.manifest` are atomically rewritten. Later runs skip files whose size and mtime
match the manifest record. This makes interrupted verification cheaper to
resume, although it does not make Hugging Face/Xet's own transfer stage fully
stream-verifiable.

## Locking

`mirror`, `verify`, `repair`, and `update` take an advisory lock on
`.verification.lock` for the target repo. The first mirror operation writes
`.verification` with `status: in_progress` before downloading. `list` does not
block; it reports lock metadata when a repo is busy.

## Hugging Face And Xet

`model-mirror` delegates transfer to `huggingface_hub.snapshot_download`.
Environment setup is derived from model-mirror config and is intentionally
authoritative: if a config boolean is false, inherited Xet environment variables
for that feature are removed before importing or using `huggingface_hub`.

The conservative default is:

- high-performance Xet mode off
- range-get concurrency unset, which leaves Hugging Face's default in place
- optional sequential reconstruction writes for HDD-backed archives

This keeps the default path usable on lower-memory machines and lets power users
tune throughput explicitly.
