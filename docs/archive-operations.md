# Archive Operations

This guide covers status, verification, repair, upstream updates, periodic
maintenance, interrupted work, cache cleanup, and mirror removal. Start with
the project [README](../README.md) for installation and the basic workflow.

## Status

Without a repository argument, `model-mirror status` prints a compact,
metadata-only archive table with repository type and ID, recorded file count
and payload size, abbreviated resolved commit, verification age, and
exceptional state. A healthy row uses `-` in `EXCEPTIONS` instead of repeating
`clean`. Torrent and live activity columns appear only when relevant.

Status reads existing verification, manifest, snapshot, lock, progress,
torrent, and shallow runtime-cache operation records. It does not recursively
scan payload or cache directories, calculate cache sizes, read payload bytes,
contact the Hub, or update metadata. Missing recorded counts or sizes are shown
as unknown; use `verify` to reconcile the local filesystem.

Pass a repository ID for a detailed, strictly local last-known report:

```bash
model-mirror status
model-mirror status org/model
model-mirror status --repo-type dataset org/data
model-mirror status --verbose org/model
model-mirror status --check-upstream org/model
model-mirror status --json
```

The default detail view emphasizes verification state and age, resolved commit,
recorded payload size, upstream state, torrent publication, exceptional state,
and useful next actions. Issues and repair paths appear only when present.
`--verbose` prints every recorded field. A mismatch between the verification
commit and authoritative snapshot description is reported as `snapshot-stale`.

`--check-upstream` performs an advisory live comparison between the locally
resolved commit and the commit currently resolved by the requested upstream
revision. It does not write verification state, snapshot metadata, timestamps,
or locks. The recorded upstream state remains separate from the live
observation in JSON.

`--json` emits the versioned `model-mirror-status` schema with one
`repositories` array for both single-repository and archive-wide output.
Numbers remain numeric, timestamps use ISO 8601, and issues and repair paths are
arrays. Exceptional runtime data appears in the top-level `cache` array and on
its associated repository; the array is empty in steady state. `--verbose` is
accepted as a no-op with `--json`.

The deprecated `model-mirror list` spelling remains a compatibility alias.

## Verification

`mirror` verifies by default. A clean mirror has:

- every expected Hub path present as a canonical regular file
- expected file sizes
- local SHA-256 and Git blob SHA-1 hashes in a versioned `.manifest`
- LFS hashes compared with Hub LFS SHA-256 metadata
- regular Git files compared with Hub Git blob IDs
- `.verification` with `status: clean`

Model formats are opaque to model-mirror. It does not require or parse
`config.json`, Safetensors, GGUF, ONNX, or framework-specific layouts. It
verifies the pinned upstream file list, safe regular-file paths, sizes, hashes,
and mirror metadata. Format compatibility belongs to the software consuming
the mirror.

By default, unexpected payload files do not make an otherwise correct mirror
dirty. Use `--strict` to report them and fail verification. Model-mirror's own
state and cache paths are never treated as payload extras.

```bash
model-mirror verify org/model
model-mirror verify --cached org/model
model-mirror verify --offline org/model
model-mirror verify --strict org/model
model-mirror verify --progress org/model
model-mirror verify --all
model-mirror verify --all --max-age 7d
```

`--cached` checks presence, sizes, and Hub-provided hashes from current
`.manifest` rows without rehashing payload files. If cached hash data is missing
or stale, it exits non-zero and tells you to run full verification.

`--offline` does not contact the Hub and therefore cannot detect whether the
upstream repository moved. Full offline verification requires an existing
`.manifest`; `--offline --cached` reports the current `.verification` state.

Every completed verification reports files checked, total payload size, bytes
actually hashed, and elapsed time. Live aggregate hash progress appears once
per second on an interactive terminal. Use `--progress` to force it when output
is redirected or `--no-progress` to suppress it. Progress goes to stderr; the
final result remains on stdout.

Failed verification prints categorized paths for missing files, size
mismatches, hash mismatches, unavailable cached hashes, unexpected files, and
unsafe payload paths, followed by an exact repair or full-verification command.
Long lists are capped; use `status --verbose REPO` for the complete recorded
issue list.

If one repository is locked, a single-repository verification exits non-zero.
`verify --all` reports it as skipped, continues through the remaining archive,
and exits non-zero at the end.

### Upstream unavailable

Online verification preserves an existing local mirror if the upstream
repository cannot be reached or no longer exists. It exits non-zero and prints:

```bash
model-mirror offline org/model
```

Offline-only mirrors use local verification and stop checking whether the Hub
repository moved or disappeared. Re-enable Hub checks with:

```bash
model-mirror online org/model
```

## Repair

Verification records missing or corrupt files as repair paths. Repair consumes
that state and restores only those paths from the mirror's recorded commit:

```bash
model-mirror verify org/model
model-mirror repair org/model
```

Repair never moves to a newer upstream commit unless `--update` is explicitly
provided. It prints the age of the verification result, warns after 24 hours,
updates manifest rows for repaired files, and performs a final cached
verification. Unchanged large files are not rehashed after the initial full
verification.

