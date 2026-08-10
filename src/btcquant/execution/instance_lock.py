"""Verrou OS secondaire contre deux boucles d'un même moteur."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import TextIO

from .errors import EngineInstanceAlreadyRunning


class EngineInstanceLock:
    """Verrou ``flock`` tenu pendant toute la vie de la boucle moteur.

    Le fichier n'est volontairement jamais supprimé : supprimer un inode
    verrouillé ouvrirait une course où une nouvelle instance verrouille un
    autre inode au même chemin. Le noyau libère le verrou à la fermeture ou au
    crash du processus.
    """

    def __init__(self, database: str | Path, engine: str) -> None:
        database_path = Path(database)
        self.path = database_path.with_name(f".{database_path.name}.{engine}.lock")
        self.engine = engine
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("Verrou d'instance déjà acquis par cet objet")
        handle = self.path.open("a+", encoding="ascii")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.seek(0)
            owner = handle.read().strip() or "pid inconnu"
            handle.close()
            raise EngineInstanceAlreadyRunning(
                f"Moteur {self.engine} déjà actif ({owner}, lock={self.path})"
            ) from error
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> EngineInstanceLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
