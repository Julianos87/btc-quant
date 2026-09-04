"""Primitives sûres et testables du protocole de déploiement BTCQuant.

Ce module ne démarre aucun service et ne touche jamais à une base de production
par lui-même. Les opérations qui écrivent sur une base exigent un appel explicite
de l'outil de migration et sont conçues pour être exercées sur une copie.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_MANIFEST_FILES = ("release-manifest.json", "uv.lock", "pyproject.toml")

# Services ayant un chemin d'écriture vers un état BTCQuant. Le gate de
# migration traite aussi les bases secondaires comme des writers afin qu'aucun
# processus ne reste actif pendant la fenêtre de maintenance.
DB_WRITER_UNITS: dict[str, str] = {
    "btcquant-carry.service": "checkpoints Carry et funding_ledger dans btcquant.db",
    "btcquant-trend.service": "checkpoints Trend et ordres dans btcquant.db",
    "btcquant-dashboard.service": "StateStore initialisant /api/readiness",
    "btcquant-watchdog.service": "incidents et santé dans btcquant.db",
    "btcquant-compact.service": "compaction de btcquant.db",
    "btcquant-backup.service": "backup_state appelle la compaction",
    "btcquant-rebalance.service": "flows, dépôts et états dans btcquant.db",
    "btcquant-rebalance-pending.service": "application des dépôts dans btcquant.db",
    "btcquant-shadow.service": "écriture de execution-shadow.db",
    "btcquant-hyperliquid-testnet.service": "écriture de btcquant-testnet.db",
    "btcquant-hyperliquid-watchdog.service": "incidents dans btcquant-testnet.db",
}
DB_WRITER_TIMERS: dict[str, str] = {
    "btcquant-watchdog.timer": "déclenche le watchdog btcquant.db",
    "btcquant-hyperliquid-watchdog.timer": "déclenche le watchdog testnet",
    "btcquant-compact.timer": "déclenche la compaction btcquant.db",
    "btcquant-backup.timer": "déclenche backup_state/compaction",
    "btcquant-rebalance.timer": "déclenche le rééquilibrage",
    "btcquant-rebalance-pending.timer": "déclenche l'application des dépôts",
}


def configured_remote_aliases(raw: str | None = None) -> dict[str, str]:
    """Parse explicit ``alias=host`` mappings used for SSH remote aliases."""

    value = os.environ.get("BTCQUANT_CANONICAL_REMOTE_ALIASES", "") if raw is None else raw
    aliases: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        alias, separator, host = item.partition("=")
        if not separator or not alias or not host:
            raise DeploymentProtocolError(
                "BTCQUANT_CANONICAL_REMOTE_ALIASES doit contenir alias=host."
            )
        aliases[alias.strip()] = host.strip()
    return aliases


class DeploymentProtocolError(RuntimeError):
    """Une précondition de déploiement n'est pas satisfaite."""


class DeploymentAlreadyRunning(DeploymentProtocolError):
    """Un autre déploiement détient déjà le verrou global."""


@dataclass(frozen=True)
class DatabaseInspection:
    path: Path
    metadata_schema_version: int | None
    pragma_schema_version: int
    journal_mode: str
    integrity_check: str
    size_bytes: int
    sha256: str


def systemd_writer_quiescence_failures() -> list[str]:
    """Interroge systemd et renvoie les writers/timers non arrêtés."""

    units = (*DB_WRITER_UNITS, *DB_WRITER_TIMERS)
    active_units: dict[str, str] = {}
    active_timers: dict[str, str] = {}
    for unit in units:
        load = subprocess.run(
            ["systemctl", "show", unit, "--property=LoadState", "--value"],
            check=False,
            capture_output=True,
            text=True,
        )
        state = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveState", "--value"],
            check=False,
            capture_output=True,
            text=True,
        )
        if load.returncode != 0 or load.stdout.strip() != "loaded":
            value = "unknown"
        elif state.returncode != 0:
            value = "unknown"
        else:
            value = state.stdout.strip() or "unknown"
        if unit in DB_WRITER_UNITS:
            active_units[unit] = value
        else:
            active_timers[unit] = value
    return writer_quiescence_failures(active_units, active_timers)


