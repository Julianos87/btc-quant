"""Deterministic provenance for quantitative reference artefacts.

The quantitative jobs are Python entry points. Their local import closure is
the smallest maintainable boundary that follows real code dependencies while
remaining independent from the machine's import path and installed packages.
Imported local packages are closed over all Python files in that package. This
also makes a newly added module in an already-used package invalidate the
reference until it is deliberately reviewed.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
_HASH_VERSION = "btcquant-quantitative-import-closure-v1"


def _module_name(path: Path, root: Path) -> str | None:
    """Return the import name for a repository Python file."""

    try:
        relative = path.relative_to(root)
    except ValueError:
        return None

    parts = list(relative.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return None
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(parts) or None


def _module_path(module: str, root: Path) -> Path | None:
    """Resolve a local btcquant or scripts module."""

    parts = module.split(".")
    if not parts or parts[0] not in {"btcquant", "scripts"}:
        return None

    base = root / "src" if parts[0] == "btcquant" else root
    candidate = base.joinpath(*parts)
    source_file = candidate.with_suffix(".py")
    if source_file.is_file():
        return source_file
    package_init = candidate / "__init__.py"
    if package_init.is_file():
        return package_init
    return None


def _relative_import(current: Path, module: str | None, level: int, root: Path) -> str | None:
    current_module = _module_name(current, root)
    if current_module is None:
        return None
    current_parts = current_module.split(".")
    package_parts = current_parts if current.name == "__init__.py" else current_parts[:-1]
    if level > len(package_parts):
        return None
    base = package_parts[: len(package_parts) - level + 1]
    if module:
        base.extend(module.split("."))
    return ".".join(base) or None


def _local_imports(path: Path, root: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            base = _relative_import(path, node.module, node.level, root)
        else:
            base = node.module
        if base:
            modules.add(base)
            # This catches forms such as from btcquant import new_module
            # without treating ordinary imported symbols as modules.
            modules.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return tuple(sorted(modules))


def _package_initializers(path: Path, root: Path) -> tuple[Path, ...]:
    """Return package initializers that execute before a path."""

    try:
        relative = path.relative_to(root / "src")
    except ValueError:
        return ()

    parts = relative.parts
    if not parts or parts[0] != "btcquant":
        return ()

    initializers: list[Path] = []
    current = root / "src"
    for part in parts[:-1]:
        current /= part
        initializer = current / "__init__.py"
        if initializer.is_file():
            initializers.append(initializer)
    root_initializer = root / "src" / "btcquant" / "__init__.py"
    if root_initializer.is_file() and root_initializer not in initializers:
        initializers.insert(0, root_initializer)
    return tuple(initializers)


def quantitative_source_files(
    entrypoint: str | Path,
    *,
    root: str | Path = ROOT,
) -> tuple[Path, ...]:
    """Return the deterministic local import closure for a quant entry point."""

    repository = Path(root).resolve()
    start = Path(entrypoint)
    if not start.is_absolute():
        start = repository / start
    start = start.resolve()
    if not start.is_file():
        raise FileNotFoundError(start)
    try:
        start.relative_to(repository)
    except ValueError as exc:
        raise ValueError(f"entrypoint is outside repository: {start}") from exc

    pending = [start]
    seen: set[Path] = set()
    included: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file() or path.suffix != ".py":
            continue
        included.add(path)

        for initializer in _package_initializers(path, repository):
            if initializer not in seen:
                pending.append(initializer)

        # A package is an execution unit for provenance purposes: registries,
        # plugin modules, and a newly added sibling cannot silently escape the
        # hash merely because the import is dynamic.
        if path.name == "__init__.py" and path.parent.name != "btcquant":
            pending.extend(sorted(path.parent.rglob("*.py")))

        for imported_module in _local_imports(path, repository):
            imported_path = _module_path(imported_module, repository)
            if imported_path is not None and imported_path not in seen:
                pending.append(imported_path)

    return tuple(
        sorted(
            included,
            key=lambda path: path.relative_to(repository).as_posix(),
        )
    )


def quantitative_source_sha256(
    entrypoint: str | Path,
    *,
    root: str | Path = ROOT,
) -> str:
    """Hash an entry point and its deterministic local quantitative closure."""

    repository = Path(root).resolve()
    digest = hashlib.sha256()
    digest.update(_HASH_VERSION.encode("utf-8"))
    digest.update(bytes([0]))
    for path in quantitative_source_files(entrypoint, root=repository):
        relative = path.relative_to(repository).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(bytes([0]))
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(bytes([0]))
    return digest.hexdigest()
