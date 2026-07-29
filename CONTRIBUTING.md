# Contributing

Start with [AGENTS.md](AGENTS.md). It defines the preservation, safety,
testability, CLI, and documentation contract for the whole repository. This
guide contains the development mechanics and implementation details used to
apply that contract.

User-facing usage belongs in [README.md](README.md) and the focused guides
under [`docs/`](docs/).

## Development Environment

Model-mirror supports Python 3.11 through 3.13. Set up the locked environment
with all optional and development dependencies:

```bash
uv sync --locked --all-extras --dev
```

Run the complete test and coverage gate:

```bash
uv run coverage run -m pytest -q
uv run coverage report -m
```

The final report must retain 100% statement and branch coverage. The gate is
configured in `pyproject.toml`.

Run a focused test while iterating, but do not substitute it for the complete
gate:

```bash
uv run pytest tests/test_NAME.py -q
```

For packaging, dependency, entry-point, or README metadata changes, also build
and smoke-test both distributions:

```bash
uv build --no-sources
uv run --isolated --no-project --with dist/*.whl tests/smoke_test.py
uv run --isolated --no-project --with dist/*.tar.gz tests/smoke_test.py
```

## Test Design

Begin behavior changes by identifying the observable contract:

1. Add or update the smallest tests that demonstrate the intended behavior or
   regression.
2. Implement the change.
3. Run focused tests while iterating.
4. Run the complete coverage gate before considering the work finished.

Tests should protect behavior and invariants rather than mirror prose or
private implementation structure. A requirement does not automatically need a
dedicated test when it is already covered by a stronger invariant, static
structure, review, or documentation. Coverage should not produce dozens of
near-duplicate examples that add little confidence.

Prefer these testing boundaries:

- Keep identity comparison, update planning, validation, and state transitions
  in small deterministic functions where practical.
- Inject Hub clients, seeder sessions, clocks, and process observations rather
  than requiring live services in ordinary tests.
- Use temporary real filesystems for path safety, atomic replacement, locks,
  manifests, and interrupted operations. Mocking `Path` methods is weaker
  evidence for filesystem behavior.
- Exercise defensive refusal as well as successful mutation.
- Cover crash/restart and partial-state behavior for resumable workflows.
- Assert that advisory and dry-run operations do not alter local metadata or
  payloads.
- Test JSON as a schema, including its schema version and value types. Test
  human output for important information and actions without freezing harmless
  spacing.
- Keep live-network tests bounded, opt-in, and separate from the deterministic
  default suite.

Common regression families include:

- an upstream branch moving without implicit local mutation
- upstream disappearance while the local archive remains preserved
- same-commit repair versus an explicit commit update
- update plans containing added, changed, removed, and reusable files
- traversal, symlink, duplicate-path, and reserved-path rejection
- stale locks versus locks held by active work
- interrupted downloads, cache cleanup, removal, and restart
- torrent restart, update fencing, retirement, maintenance, and import trust
- avoidance of unnecessary unchanged-file payload reads

## Archive State

Each mirrored repository owns its state inside the repository directory:

- `.verification`: YAML status, timestamps, commit metadata, offline-only flag,
  issues, and repair paths
- `.manifest`: versioned JSONL records containing local size, modification
  time, SHA-256, and Git blob SHA-1
- `.verification.lock`: advisory lock metadata while an operation is active
- `.model-mirror/snapshot.json`: authoritative commit-pinned expected file list
- `.model-mirror/torrent/coverage/`: atomic, resumable, profile-specific piece
  and Merkle hash coverage
- `.model-mirror/torrent/publications/`: torrent artifacts, recovery records,
  and durable publication and desired-seed state
- `.model-mirror/torrent/fence.json`: the active commit-update fence

There is no global model-state database. Deleting a repository directory
removes its payload and local control state together; the CLI removal workflow
exists to make the target and impact explicit.

State formats are durable compatibility boundaries. Version them, validate
them defensively, and reject malformed or unknown future versions rather than
silently rewriting them.

## Commits, Verification, And Updates

Online operations resolve the requested revision to a concrete Hub commit
before downloading or verifying. A clean local mirror is trusted for its
recorded resolved commit.

If upstream moves, verification records the change but does not mutate local
files. `repair` restores the recorded commit. `repair --update --dry-run`
previews the recorded transition, and `repair --update` is the explicit
operation that moves the archive to the new commit.

If upstream is unavailable, verification exits nonzero and preserves existing
local verification state. `offline` records that future verification should be
local-only; `online` re-enables upstream checks. Upstream failure alone is not
evidence that a local snapshot is corrupt.

Updates may delete only paths owned by the prior pinned snapshot. Preserve
unrelated local extras. Active torrent publications fence a commit-changing
update until explicitly retired.

## Checksums And I/O

Manifest writes are incremental. After each file is hashed, `.manifest` is
atomically rewritten with a schema/version header and one record per payload
file. Each payload pass computes SHA-256 and Git blob SHA-1 together. Later
runs skip files whose size, modification time, and recorded hashes remain
valid.

Downloads accumulate integrity hashes and enabled torrent coverage while
streaming into the destination's `.incomplete` file. This supports resumption
without requiring a second full-file hash pass.

Avoid introducing whole-payload reads into metadata-only commands. When a new
feature needs hash evidence, first determine whether existing authoritative
manifest or torrent coverage can answer it. Treat sequential I/O, bounded
memory, and minimal full copies as functional requirements for HDD-backed
multi-terabyte archives.

