# Changelog

Notable user-facing changes are recorded here. The release workflow copies the
matching version section into the corresponding GitHub Release.

## [Unreleased]

### Changed

- The README is now a concise installation and everyday-workflow entry point;
  detailed archive operations, configuration/storage, and experimental torrent
  guidance moved into focused documents under `docs/`.
- Added a repository-wide agent contract and consolidated detailed development
  guidance in the conventional `CONTRIBUTING.md`.

## [0.2.3] - 2026-07-29

### Added

- `repair --update --dry-run` previews the recorded commit transition, file and
  payload-size deltas, candidate downloads, removals, and reusable files without
  mutating the mirror. `--verbose` shows every affected path, and removal impact
  is reported as both file and byte percentages.
- Verification now reports file count, payload size, bytes hashed, and duration,
  with automatic interactive progress plus explicit `--progress` and
  `--no-progress` controls.

### Changed

- `model-mirror list` is now a deprecated compatibility alias shown with
  `status` instead of appearing as a separate top-level command.
- Verification output links changed upstreams to both preview and apply
  commands.
- Applying an upstream update removes paths made obsolete by the prior pinned
  snapshot while preserving unrelated local extras.

## [0.2.2] - 2026-07-28

### Added

- Status now reports only exceptional runtime-cache state, distinguishes
  identified interrupted staging from untracked data, and prints exact resume
  and cleanup commands.
- Download staging now records its repository and resolved commit so crash
  residue can be diagnosed without recursively scanning large archives.
- Verification failures now show categorized file-level causes and actionable
  next commands.
- Snapshot, download, checksum, repair, and publication paths now share
  canonical-path, duplicate-path, regular-file, and symlink safety checks.

### Fixed

- Verification no longer imposes format-specific file or layout requirements;
  model payload formats are treated as opaque bytes.
- Forced cache cleanup now refuses to run while repository or runtime-cache
  locks show active work.

## [0.2.1] - 2026-07-28

### Added

- Added `model-mirror version` and `model-mirror version --json` to compare the
  installed distribution with PyPI and show published release notes when an
  update is available.

### Changed

- All repo-targeted commands now use the configured `repo_type` when
  `--repo-type` is omitted. The initial configured default remains `model`.
- Archive-wide `list` and `status` still include every repo type, while torrent
  imports continue to use the type embedded in the publication descriptor.

## [0.2.0] - 2026-07-28

### Added

- Initial public PyPI release of the `model-mirror-cli` distribution and
  `model-mirror` command.
- Commit-pinned Hugging Face mirroring, verification, repair, explicit upstream
  updates, offline-only preservation, fast metadata status, and safe resumable
  removal.
- Experimental hybrid v1/v2 torrent publication, managed seeding, external
  client handoff, torrent recovery/import, and reusable hash coverage.
