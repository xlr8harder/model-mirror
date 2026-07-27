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

`model-mirror` is not currently published on PyPI. Install it from a source
checkout with [uv](https://docs.astral.sh/uv/) (Python 3.11 or newer):

```bash
git clone https://github.com/xlr8harder/model-mirror.git
cd model-mirror
uv tool install .

model-mirror config directory /mnt/big-drive/huggingface
model-mirror config set hf-xet-reconstruct-write-sequentially true  # useful for HDDs
# Optional if token autodetection does not find your Hugging Face token:
model-mirror config set token-path ~/.cache/huggingface/token

model-mirror mirror org/model
model-mirror status
model-mirror verify org/model
model-mirror repair org/model  # if verify reports repair paths
```

For development inside the checkout, use `uv sync` and prefix commands with
`uv run`. The rest of this README assumes the tool installation above.

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
```

Run `model-mirror --help` or `model-mirror COMMAND --help` for the full CLI
reference. Run `model-mirror config options` for every supported config key.
Commands exit non-zero for dirty, incomplete, busy, or invalid states where that
matters; see each subcommand's help for exact exit-status behavior.

`model-mirror status` gives an archive-wide operational view: configured
archive roots, mirror count and payload size, cache and temporary usage, and
each repository's size, verification state, last check, active lock, and live
file progress. Torrent state is included when present. `model-mirror list` is
currently an alias for the same output. State tags include values such as
`offline` and `needs-repair`.

## Verification

`mirror` verifies by default. A clean mirror has:

- all expected Hub files present
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

## Experimental Torrent Publishing And Recovery

Torrent support is optional and experimental. The `upgrade` and `torrent`
commands, their behavior, and the torrent-specific metadata formats may change
before stabilization. Ordinary mirror, verify, and repair workflows remain
stable and do not require the torrent extra:

```bash
# Reinstall the source checkout as a uv tool with torrent support:
uv tool install --force '.[torrent]'

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
model-mirror list                          # show mirrors, state tags, and verification age
model-mirror status                        # archive sizes, cache use, locks, progress, and torrent state
model-mirror upgrade org/model             # fill missing torrent hash coverage
model-mirror torrent publish org/model     # publish and request durable seeding
model-mirror torrent join FILE_OR_MAGNET   # recover a normal local archive
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
transport cache under `DIRECTORY/.tmp/downloads`. The auxiliary cache is not
specified as a fixed percentage and can vary with the HTTP/Xet implementation,
but model-mirror does not intentionally reconstruct a second full payload
there. Interrupted partial files and staging cache are retained for resume.
`model-mirror status` reports cache and temporary usage, and
`model-mirror clean-cache` previews reclaimable cache space before
`clean-cache --force` removes it without deleting mirrored payloads. Do not run
forced cache cleanup while `status` shows an active download or repair.

## Locking And Interrupted Commands

Repository operations use an advisory kernel `flock` on
`.verification.lock`. `mirror`, `card`, `verify`, `repair`, `offline`, `online`,
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

There is no `remove` command or global repository database. A repository and
all of its model-mirror state live in one directory, so removal is an explicit
filesystem operation:

```bash
model-mirror status
# For a published torrent, stop external seeding if applicable, then:
model-mirror torrent retire org/model
rm -r -- /mnt/big-drive/huggingface/models/org/model
```

Do not remove a repository reported as busy. For datasets or Spaces, use the
corresponding path below `datasets/` or `spaces/`. Torrent retirement releases
local managed seed intent and the update fence, but cannot revoke torrent
metadata already shared with other peers. Removing a repository does not remove
archive-wide cache; inspect that separately with `model-mirror clean-cache`.

## Notes

Model-mirror uses Hugging Face metadata and the HTTP/Xet transports, selecting
Xet automatically when available. It keeps Hugging Face cache and temporary
directories under the configured archive root so large downloads do not spill
into the default home cache.

See [CONTRIBUTORS.md](CONTRIBUTORS.md) for implementation details, testing, and
future design notes.