Repair also reconciles a pinned snapshot plan older than the clean verification
state. It fetches metadata for the recorded commit and reuses current manifest
hashes, avoiding payload rereads when cached evidence is complete.

If cached verification data for untouched files is incomplete, repair stops
before downloading and requests a full verification. `--force-partial`
overrides that safety check but can leave the repository inconsistent and still
exits non-zero if the final cached verification cannot prove the result.

Offline-only mirrors cannot be repaired from the Hub. A direct repair explains
the limitation; `repair --all` warns and skips them.

## Upstream Updates

Every online operation resolves the requested revision, normally `main`, to a
specific commit. If verification sees that the revision now resolves to a
different commit, it records `upstream_status: changed` without modifying local
files.

Repair and update are deliberately separate:

- `repair` restores the currently recorded commit.
- `repair --update` moves to the changed upstream commit recorded by
  verification.

Preview the complete transition before applying it:

```bash
model-mirror diff org/model
model-mirror diff --verbose org/model
model-mirror diff --json org/model
model-mirror repair --update org/model
model-mirror repair --all --update
```

`diff` is advisory. It uses the current and upstream commits recorded by the
last verification, contacts the Hub for their file metadata, and does not
download payload files or alter local mirror metadata. It reports:

- current and target commits
- added, changed, removed, and byte-identical reusable paths
- current and target payload sizes
- candidate download bytes
- removed-file and removed-byte percentages

Normal output caps very long path groups and prints the exact `--verbose`
command for the complete inventory. `--json` emits the versioned
`model-mirror-diff` schema with complete added, changed, removed, and reusable
path groups. The older `repair --update --dry-run` spelling remains available
for compatibility. Applying the update removes only paths made obsolete by the
prior pinned snapshot; unrelated local extras remain.

Use an exact commit when creating a reproducible fixed archive:

```bash
model-mirror mirror --commit abc123 org/model
```

An active torrent publication fences the canonical mirror against commit
updates. See the [torrent guide](torrent.md) for retirement and same-commit
maintenance behavior.

## Periodic Maintenance

A typical periodic job is:

```bash
#!/usr/bin/env bash
set -u

model-mirror verify --all --max-age 30d || true
model-mirror repair --all
```

Do not connect verification and repair with `&&`: verification exits non-zero
when it discovers repairable damage. Busy mirrors are skipped while the rest of
the archive continues.

For cron, use the same commands and direct output to a log:

```cron
0 3 * * 0 /usr/local/bin/model-mirror verify --all --max-age 30d >>/var/log/model-mirror.log 2>&1
0 4 * * 0 /usr/local/bin/model-mirror repair --all >>/var/log/model-mirror.log 2>&1
```

## Runtime Cache And Interrupted Work

Each download staging directory contains a small operation record identifying
the repository and pinned target commit. Successful operations remove staging,
so steady state has no runtime cache.

`status` performs a shallow inspection without walking or sizing cached files.
It reports:

- `stale-cache` for identified interrupted staging, with exact resume and
  discard commands
- `untracked-cache` for runtime data that cannot be associated with a known
  operation

Inspect reclaimable space without deleting anything:

```bash
model-mirror clean-cache
```

Delete disposable cache explicitly:

```bash
model-mirror clean-cache --force
```

Forced cleanup refuses to run while repository or runtime-cache locks show
active work. It also detects legacy archive-root `.cache/` and `.tmp/`
directories. The archive control directory itself remains.

## Locks And Crashes

Repository operations use an advisory kernel `flock` on
`.verification.lock`. `mirror`, `remove`, `verify`, `repair`, `offline`,
`online`, `upgrade`, and torrent control-plane transitions take this lock. The
managed seeder holds it only during short reconciliation transitions.

`status` remains non-blocking and reports the command, PID, host, and start time
for a lock that is actually held. The kernel releases the lock automatically
when a process exits or the host restarts. The lock file can remain after a
crash; its presence alone does not mean the repository is locked. Do not delete
it to override genuinely active work.

After a killed download, rerun the same `mirror` command to resume its pinned
snapshot.

## Removing A Mirror

`remove` displays identity, type, path, status, exceptional state, resolved
commit, verification time and age, payload file count, and exact size before
confirmation:

```bash
model-mirror remove org/model
model-mirror remove --repo-type dataset org/data
model-mirror remove --yes org/model
```

By default, type the complete repository ID to confirm. `--yes` is intended for
deliberate automation.

Removal is blocked while a torrent publication fence is active:

```bash
model-mirror torrent stop org/model
model-mirror torrent retire org/model
model-mirror remove org/model
```

After confirmation, model-mirror atomically moves the mirror from its canonical
path into `.model-mirror-removals/` on the same archive. It removes payload
files first, ordinary metadata next, and the preserved removal record and lock
last. If interrupted, rerun the same command to resume. A new mirror created at
the original path while an older removal is pending is never deleted by resumed
cleanup.

Removing one repository does not clear archive-wide cache. Inspect that
separately with `clean-cache`.
