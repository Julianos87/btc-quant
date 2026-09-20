"""Verrou d’ownership process pour les moteurs d’exécution."""

from __future__ import annotations

import fcntl
from pathlib import Path


class InstanceAlreadyRunning(RuntimeError):
    """Un autre processus possède déjà le moteur demandé."""


class EngineInstanceLock:
    """Verrou flock non bloquant, libéré automatiquement à la fin du process."""

    def __init__(self, state_path: str | Path, engine: str) -> None:
        path = Path(state_path)
        self.path = path.with_name(f".{path.stem}.{engine}.lock")
        self._stream = None

    def acquire(self, *, blocking: bool = False) -> bool:
        if self._stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(stream.fileno(), flags)
        except BlockingIOError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> EngineInstanceLock:
        if not self.acquire():
            raise InstanceAlreadyRunning(f"Instance déjà active : {self.path}")
        return self