def writer_quiescence_failures(
    active_units: dict[str, str], active_timers: dict[str, str]
) -> list[str]:
    """Retourne les writers/timers dont l'arrêt n'est pas démontré.

    Un état absent ou inconnu est un échec volontairement fail-closed : le
    protocole ne peut pas déduire qu'un writer est arrêté.
    """

    allowed = {"inactive", "dead"}
    failures = [
        f"{unit}: état {active_units.get(unit, 'unknown')}"
        for unit in DB_WRITER_UNITS
        if active_units.get(unit) not in allowed
    ]
    failures.extend(
        f"{timer}: état {active_timers.get(timer, 'unknown')}"
        for timer in DB_WRITER_TIMERS
        if active_timers.get(timer) not in allowed
    )
    return failures


def open_database_handle_failures(
    paths: tuple[str | Path, ...], *, proc_root: str | Path = "/proc"
) -> list[str]:
    """Find open DB/WAL/SHM descriptors through Linux ``/proc``.

    This is defence in depth after the systemd gate, not a replacement for it.
    Permission or inspection errors fail closed because absence of proof must be
    treated as a possible writer.
    """

    targets = {os.path.realpath(Path(path)) for path in paths}
    failures: list[str] = []
    proc = Path(proc_root)
    try:
        processes = list(proc.iterdir())
    except OSError as error:
        return [f"proc inspection unavailable: {error}"]
    for process in processes:
        if not process.name.isdigit():
            continue
        fd_dir = process / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError as error:
            failures.append(f"pid {process.name}: fd inspection unavailable: {error}")
            continue
        for descriptor in descriptors:
            try:
                target = os.path.realpath(descriptor)
            except OSError as error:
                failures.append(f"pid {process.name}: fd inspection unavailable: {error}")
                continue
            if target in targets:
                failures.append(f"pid {process.name} holds {target}")
    return sorted(set(failures))


def migration_auto_rollback_allowed(*, db_migrated: bool, target_writers_started: bool) -> bool:
    """Apply the irreversible writer frontier rule."""

    return not db_migrated or not target_writers_started


def migration_rollback_disposition(*, db_migrated: bool, target_writers_started: bool) -> str:
    """Return the only permitted rollback disposition for a deployment state."""

    if not db_migrated:
        return "AUTO_CODE_ROLLBACK"
    if target_writers_started:
        return "MANUAL_RECOVERY_REQUIRED"
    return "AUTO_DB_RESTORE_AND_CODE_ROLLBACK"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def inspect_sqlite(path: str | Path) -> DatabaseInspection:
    """Inspecte SQLite en lecture seule, y compris une base en mode WAL."""

    database = Path(path)
    if not database.is_file():
        raise DeploymentProtocolError(f"Base SQLite absente: {database}")
    try:
        with sqlite3.connect(_readonly_uri(database), uri=True) as connection:
            connection.row_factory = sqlite3.Row
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            pragma_schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
            integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            metadata_schema_version: int | None = None
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
            except sqlite3.OperationalError as error:
                if "no such table" not in str(error).lower():
                    raise
                row = None
            if row is not None:
                metadata_schema_version = int(row[0])
    except sqlite3.DatabaseError as error:
        raise DeploymentProtocolError(f"Lecture SQLite impossible: {database}: {error}") from error
    return DatabaseInspection(
        path=database,
        metadata_schema_version=metadata_schema_version,
        pragma_schema_version=pragma_schema_version,
        journal_mode=journal_mode,
        integrity_check=integrity_check,
        size_bytes=database.stat().st_size,
        sha256=sha256_file(database),
    )


def checkpoint_sqlite_wal(path: str | Path) -> tuple[int, int, int]:
    """Consolide le WAL via SQLite après preuve de quiescence des writers."""

    database = Path(path)
    if not database.is_file():
        raise DeploymentProtocolError(f"Base SQLite absente: {database}")
    try:
        with sqlite3.connect(database, timeout=10.0) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.DatabaseError as error:
        raise DeploymentProtocolError(f"Checkpoint WAL impossible: {database}: {error}") from error
    if result is None or len(result) != 3 or int(result[0]) != 0 or integrity != "ok":
        raise DeploymentProtocolError(
            f"Checkpoint WAL non confirmé: result={result!r}, integrity={integrity}"
        )
    return (int(result[0]), int(result[1]), int(result[2]))


