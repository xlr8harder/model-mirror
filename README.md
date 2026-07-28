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

Install the `model-mirror-cli` distribution from PyPI with
[uv](https://docs.astral.sh/uv/) (Python 3.11 or newer):

```bash
uv tool install model-mirror-cli

model-mirror config directory /mnt/big-drive/huggingface
model-mirror config set hf-xet-reconstruct-write-sequentially true  # useful for HDDs
# Optional if token autodetection does not find your Hugging Face token:
model-mirror config set token-path ~/.cache/huggingface/token

model-mirror mirror org/model
model-mirror status
model-mirror verify org/model
model-mirror repair org/model  # if verify reports repair paths
```

The PyPI distribution is named `model-mirror-cli` to distinguish it from an
unrelated package; the installed command remains `model-mirror`. To include
experimental torrent support from the start, install
`uv tool install 'model-mirror-cli[torrent]'`.

Update and inspect an installed release with:

```bash
uv tool upgrade model-mirror-cli
model-mirror --version
model-mirror version
```

`--version` reports the installed version without network access. The `version`
command explicitly checks the latest PyPI release and, when the installation is
out of date, prints the published changes and
`uv tool upgrade model-mirror-cli`. Release notes come from the curated
[`CHANGELOG.md`](https://github.com/xlr8harder/model-mirror/blob/main/CHANGELOG.md)
through the corresponding GitHub Releases. The check does not run implicitly
from `status` or other archive operations.

For development, clone the
[source repository](https://github.com/xlr8harder/model-mirror), run
`uv sync --all-extras --dev`, and prefix commands with `uv run`. The rest of
this README assumes the tool installation above.

For periodic maintenance of the whole archive:

```bash
model-mirror verify --all --max-age 30d || true
model-mirror repair --all
```

Mirrors are stored by repo type:

```text
/mnt/big-drive/huggingface/models/org/model/
/mnt/big-drive/huggingface/datasets/org/data/
/mnt/big-drive/huggingface/spaces/org/space/
/mnt/big-drive/huggingface/.model-mirror/cache/
/mnt/big-drive/huggingface/.model-mirror/tmp/
```

The archive has one top-level control directory, `.model-mirror/`. Its `cache/`
and `tmp/` children contain disposable Hugging Face/Xet state and resumable
download staging. Each mirrored repository also has its own `.model-mirror/`
directory for durable commit-scoped metadata. Payload directories never contain
a model-mirror virtual environment or package-installation cache.

Run `model-mirror --help` or `model-mirror COMMAND --help` for the full CLI
reference. Run `model-mirror config options` for every supported config key.
Commands exit non-zero for dirty, incomplete, busy, or invalid states where that
matters; see each subcommand's help for exact exit-status behavior.

Without a repository argument, `model-mirror status` prints a compact,
metadata-only archive table with repository type and ID, recorded file count
and payload size, abbreviated resolved commit, verification age, and
exceptional state. A healthy row uses `-` in `EXCEPTIONS` instead of repeating
`clean`. Torrent and live activity columns appear only when relevant.
The deprecated `model-mirror list` spelling remains available as a compatibility
alias but is no longer presented as a separate command.

Status reads model-mirror's existing verification, manifest, snapshot, lock,
progress, torrent metadata, and shallow runtime-cache operation records. It
does not recursively scan payload or cache directories, calculate cache sizes,
read payload bytes, contact the Hub, or update metadata. Missing recorded
counts or sizes are displayed as unknown; use `verify` to reconcile the local
filesystem.

Pass a repository ID for a detailed, strictly local last-known report without
contacting the Hub:

```bash
model-mirror status org/model
model-mirror status --repo-type dataset org/data
model-mirror status --check-upstream org/model
model-mirror status --verbose org/model
model-mirror status --json
```

The default detail view emphasizes verification state and age, resolved commit,
recorded payload size, upstream state, torrent publication, exceptional state,
and useful next actions. Issues and repair paths appear only when present.
`--verbose` prints all recorded metadata fields.

`--check-upstream` performs an advisory live comparison between the locally
resolved commit and the commit currently resolved by the requested upstream
revision. It does not write verification state, snapshot metadata, timestamps,
or locks. The ordinary recorded upstream state is still shown separately from
the live observation in JSON.

`--json` emits the versioned `model-mirror-status` schema with one
`repositories` array for both single-repository and archive-wide output.
Numbers remain numeric, timestamps are ISO 8601, and issues and repair paths are
arrays. Exceptional runtime data appears in the top-level `cache` array and on
its associated repository; the array is empty in steady state. `--verbose` is
accepted as a no-op when combined with `--json`. A mismatch between verification
and snapshot commits is reported as `snapshot-stale`.

## Verification

`mirror` verifies by default. A clean mirror has:

- all expected Hub paths present as canonical regular files (symlinks are rejected)
- expected file sizes
- local SHA-256 and Git blob SHA-1 hashes in versioned `.manifest`
- LFS file hashes compared with Hub LFS SHA-256 metadata
- regular Git files compared with Hub Git blob ids
- `.verification` with `status: clean`

By default, verification ignores unexpected payload files, so they do not make
an otherwise correct mirror dirty. Use `verify --strict` to report unexpected
files and make verification fail. Model-mirror's own state and cache paths are
not treated as payload extras.

Useful verification commands:

```bash
model-mirror verify org/model
model-mirror verify --cached org/model
model-mirror verify --offline org/model
model-mirror verify --strict org/model
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

Failed verification prints categorized paths for missing files, size
mismatches, hash mismatches, unavailable cached hashes, unexpected files, and
unsafe payload paths, followed by an exact repair or full-verification command.
Long lists are capped in the immediate output; the complete recorded issue list
remains available through `model-mirror status --verbose REPO`.

Model formats are opaque to model-mirror. It does not require or parse
`config.json`, Safetensors, GGUF, ONNX, or framework-specific layouts. It
verifies the pinned upstream file list, safe regular-file paths, sizes, hashes,
and mirror metadata; format compatibility belongs to the software consuming
the mirror.

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

Repair also reconciles a pinned snapshot plan whose commit is older than the
clean verification state. It fetches the recorded commit's Hub metadata and
reuses current manifest hashes, so payload files are not reread when that cached
evidence is complete. A successful commit update promotes the new snapshot plan
before recording the new commit as clean.

If repair sees incomplete cached verification data for untouched files, it stops
before downloading and tells you to run full `verify`. `repair --force-partial`
overrides that safety check, but may leave the repository inconsistent and still
exits non-zero if final cached verification cannot prove the result.

An offline-only mirror cannot be repaired because there is no upstream source to
repair against. A direct `repair org/model` exits non-zero with that explanation;
`repair --all` logs a warning and skips offline-only mirrors.

## Experimental Torrent Publishing And Recovery

Torrent support is optional and experimental. The `upgrade` and `torrent`
commands, their behavior, and the torrent-specific metadata formats may change
before stabilization. Ordinary mirror, verify, and repair workflows remain
stable and do not require the torrent extra:

```bash
# Install or replace the PyPI tool with torrent support:
uv tool install --force 'model-mirror-cli[torrent]'

# Or add the extra to the checkout's development environment:
uv sync --extra torrent
```

Every torrent is an immutable publication of one resolved
`repo@commit`. Publishing first requires a clean, pinned mirror. It completes
any missing torrent hash coverage, writes an ordinary hybrid v1/v2 `.torrent`,
prints a standard magnet URI and external-client data location, and creates a
persistent fence that prevents the canonical archive from moving to another
commit.

```bash
model-mirror upgrade org/model             # optional explicit precomputation
model-mirror torrent create org/model      # artifacts and fence; no seed intent
model-mirror torrent publish org/model     # artifacts, fence, and durable seed intent
model-mirror torrent show org/model
```

Downloads and same-commit repairs accumulate reusable torrent hashes during
their existing payload pass. `upgrade` is for older archives or incomplete
coverage; it reads only files still missing hashes for the current publication
profile:

```bash
model-mirror upgrade org/model
model-mirror upgrade --all --dry-run
model-mirror upgrade --all
```

The publication profile is the versioned, deterministic set of rules that fixes
file order, piece sizing, padding, descriptor encoding, and therefore torrent
identity. The current and only profile is `hybrid-v1-v2-1`; model-mirror selects
it automatically. There is no user-selectable profile option yet. A future
identity-affecting algorithm change will use a new profile name rather than
silently changing an existing swarm.

The managed backend is a replaceable long-running process:

```bash
model-mirror torrent serve
```

Run it under your normal service manager with restart-on-failure and
restart-on-boot. Publication intent, verified file fingerprints, and the update
fence are durable; a restarted backend reconciles desired seeds without an
explicit reseed command or a payload-wide recheck. `model-mirror status` reports
desired and observed torrent state. A minimal systemd service uses the same
configured archive:

```ini
[Service]
ExecStart=/path/to/model-mirror --config /path/to/config.yaml torrent serve
Restart=always
```

The emitted `.torrent` and magnet are not tied to the managed backend. To use a
preferred client, either add the printed torrent with its printed data location
for seeding, or record external ownership explicitly:

```bash
model-mirror torrent publish --external org/model
```

Model-mirror does not control or infer the runtime health of an external
client. Stop that client before modifying payload. `torrent stop` stops managed
intent (or records that an external seed was stopped) but deliberately retains
the update fence. Only explicit retirement releases it:

```bash
model-mirror torrent stop org/model
model-mirror torrent retire org/model
model-mirror repair --update org/model
```

Same-commit `repair` enters maintenance, waits for the managed backend to
detach, repairs only recorded paths, refreshes their coverage, and resumes the
same publication when verification succeeds. `repair --update` stays blocked
until retirement.

On another host, model-mirror can download natively or provide an exact
standard-client handoff:

```bash
model-mirror torrent join /path/model@commit.torrent
model-mirror torrent join 'magnet:?xt=...' --seed

model-mirror torrent handoff /path/model@commit.torrent
# Download with any standard client to the printed data location, then run:
model-mirror torrent import /path/model@commit.torrent /printed/path/model
```

Import validates hostile paths, the commit-scoped descriptor, sizes, and
content before atomically moving the staged payload into the normal archive
layout. Native join reuses libtorrent's verified piece state and reconstructs
coverage without rereading the payload. External-client import independently
reads and hashes the downloaded files because model-mirror cannot assume that
client's runtime state.

A trusted torrent proves consistency with the supplied infohash; by itself it
does not prove that the publisher's bytes were authentic Hugging Face content.
Imported state therefore records `torrent-verified`,
`trusted-infohash`, and `not-upstream-verified` separately. It remains usable
when upstream has disappeared and can later be checked against upstream if it
becomes available.

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

Repair and update are deliberately separate operations:

- `repair` restores missing or damaged files from the mirror's currently
  recorded commit. It never moves the mirror to a different commit, even when
  `verify` has detected an upstream change.
- `repair --update` explicitly moves the mirror to the changed upstream commit
  recorded by `verify`.

Updating one or all changed mirrors is therefore explicit:

```bash
model-mirror repair --update org/model
model-mirror repair --all --update
```

Use `model-mirror mirror --commit abc123 org/model` when you want a reproducible
archive pinned to an exact Hub revision.

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
model-mirror status                        # metadata-only archive status
model-mirror status org/model              # concise last-known repository state
model-mirror status --verbose org/model    # full recorded metadata
model-mirror status --check-upstream org/model  # advisory live upstream comparison
model-mirror status --json                 # stable machine-readable status
model-mirror version                       # compare installed version with PyPI
model-mirror remove org/model              # inspect, confirm, and permanently remove one mirror
model-mirror upgrade org/model             # fill missing torrent hash coverage
model-mirror torrent publish org/model     # publish and request durable seeding
model-mirror torrent join FILE_OR_MAGNET   # recover a normal local archive
```

Omitting `--repo-type` uses the configured default, initially `model`, for every
repo-targeted command. Use `--repo-type dataset` or `--repo-type space` to
override it. Archive-wide `list` and `status` include all repo types; torrent
join/import operations read the repo type from the publication descriptor.

## Key Configuration

```bash
model-mirror config show
model-mirror config options
model-mirror config directory /mnt/big-drive/huggingface
model-mirror config set checksum-workers 1
model-mirror config set hf-xet-reconstruct-write-sequentially true
model-mirror config set hf-xet-num-concurrent-range-gets 1
```

`config set` accepts either kebab-case or snake_case keys; the saved YAML and
`config options` use canonical snake_case. `config directory PATH` is a
convenience form for the most common setting and creates `PATH` immediately.
`config set directory PATH` saves the same value without creating the directory
until it is needed.

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
- `cache_dir`: optional override for the Hugging Face/Xet cache; defaults to
  `DIRECTORY/.model-mirror/cache`
- `tmp_dir`: optional override for staging and temporary files; defaults to
  `DIRECTORY/.model-mirror/tmp`
- `hf_xet_reconstruct_write_sequentially`: HDD-friendly Xet reconstruction
  writes; uses the current `HF_XET_RECONSTRUCTION_USE_VECTORED_WRITE=false`
  knob when supported
- `hf_xet_num_concurrent_range_gets`: Xet internal download concurrency.
  Default `1` is HDD-friendly; increase for SSD/NVMe.
- `hf_xet_high_performance`: enable Xet high-performance mode. This is off by
  default; use only on high-bandwidth machines with fast disks and ample memory,
  typically 64 GB RAM or more.

## Disk Space During Transfers

Model-mirror does not stage a second complete snapshot. Each file is streamed
through the HTTP or Xet transport into a sibling `NAME.incomplete` file while
its integrity hashes are accumulated, then atomically renamed to `NAME`. The
partial file is the eventual payload allocation rather than an additional full
copy. With the default `download_workers: 1`, only one payload file is
incomplete at a time. Same-commit repair removes a damaged file before
redownloading it, so it likewise does not retain both the damaged and repaired
copy.

Allow space for the final snapshot, active partial files, and auxiliary
transport cache under `DIRECTORY/.model-mirror/tmp/downloads`. The auxiliary
cache is not specified as a fixed percentage and can vary with the HTTP/Xet
implementation, but model-mirror does not intentionally reconstruct a second
full payload there. Interrupted partial files and staging cache are retained
for resume. Every staging directory contains a small operation record identifying
the repository and pinned target commit. Successful operations remove their
staging directory, so steady state contains no runtime cache.

`status` performs a shallow check of those records. It prints no cache section
in steady state. Interrupted or leftover staging is reported as `stale-cache`
with both the exact resume command and an explicit discard command; cache that
cannot be associated with a known operation is reported as `untracked-cache`.
This check does not walk or size the cached files.

`model-mirror clean-cache` recursively sizes and previews reclaimable cache
space. `clean-cache --force` removes it without deleting mirrored payloads, but
refuses to run while repository or runtime-cache locks show active work. It also
detects legacy archive-root `.cache/` and `.tmp/` directories left by older
versions. The `.model-mirror/` control directory itself remains in place.

## Locking And Interrupted Commands

Repository operations use an advisory kernel `flock` on
`.verification.lock`. `mirror`, `remove`, `verify`, `repair`, `offline`, `online`,
`upgrade`, and torrent control-plane transitions take this per-repository lock;
the managed seeder holds it only during short reconciliation transitions.
`list` and `status` remain non-blocking and report the command, PID, host, and
start time for a lock that is actually held.

The kernel releases the lock automatically when a process exits or the host
restarts. The `.verification.lock` file may remain after a crash, but its
presence alone does not mean the repository is locked; model-mirror probes the
lock itself and ignores stale contents. Do not delete the file to override a
genuinely running operation. After a killed download, rerun the same `mirror`
command to resume its pinned snapshot.

## Removing A Mirror

`remove` prints the repository identity, type, path, status, exceptional state,
resolved commit, last verification time and age, payload file count, and exact
size. By default it requires typing the full repository ID; `--yes` is available
for deliberate automation:

```bash
model-mirror remove org/model
model-mirror remove --repo-type dataset org/data
model-mirror remove --yes org/model
```

Removal is blocked while a torrent publication fence is active. Stop any
external client first when applicable, then release the local publication:

```bash
model-mirror torrent stop org/model
model-mirror torrent retire org/model
model-mirror remove org/model
```

After confirmation, model-mirror atomically moves the mirror from its canonical
path into `.model-mirror-removals/` under the same archive. It removes payload
files first, ordinary metadata next, and the preserved removal record and lock
last. If the command or host stops partway through, rerun the same `remove`
command to resume. A new mirror created at the original path while an older
removal is pending is never deleted by that resumed cleanup.

Torrent retirement releases local managed seed intent and the update fence, but
cannot revoke torrent metadata already shared with other peers. Removing a
repository does not remove archive-wide cache; inspect that separately with
`model-mirror clean-cache`.

## Notes

Model-mirror uses Hugging Face metadata and the HTTP/Xet transports, selecting
Xet automatically when available. It keeps Hugging Face cache and temporary
directories under the configured archive root so large downloads do not spill
into the default home cache.

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for implementation details, testing, and
future design notes.
