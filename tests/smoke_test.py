from __future__ import annotations

import importlib.metadata
import subprocess
import sys


def main() -> None:
    expected = importlib.metadata.version("model-mirror-cli")
    completed = subprocess.run(
        ["model-mirror", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = completed.stdout.strip()
    if actual != f"model-mirror {expected}":
        raise SystemExit(f"unexpected version output: {actual!r}")
    subprocess.run(
        ["model-mirror", "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(f"model-mirror {expected} smoke test passed")


if __name__ == "__main__":
    main()