def restore_sqlite_database(backup: str | Path, destination: str | Path) -> dict[str, object]:
    """Restaure une copie SQLite vérifiée sans supprimer manuellement WAL/SHM.

    La destination est ouverte par SQLite en journal DELETE avant la restauration;
    les éventuels fichiers auxiliaires sont donc gérés par SQLite lui-même.
    Cette opération est réservée au rollback explicite, writers arrêtés.
    """

    backup_path = Path(backup)
    destination_path = Path(destination)
    backup_info = inspect_sqlite(backup_path)
    if backup_info.integrity_check != "ok":
        raise DeploymentProtocolError("Restauration refusée: backup non intègre")
    if not destination_path.is_file():
        raise DeploymentProtocolError(f"Destination SQLite absente: {destination_path}")
    try:
        with sqlite3.connect(destination_path, timeout=10.0) as destination_connection:
            destination_connection.execute("PRAGMA busy_timeout = 10000")
            # A migrated database may still have a WAL generated by the
            # migration transaction. Let SQLite consolidate it; never remove
            # -wal/-shm by hand. Keeping the destination journal mode intact is
            # safer than forcing DELETE on a live-format database.
            checkpoint = destination_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise DeploymentProtocolError(
                    f"Restauration refusée: checkpoint WAL={checkpoint!r}"
                )
            destination_connection.commit()
            with sqlite3.connect(_readonly_uri(backup_path), uri=True) as source_connection:
                source_connection.backup(destination_connection, pages=256, sleep=0.1)
            integrity = str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0])
            destination_connection.commit()
    except sqlite3.DatabaseError as error:
        raise DeploymentProtocolError(f"Restauration SQLite impossible: {error}") from error
    if integrity != "ok":
        raise DeploymentProtocolError(f"Restauration invalide: integrity_check={integrity}")
    restored = inspect_sqlite(destination_path)
    return {
        "backup_path": str(backup_path),
        "restored_path": str(destination_path),
        "schema_version": restored.metadata_schema_version,
        "sha256": restored.sha256,
        "integrity_check": restored.integrity_check,
    }


