"""Rewrite virtualenv launchers before a staging directory is renamed."""

from __future__ import annotations

import argparse
from pathlib import Path


def rewrite_launchers(venv: Path, old_prefix: Path, new_prefix: Path) -> None:
    bin_dir = venv / "bin"
    old_shebang = f"#!{old_prefix}/venv/bin/python".encode()
    new_shebang = f"#!{new_prefix}/venv/bin/python".encode()

    if not bin_dir.is_dir():
        raise ValueError(f"virtualenv bin directory is missing: {bin_dir}")

    for launcher in sorted(bin_dir.iterdir()):
        if not launcher.is_file() or launcher.is_symlink():
            continue
        content = launcher.read_bytes()
        first_line, separator, remainder = content.partition(b"\n")
        if first_line == old_shebang:
            launcher.write_bytes(new_shebang + separator + remainder)

    for launcher in sorted(bin_dir.iterdir()):
        if not launcher.is_file() or launcher.is_symlink():
            continue
        first_line = launcher.read_bytes().splitlines()[0] if launcher.read_bytes() else b""
        if first_line.startswith(old_prefix.as_posix().encode()):
            raise ValueError(f"virtualenv launcher still references staging: {launcher}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--old-prefix", type=Path, required=True)
    parser.add_argument("--new-prefix", type=Path, required=True)
    args = parser.parse_args()
    rewrite_launchers(args.venv, args.old_prefix, args.new_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
