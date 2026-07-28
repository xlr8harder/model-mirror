from __future__ import annotations

import argparse
from pathlib import Path
import re


HEADING = re.compile(r"^## \[([^\]]+)\](?: - .+)?$", re.MULTILINE)


def release_notes(document: str, version: str) -> str:
    selected = version.removeprefix("v")
    headings = list(HEADING.finditer(document))
    for index, heading in enumerate(headings):
        if heading.group(1) != selected:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(document)
        notes = document[heading.end() : end].strip()
        if not notes:
            raise ValueError(f"changelog section {selected!r} is empty")
        return notes
    raise ValueError(f"changelog section {selected!r} was not found")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print release notes from CHANGELOG.md.")
    parser.add_argument("version", help="package version or release tag, for example 0.2.1 or v0.2.1")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args()
    try:
        notes = release_notes(args.changelog.read_text(encoding="utf-8"), args.version)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
