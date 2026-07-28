from __future__ import annotations

import argparse
from pathlib import Path
import tomllib


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that a release tag matches pyproject.toml.")
    parser.add_argument("tag", help="release tag, for example v0.2.0")
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args()

    document = tomllib.loads(args.project.read_text(encoding="utf-8"))
    version = document["project"]["version"]
    expected = f"v{version}"
    if args.tag != expected:
        parser.error(f"release tag {args.tag!r} does not match package version {version!r}; expected {expected!r}")
    print(f"release tag {args.tag} matches model-mirror {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