## Locking And Interruption

Mutating mirror, verification, repair, upgrade, removal, and torrent
control-plane transitions take the target repository's advisory
`.verification.lock`. The first mirror operation records `in_progress` before
downloading.

The seeder acquires the lock only for short reconciliation transitions, not
for its entire lifetime. Publication fences, rather than a permanently held
process lock, prevent commit-changing updates.

Read-only summary commands must not block on repository locks; they may report
last-known busy metadata. Code that clears stale state must distinguish a
dead owner from active work and must resolve the exact repository or cache
identity before deleting anything.

## Hugging Face And Xet

Model-mirror uses `huggingface_hub` to resolve pinned metadata and transport
URLs, then streams HTTP or Xet bytes through its own resumable hashing writer.

Transport environment is derived from model-mirror configuration and is
authoritative. When a configured boolean is false, inherited Xet environment
variables for that feature are removed before the transport is imported or
used.

Conservative defaults keep HDD-backed and lower-memory systems usable:

- high-performance Xet mode disabled
- range-get concurrency of `1`
- optional sequential reconstruction writes

Do not raise I/O or concurrency defaults based only on SSD behavior.

## Torrent Boundaries

Torrent support is an optional distribution and recovery transport around a
verified, commit-pinned snapshot. It is not an alternate source of upstream
authority.

- Publication artifacts are immutable and per resolved commit.
- Piece and Merkle coverage may be reused only when its profile and file
  identity match.
- Same-commit repair may refresh affected coverage; commit-changing updates
  require publication retirement.
- Backend completion, a valid infohash, or successful piece checks do not
  replace model-mirror provenance verification.
- Generated torrent files and magnets should remain usable with ordinary
  external clients.
- Core archive workflows must work when the torrent extra is not installed.

The normative lifecycle and identity rules are in
[Torrent Distribution And Archive Upgrade Requirements](docs/torrent-distribution-requirements.md).
Backend evidence and tradeoffs are in
[Torrent Backend Spike](docs/torrent-backend-spike.md).

## CLI Design Details

Human output is the default interface:

- Use aligned columns for archive-wide comparisons and labeled sections for
  single-repository detail.
- Lead with current state and impact rather than internal metadata order.
- Include useful file counts, byte sizes, commits, ages, durations, and paths.
- Keep healthy summary noise low while retaining informative targeted detail.
- Categorize failures and print an exact safe next command where possible.
- Summarize potentially huge path lists and provide a clear verbose or
  machine-readable way to retrieve all entries.

Add `--json` when a command reports state or plans that callers are likely to
automate. JSON must have an explicit schema version and stable domain fields;
do not serialize internal dataclasses directly. Preserve numbers, booleans,
arrays, and ISO 8601 timestamps as their native JSON types.

Human prose and progress must not contaminate JSON stdout. Progress belongs on
stderr and should be forceable or suppressible for redirected use. A
human-only formatting option may be accepted as a no-op under `--json` if that
keeps command composition predictable.

Commands that appear read-only must not mutate state. Commands that appear
cheap must not hide upstream calls, recursive scans, or payload hashing. Use
explicit flags or separate operations for upstream comparison, full
verification, cleanup, and update application.

Keep terminology consistent across human output, JSON, help, documentation,
and state files. CLI configuration accepts kebab-case spellings while stored
and documented keys use canonical snake_case. Deprecated aliases should be
identified as aliases rather than presented as separate concepts.

Every subcommand help should state its purpose and meaningful exit status.
Bare invocation and topic help should remain useful.

## Documentation Ownership

Put information in one authoritative home:

- `README.md`: purpose, installation, safety model, everyday workflow, compact
  command map, and links
- `docs/archive-operations.md`: status, verification, repair, updates, cache,
  locks, interruption, and removal
- `docs/configuration-and-storage.md`: configuration, authentication, layout,
  repository types, Xet/HDD tuning, and disk-space behavior
- `docs/torrent.md`: user-facing experimental torrent publishing, seeding,
  external-client, recovery, trust, repair, and retirement workflows
- `docs/torrent-distribution-requirements.md`: normative torrent identity and
  lifecycle requirements
- `docs/torrent-backend-spike.md`: backend evaluation and evidence
- `AGENTS.md`: high-level invariants and ways of working
- `CONTRIBUTING.md`: development, implementation, testing, and output details
- `RELEASING.md`: PyPI and GitHub release mechanics
- `CHANGELOG.md`: curated user-visible changes
- CLI `--help`: authoritative flags, arguments, and exit-status behavior

When behavior changes, update the owning guide and CLI help in the same change.
Keep the README concise and link to detail rather than duplicating it. README
links to repository documents must work when rendered on PyPI, which generally
requires absolute GitHub URLs.

## Change And Release Discipline

- Inspect current source and tests before trusting design notes or handoffs.
- Preserve unrelated user changes in a dirty worktree.
- Avoid new dependencies when the standard library or current dependency set
  is sufficient.
- Update `uv.lock` whenever dependency metadata changes.
- Record user-visible work under `[Unreleased]` in `CHANGELOG.md`.
- Do not change versioned schemas or torrent identity behavior in place.
- Commit every completed change; do not leave intended work only in the working
  tree.
- Commits and pushes are not inherently releases. Only bump versions, create
  tags, or publish when a release is explicitly requested.
