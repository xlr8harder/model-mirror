# model-mirror

Mirror and validate large Hugging Face model archives into bulk storage.

The CLI is designed to avoid the default Hugging Face cache for model payloads.
It stores downloaded files under a configured archive directory and writes
per-model hidden verification state:

- `.verification` for status, timestamp, issues, and repair paths
- `.checksums` for SHA-256 hashes
- `.manifest` for local sizes and mtimes

```bash
model-mirror config directory /mnt/big-drive/huggingface
model-mirror config
model-mirror config options
model-mirror config set hf-xet-high-performance true
model-mirror config set hf-xet-reconstruct-write-sequentially true
model-mirror mirror org/model
model-mirror mirror --commit abc123 org/model
model-mirror verify org/model
model-mirror verify --quick org/model
model-mirror verify --all
model-mirror verify --all --max-age 7d
model-mirror verify --repair org/model
model-mirror repair org/model
model-mirror update org/model
model-mirror list
```

Local configuration is written to `~/.model-mirror.yaml`.

## Download Backend

`model-mirror` uses `huggingface_hub` snapshot downloads, so Hugging Face Xet
remains the primary backend when available. Useful Xet-related config:

- `hf_xet_high_performance`: sets `HF_XET_HIGH_PERFORMANCE=1`
- `hf_xet_reconstruct_write_sequentially`: sets `HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY=1`
- `hf_xet_num_concurrent_range_gets`: sets `HF_XET_NUM_CONCURRENT_RANGE_GETS`

Sequential reconstruction writes are useful for HDD-backed archives. Xet still
owns the transfer/reconstruction stream, so `model-mirror` does not currently
compute SHA-256 while bytes are being written by Xet.

## Revision Handling

`model-mirror` resolves the requested revision, usually `main`, to a concrete
Hub commit before downloading. The download is then performed against that
commit SHA, avoiding races if the branch moves during a mirror.

Use `--revision` for a branch or tag and `--commit` for an exact commit SHA.
Both options produce a commit-pinned local mirror.

Verification checks the stored commit. When online, it also checks whether the
requested upstream revision now points at a different commit and records that in
`.verification` as `upstream_status: changed`. It does not overwrite a clean
local mirror just because upstream moved; use `model-mirror update MODEL` to do
that explicitly.

## Verification State

Each mirrored model directory owns its state:

```text
models/org/model/.verification
models/org/model/.checksums
models/org/model/.manifest
```

Deleting a model directory deletes its verification state with it. `.verification`
is small and human-readable; `.checksums` and `.manifest` are mechanical local
integrity data.

Checksum generation is resumable. `.checksums` and `.manifest` are updated after
each file hash completes, and later runs skip unchanged files using the recorded
size and mtime. `checksum_workers` controls checksum hashing concurrency and
defaults to `1`, which is conservative for HDDs.

## Completion And Locking

`.verification` is the durable completion marker. A mirror is trusted only when
that file says `status: clean` for the resolved Hub commit. Missing
`.verification`, `status: in_progress`, or `status: dirty` means the local copy
is not trusted yet.

Operations that mutate or verify a model take an advisory lock on
`.verification.lock`. The first mirror operation in a new model directory writes
`.verification` with `status: in_progress` before downloading. If another
operation tries to mirror, verify, repair, or update the same model while the
lock is held, it fails fast with lock details. `model-mirror list` does not
block; it shows busy lock details when present.
