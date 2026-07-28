import pytest

from scripts.changelog import release_notes


def test_release_notes_extracts_exact_version_section():
    document = """# Changelog

## [Unreleased]

Pending.

## [1.1.0] - 2026-07-28

First line.

Second line.

## [1.0.0] - 2026-07-27

Old.
"""

    assert release_notes(document, "v1.1.0") == "First line.\n\nSecond line."
    assert release_notes(document, "1.0.0") == "Old."


def test_release_notes_rejects_missing_or_empty_sections():
    document = """## [Unreleased]

## [1.0.0] - 2026-07-27

Notes.
"""

    with pytest.raises(ValueError, match="section '1.1.0' was not found"):
        release_notes(document, "1.1.0")
    with pytest.raises(ValueError, match="section 'Unreleased' is empty"):
        release_notes(document, "Unreleased")
