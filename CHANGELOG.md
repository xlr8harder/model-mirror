# Changelog

Notable user-facing changes are recorded here. The release workflow copies the
matching version section into the corresponding GitHub Release.

## [Unreleased]

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
