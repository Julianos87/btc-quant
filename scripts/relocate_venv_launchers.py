"""Rewrite and validate virtualenv launchers after a staging directory is moved.

uv/venv may emit interpreter shebangs of the form ``python``, ``python3`` or
``python3.12``. Only the staging prefix of those interpreter paths is rewritten.
Unrelated system shebangs and binary files are left untouched.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

PYTHON_INTERPRETER_RE = re.compile(rb"python(?:\d+(?:\.\d+)*)?\Z")
VENV_PYTHON_DIR = "/venv/bin/"
SMOKE_LAUNCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gunicorn", ("--version",)),
    ("btcquant-trend", ("--help",)),
    ("btcquant-carry", ("--help",)),
    ("btcquant-readiness", ("--help",)),
    ("btcquant-carry-cutover", ("--help",)),
    ("btcquant-shadow", ("--help",)),
)
_UNSET_FOR_SMOKE = (
    "BTCQUANT_ROOT",
    "BTCQUANT_CURRENT",
    "BTCQUANT_DATABASE",
    "BTCQUANT_CLONE",
)


def _posix(path: Path) -> str:
    # Match the shebang text as written by uv/venv. Do not resolve() here:
    # launchers record the prefix path they were built with, not its canonical
    # physical location after symlink expansion.
    return Path(path).as_posix()


def _prefix_needles(prefix: Path) -> tuple[bytes, ...]:
    given = Path(prefix).as_posix().encode()
    resolved = Path(prefix).expanduser().resolve().as_posix().encode()
    if given == resolved:
        return (given,)
    return (given, resolved)


def _first_line(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(4096)
    newline = data.find(b"\n")
    if newline == -1:
        return data
    return data[:newline]


def _regular_executables(bin_dir: Path) -> list[Path]:
    launchers: list[Path] = []
    for launcher in sorted(bin_dir.iterdir()):
        if not launcher.is_file() or launcher.is_symlink():
            continue
        if launcher.stat().st_mode & 0o111:
            launchers.append(launcher)
    return launchers


def python_interpreter_name(first_line: bytes, prefix: Path) -> str | None:
    """Return the venv python interpreter name if ``first_line`` is a match."""

    line = first_line.rstrip(b"\r")
    expected = f"#!{_posix(prefix)}{VENV_PYTHON_DIR}".encode()
    if not line.startswith(expected):
        return None
    name = line[len(expected) :]
    if PYTHON_INTERPRETER_RE.fullmatch(name):
        return name.decode("ascii")
    return None


def rewrite_launchers(venv: Path, old_prefix: Path, new_prefix: Path) -> None:
    bin_dir = venv / "bin"
    if not bin_dir.is_dir():
        raise ValueError(f"virtualenv bin directory is missing: {bin_dir}")

    for launcher in _regular_executables(bin_dir):
        content = launcher.read_bytes()
        first_line, separator, remainder = content.partition(b"\n")
        interpreter = python_interpreter_name(first_line, old_prefix)
        if interpreter is None:
            continue
        new_shebang = f"#!{_posix(new_prefix)}{VENV_PYTHON_DIR}{interpreter}".encode()
        launcher.write_bytes(new_shebang + separator + remainder)

    assert_no_stale_prefix(venv, old_prefix)


def assert_no_stale_prefix(venv: Path, forbidden_prefix: Path) -> None:
    bin_dir = venv / "bin"
    if not bin_dir.is_dir():
        raise ValueError(f"virtualenv bin directory is missing: {bin_dir}")
    needles = _prefix_needles(forbidden_prefix)
    for launcher in _regular_executables(bin_dir):
        first_line = _first_line(launcher)
        if not first_line.startswith(b"#!"):
            continue
        if any(needle in first_line for needle in needles):
            raise ValueError(f"virtualenv launcher still references staging: {launcher}")


def assert_release_python_shebangs(release: Path) -> None:
    """Require venv python shebangs to name this release, not another prefix."""

    release = release.expanduser().resolve()
    bin_dir = release / "venv" / "bin"
    if not bin_dir.is_dir():
        raise ValueError(f"virtualenv bin directory is missing: {bin_dir}")
    expected_head = f"#!{_posix(release)}{VENV_PYTHON_DIR}".encode()
    for launcher in _regular_executables(bin_dir):
        first_line = _first_line(launcher)
        if not first_line.startswith(b"#!"):
            continue
        line = first_line.rstrip(b"\r")
        if b"/venv/bin/" not in line:
            continue
        if not line.startswith(expected_head):
            raise ValueError(f"virtualenv launcher does not target this release: {launcher}")
        name = line[len(expected_head) :]
        if not PYTHON_INTERPRETER_RE.fullmatch(name):
            # Console entrypoints must point at a venv python interpreter.
            # A shebang such as #!<release>/venv/bin/gunicorn is not expected.
            if name.startswith(b"python"):
                raise ValueError(f"unsupported venv python interpreter shebang: {launcher}")


def smoke_exec_launchers(release: Path) -> None:
    """Execute representative launchers from the final release path.

    Only ``--version`` / ``--help`` are invoked. Runtime databases are not
    opened by these flags for the selected entrypoints, except Carry ``--help``
    which reads the paper YAML from the release tree.
    """

    release = release.expanduser().resolve()
    env = {key: value for key, value in os.environ.items() if key not in _UNSET_FOR_SMOKE}
    bin_dir = release / "venv" / "bin"
    for name, args in SMOKE_LAUNCHERS:
        launcher = bin_dir / name
        if not launcher.exists():
            raise ValueError(f"required launcher missing: {launcher}")
        result = subprocess.run(
            [str(launcher), *args],
            cwd=str(release),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ValueError(
                f"post-move launcher smoke failed: {launcher} {' '.join(args)}"
                f" (exit {result.returncode}): {detail}"
            )


def validate_existing_release_launchers(release: Path) -> None:
    """Read-only runtime contract for a pre-existing release. Never mutates."""

    release = release.expanduser().resolve()
    python_bin = release / "venv" / "bin" / "python"
    if not python_bin.exists():
        raise ValueError(f"existing release python is missing: {python_bin}")
    assert_release_python_shebangs(release)
    smoke_exec_launchers(release)


def _resolved_link(path: Path) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return path.resolve()
    except OSError:
        return None


def quarantine_new_release(target: Path, root: Path) -> Path:
    """Move a newly created invalid TARGET aside. Never touches current/previous."""

    target = target.expanduser().resolve()
    root = root.expanduser().resolve()
    releases = (root / "releases").resolve()
    current = _resolved_link(root / "current")
    previous = _resolved_link(root / "previous")
    if current is not None and target == current:
        raise ValueError(f"refusing to quarantine the active current release: {target}")
    if previous is not None and target == previous:
        raise ValueError(f"refusing to quarantine the previous release: {target}")
    if target.parent != releases:
        raise ValueError(f"refusing to quarantine a path outside releases/: {target}")
    if target.name.startswith("."):
        raise ValueError(f"refusing to quarantine a hidden/staging path: {target}")
    if not target.is_dir():
        raise ValueError(f"new release target is not a directory: {target}")
    dest = releases / f".{target.name}.invalid.{os.getpid()}"
    if dest.exists():
        raise ValueError(f"quarantine destination already exists: {dest}")
    target.rename(dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", type=Path)
    parser.add_argument("--old-prefix", type=Path)
    parser.add_argument("--new-prefix", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--validate-existing", action="store_true")
    parser.add_argument("--quarantine-new", action="store_true")
    args = parser.parse_args(argv)

    if args.quarantine_new:
        if args.release is None or args.root is None:
            parser.error("--quarantine-new requires --release and --root")
        dest = quarantine_new_release(args.release, args.root)
        print(dest)
        return 0
    if args.validate_existing:
        if args.release is None:
            parser.error("--validate-existing requires --release")
        validate_existing_release_launchers(args.release)
        return 0
    if args.smoke:
        if args.release is None:
            parser.error("--smoke requires --release")
        assert_release_python_shebangs(args.release)
        smoke_exec_launchers(args.release)
        return 0
    if args.check_only:
        if args.venv is None or args.old_prefix is None:
            parser.error("--check-only requires --venv and --old-prefix")
        assert_no_stale_prefix(args.venv, args.old_prefix)
        return 0
    if args.venv is None or args.old_prefix is None or args.new_prefix is None:
        parser.error("--venv, --old-prefix and --new-prefix are required")
    rewrite_launchers(args.venv, args.old_prefix, args.new_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