def backup_sqlite_database(
    source: str | Path,
    destination: str | Path,
    *,
    target_git_sha: str,
) -> dict[str, object]:
    """Crée une copie cohérente avec l'API SQLite ``backup`` puis la vérifie."""

    source_path = Path(source)
    destination_path = Path(destination)
    if not FULL_SHA_RE.fullmatch(target_git_sha):
        raise DeploymentProtocolError(
            "Le SHA cible doit être complet (40 caractères hexadécimaux)."
        )
    if source_path.resolve() == destination_path.resolve():
        raise DeploymentProtocolError("La destination du backup ne peut pas être la source.")
    source_info = inspect_sqlite(source_path)
    if source_info.integrity_check != "ok":
        raise DeploymentProtocolError(
            f"Backup refusé: integrity_check source={source_info.integrity_check}"
        )
    if destination_path.exists():
        raise DeploymentProtocolError(f"Backup déjà présent, refus d'écraser: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(f".{destination_path.name}.{os.getpid()}.tmp")
    try:
        with sqlite3.connect(_readonly_uri(source_path), uri=True) as source_connection:
            with sqlite3.connect(temporary) as destination_connection:
                source_connection.backup(destination_connection, pages=256, sleep=0.1)
                mode = str(
                    destination_connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                )
                if mode.lower() != "delete":
                    raise DeploymentProtocolError(
                        f"Backup refusé: journal SQLite non basculable en DELETE ({mode})"
                    )
                result = str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0])
                if result != "ok":
                    raise DeploymentProtocolError(
                        f"Backup invalide après copie: integrity_check={result}"
                    )
        os.replace(temporary, destination_path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    backup_info = inspect_sqlite(destination_path)
    manifest: dict[str, object] = {
        "backup_created_at": utc_now(),
        "source_path": str(source_path),
        "source_schema_version": source_info.metadata_schema_version,
        "source_size_bytes": source_info.size_bytes,
        "source_sha256": source_info.sha256,
        "backup_path": str(destination_path),
        "backup_size_bytes": backup_info.size_bytes,
        "backup_sha256": backup_info.sha256,
        "target_git_sha": target_git_sha,
        "integrity_check": backup_info.integrity_check,
    }
    manifest_path = Path(f"{destination_path}.manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def canonical_repository_identity(
    value: str, *, allowed_aliases: dict[str, str] | None = None
) -> str:
    """Normalise GitHub après validation d'un alias explicitement configuré."""

    raw = value.strip()
    if raw.startswith("git@github.com:"):
        raw = "https://github.com/" + raw.split(":", 1)[1].lstrip("/")
    elif raw.startswith("ssh://git@github.com/"):
        raw = "https://github.com/" + raw.split("github.com/", 1)[1]
    elif ":" in raw and not raw.startswith(("http://", "https://")):
        alias, path = raw.split(":", 1)
        aliases = configured_remote_aliases() if allowed_aliases is None else allowed_aliases
        host = aliases.get(alias)
        if host is None:
            raise DeploymentProtocolError(
                f"Alias remote non configuré: {alias}; identité canonique non prouvée"
            )
        raw = f"https://{host.rstrip('/')}/{path.lstrip('/')}"
    if raw.startswith("github.com/"):
        raw = "https://" + raw
    if raw.startswith("https://"):
        raw = raw.removeprefix("https://")
    elif raw.startswith("http://"):
        raw = raw.removeprefix("http://")
    host, separator, path = raw.partition("/")
    if not separator or host.lower() != "github.com":
        raise DeploymentProtocolError(f"Repository canonique invalide: {value}")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise DeploymentProtocolError(f"Repository canonique invalide: {value}")
    return f"github.com/{parts[0]}/{parts[1]}".lower()


def validate_canonical_repository(
    remote_url: str,
    expected_repository: str,
    *,
    allowed_aliases: dict[str, str] | None = None,
) -> str:
    actual = canonical_repository_identity(remote_url, allowed_aliases=allowed_aliases)
    expected = canonical_repository_identity(expected_repository, allowed_aliases=allowed_aliases)
    if actual != expected:
        raise DeploymentProtocolError(
            f"Repository canonique inattendu: {actual}; attendu={expected}"
        )
    return actual


def validate_full_sha(value: str) -> str:
    if not FULL_SHA_RE.fullmatch(value):
        raise DeploymentProtocolError("Une release exige un SHA Git complet de 40 caractères.")
    return value


def _hash_if_file(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def _current_application_schema_version() -> int:
    """Return the schema contract implemented by this release."""

    # Keep this import lazy: the migration entrypoint imports this module while
    # the execution package has not necessarily finished loading yet.
    from .execution.state_store import SCHEMA_VERSION

    return SCHEMA_VERSION


def build_release_manifest(
    release: str | Path,
    *,
    git_sha: str,
    git_tree: str,
    origin: str,
    python_version: str,
    uv_version: str,
    release_created_at: str | None = None,
    schema_version_required: int | None = None,
) -> dict[str, object]:
    """Construit un manifeste sans lire de secret ni inclure de fichier secret."""

    release_path = Path(release)
    validate_full_sha(git_sha)
    expected_schema_version = _current_application_schema_version()
    if schema_version_required is None:
        schema_version_required = expected_schema_version
    elif schema_version_required != expected_schema_version:
        raise DeploymentProtocolError(
            "Le manifeste doit cibler le schéma implémenté par la release "
            f"(v{expected_schema_version})."
        )
    if not release_path.is_dir():
        raise DeploymentProtocolError(f"Release absente: {release_path}")
    forbidden = [
        path
        for path in release_path.rglob("*")
        if path.is_file() and (path.name == ".env" or path.name.endswith((".db", ".sqlite")))
    ]
    if forbidden:
        raise DeploymentProtocolError(
            "Fichiers sensibles présents dans la release: "
            + ", ".join(str(path.relative_to(release_path)) for path in forbidden)
        )
    required = {
        name: _hash_if_file(release_path / name)
        for name in ("uv.lock", "pyproject.toml", "sbom.cdx.json", "requirements.txt")
    }
    if required["uv.lock"] is None or required["pyproject.toml"] is None:
        raise DeploymentProtocolError("uv.lock et pyproject.toml sont requis dans la release.")
    config = release_path / "environments" / "paper" / "config.yaml"
    try:
        project_metadata = tomllib.loads(
            (release_path / "pyproject.toml").read_text(encoding="utf-8")
        )
        application_version = str(project_metadata["project"]["version"])
    except (KeyError, OSError, tomllib.TOMLDecodeError) as error:
        raise DeploymentProtocolError("Version applicative absente du pyproject.toml") from error
    return {
        "git_sha": git_sha,
        "git_tree": git_tree,
        "origin": origin,
        "python_version": python_version,
        "uv_version": uv_version,
        "uv_lock_sha256": required["uv.lock"],
        "pyproject_sha256": required["pyproject.toml"],
        "release_created_at": release_created_at or utc_now(),
        "application_version": application_version,
        "sbom_sha256": required["sbom.cdx.json"],
        "dependency_export_sha256": required["requirements.txt"],
        "schema_version_required": schema_version_required,
        "config_file_sha256": _hash_if_file(config),
    }


def write_release_manifest(release: str | Path, manifest: dict[str, object]) -> Path:
    path = Path(release) / "release-manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def validate_release_manifest(release: str | Path, expected_sha: str) -> dict[str, object]:
    release_path = Path(release)
    validate_full_sha(expected_sha)
    manifest_path = release_path / "release-manifest.json"
    if not manifest_path.is_file():
        raise DeploymentProtocolError(f"Manifeste absent: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentProtocolError(f"Manifeste illisible: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise DeploymentProtocolError("Le manifeste doit être un objet JSON.")
    if manifest.get("git_sha") != expected_sha:
        raise DeploymentProtocolError("Le SHA du manifeste ne correspond pas à la release.")
    expected_schema_version = _current_application_schema_version()
    if manifest.get("schema_version_required") != expected_schema_version:
        raise DeploymentProtocolError(
            "Le manifeste ne requiert pas le schéma implémenté par la release "
            f"(v{expected_schema_version})."
        )
    for path in release_path.rglob("*"):
        if path.is_file() and (path.name == ".env" or path.suffix in {".db", ".sqlite", ".key"}):
            raise DeploymentProtocolError(
                f"Fichier sensible dans la release: {path.relative_to(release_path)}"
            )
    required_hashes = {
        "uv.lock": "uv_lock_sha256",
        "pyproject.toml": "pyproject_sha256",
        "sbom.cdx.json": "sbom_sha256",
        "requirements.txt": "dependency_export_sha256",
    }
    for filename, field in required_hashes.items():
        path = release_path / filename
        expected_hash = manifest.get(field)
        if expected_hash is not None and (not path.is_file() or sha256_file(path) != expected_hash):
            raise DeploymentProtocolError(f"Hash de provenance invalide pour {filename}: {field}")
    return manifest


def _link_target(path: Path) -> Path | None:
    if not path.is_symlink():
        return None
    target = Path(os.readlink(path))
    return (path.parent / target).resolve() if not target.is_absolute() else target.resolve()


def _replace_link(path: Path, target: Path) -> None:
    temporary = path.parent / f".{path.name}.new.{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(str(target), temporary)
    os.replace(temporary, path)


def atomic_switch_release(root: str | Path, new_release: str | Path) -> tuple[Path | None, Path]:
    """Bascule ``current`` puis ``previous`` avec restauration si la seconde échoue."""

    root_path = Path(root)
    current = root_path / "current"
    previous = root_path / "previous"
    target = Path(new_release).resolve()
    releases = (root_path / "releases").resolve()
    if not target.is_dir() or releases not in target.parents:
        raise DeploymentProtocolError(
            "La release cible doit être un répertoire immutable sous releases."
        )
    old_current = _link_target(current)
    old_previous = _link_target(previous)
    if old_current == target:
        return old_current, target
    _replace_link(current, target)
    try:
        if old_current is not None:
            _replace_link(previous, old_current)
    except Exception as error:
        try:
            if old_current is None:
                if current.is_symlink():
                    current.unlink()
            else:
                _replace_link(current, old_current)
            if old_previous is None:
                if previous.is_symlink():
                    previous.unlink()
            else:
                _replace_link(previous, old_previous)
        except Exception as rollback_error:
            raise DeploymentProtocolError(
                "Switch partiel et restauration impossible: intervention manuelle requise"
            ) from rollback_error
        raise DeploymentProtocolError("Mise à jour de previous échouée; switch annulé") from error
    return old_current, target


@contextmanager
def deployment_lock(path: str | Path) -> Iterator[None]:
    """Verrou non bloquant: un second déploiement échoue immédiatement."""

    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentAlreadyRunning(str(lock_path)) from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
