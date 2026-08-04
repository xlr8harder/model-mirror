# model-mirror

Mirror Hugging Face repositories into local bulk storage, pin them to exact
commits, and verify that their files remain complete.

`model-mirror` streams payload directly into one archive directory instead of
leaving model-sized files in the default Hugging Face cache. It records the
resolved Hub commit, local SHA-256 and Git blob hashes, the expected file list,
and verification state beside each mirror.

If upstream later moves, disappears, or serves changed content, the existing
local mirror is not silently updated.

## Quick Start

Install the `model-mirror-cli` distribution from PyPI with
[uv](https://docs.astral.sh/uv/) using Python 3.11 or newer:

```bash
uv tool install model-mirror-cli

model-mirror config directory /mnt/big-drive/huggingface
model-mirror mirror org/model
model-mirror status
```

If Hugging Face token autodetection does not find the intended token:

```bash
model-mirror config set token-path ~/.cache/huggingface/token
```

For HDD-backed archives, sequential Xet reconstruction is usually preferable:

```bash
model-mirror config set hf-xet-reconstruct-write-sequentially true
```

The distribution is named `model-mirror-cli` to distinguish it from an
unrelated package; the installed command remains `model-mirror`.

## The Safety Model

- Requested branches and tags resolve to concrete Hub commits before download.
- Mirror, verification, and repair stay pinned to that recorded commit.
- Downloads stream into resumable `.incomplete` files while hashes accumulate.
- Completed files are verified and atomically renamed into place.
- `repair` restores the recorded commit; it never applies a newer commit.
- Upstream updates require an explicit, inspectable `repair --update`.
- If upstream disappears, local verification and torrent recovery remain
  available.

Model formats are opaque bytes. Model-mirror does not impose assumptions about
Safetensors, GGUF, ONNX, `config.json`, or framework-specific layouts.

## Everyday Workflow

Mirror and verify:

```bash
model-mirror mirror org/model
model-mirror verify org/model
model-mirror repair org/model  # only if verification reports repair paths
```

`mirror` verifies by default. Full verification reports file count, payload
size, bytes hashed, duration, and interactive progress. Cached verification can
avoid rereading payload:

```bash
model-mirror verify --cached org/model
model-mirror verify --progress org/model
```

Archive status is metadata-only and normally returns quickly, even for large
HDD mirrors:

```bash
model-mirror status
model-mirror status org/model
model-mirror status --check-upstream org/model
model-mirror status --json
```

`--check-upstream` is advisory and does not update local metadata.

When verification detects that upstream moved, preview the exact transition
before applying it:

```bash
model-mirror diff org/model
model-mirror diff --verbose org/model
model-mirror diff --json org/model
model-mirror repair --update org/model
```

The preview shows commits, added, changed, removed, and reusable files, payload
size changes, candidate downloads, and removal percentages.

For periodic archive maintenance:

```bash
model-mirror verify --all --max-age 30d || true
model-mirror repair --all
```

Do not join those commands with `&&`: verification exits non-zero when it finds
repairable damage.

## Experimental Torrent Recovery

Torrent support is optional and experimental:

```bash
uv tool install --force 'model-mirror-cli[torrent]'
```

Publish one immutable `repo@resolved-commit`, or recover it elsewhere:

```bash
model-mirror torrent publish org/model
model-mirror torrent serve

model-mirror torrent join /path/model@commit.torrent
model-mirror torrent join 'magnet:?xt=...' --seed
```

Publications are deterministic hybrid v1/v2 torrents. Managed seed intent
survives restart, standard torrent clients can use the emitted artifacts, and
native join finalizes a normal model-mirror archive without a second complete
payload copy.

An active publication prevents the canonical mirror from moving to another
commit until it is explicitly retired. Torrent consistency and upstream
authenticity are recorded separately.

See the [torrent guide](https://github.com/xlr8harder/model-mirror/blob/main/docs/torrent.md)
for publication, managed seeding, external-client handoff, import, trust, repair,
and retirement.

## Removal And Interrupted Work

Remove a mirror through the resumable, confirmed lifecycle:

```bash
model-mirror remove org/model
```

`remove` displays the repository, commit, verification age, file count, and size
before confirmation. Active torrent publications must be retired first.

Successful downloads leave no runtime cache in steady state. `status` reports
identified interrupted staging with exact resume and cleanup commands. Inspect
or remove disposable runtime cache explicitly:

```bash
model-mirror clean-cache
model-mirror clean-cache --force
```

Kernel-backed repository locks are released when a process or host exits; a
leftover `.verification.lock` file alone does not indicate active work.

## Command Map

```bash
model-mirror mirror org/model
model-mirror verify org/model
model-mirror verify --cached org/model
model-mirror repair org/model
model-mirror diff org/model
model-mirror status
model-mirror status --json
model-mirror offline org/model
model-mirror online org/model
model-mirror remove org/model
model-mirror clean-cache
model-mirror torrent publish org/model
model-mirror torrent join FILE_OR_MAGNET
model-mirror version
```

Omitting `--repo-type` uses the configured default, initially `model`. Dataset
and Space repositories use `--repo-type dataset` or `--repo-type space`.

Run `model-mirror --help`, `model-mirror COMMAND --help`, or
`model-mirror config options` for the authoritative CLI reference and exit
status behavior.

## Documentation

- [Archive Operations](https://github.com/xlr8harder/model-mirror/blob/main/docs/archive-operations.md):
  status, verification, repair, updates, periodic jobs, locks, cache, and removal
- [Configuration And Storage](https://github.com/xlr8harder/model-mirror/blob/main/docs/configuration-and-storage.md):
  archive layout, repository types, authentication, HDD/Xet tuning, and disk use
- [Experimental Torrent Guide](https://github.com/xlr8harder/model-mirror/blob/main/docs/torrent.md):
  publication, seeding, recovery, external clients, trust, and lifecycle
- [Torrent Requirements](https://github.com/xlr8harder/model-mirror/blob/main/docs/torrent-distribution-requirements.md):
  normative identity, safety, and compatibility requirements

## Installation Updates

```bash
uv tool upgrade model-mirror-cli
model-mirror --version
model-mirror version
```

`--version` is offline. `version` checks PyPI and prints all missed release
notes plus the update command when the installation is out of date.

For development:

```bash
git clone https://github.com/xlr8harder/model-mirror.git
cd model-mirror
uv sync --locked --all-extras --dev
uv run coverage run -m pytest -q
uv run coverage report -m
```

Contributor and implementation details are in
[CONTRIBUTING.md](https://github.com/xlr8harder/model-mirror/blob/main/CONTRIBUTING.md).
Model-mirror is licensed under the MIT License.
