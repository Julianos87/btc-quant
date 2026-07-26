"""Retry borné et circuit breaker pour les opérations réseau sans effet."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


class CircuitOpen(RuntimeError):
    """Les appels sont suspendus après une série d'échecs réseau."""


class RetryPolicy:
    def __init__(
        self,
        *,
        attempts: int = 4,
        base_delay: float = 1.0,
        failure_threshold: int = 4,
        reset_after: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if attempts < 1 or failure_threshold < 1:
            raise ValueError("attempts et failure_threshold doivent être positifs")
        self.attempts = attempts
        self.base_delay = base_delay
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._sleep = sleep
        self._monotonic = monotonic
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def call(
        self,
        operation: Callable[..., T],
        *args: Any,
        retry_on: tuple[type[BaseException], ...],
        **kwargs: Any,
    ) -> T:
        with self._lock:
            if self._opened_at is not None:
                if self._monotonic() - self._opened_at < self.reset_after:
                    raise CircuitOpen("Circuit réseau ouvert")
                self._opened_at = None
                self._failures = 0

        for attempt in range(self.attempts):
            try:
                result = operation(*args, **kwargs)
            except retry_on as error:
                with self._lock:
                    self._failures += 1
                    if self._failures >= self.failure_threshold:
                        self._opened_at = self._monotonic()
                if attempt + 1 >= self.attempts:
                    raise
                delay = self.base_delay * (2**attempt)
                log.warning(
                    "Lecture réseau échouée (%s), nouvel essai dans %.1fs",
                    type(error).__name__,
                    delay,
                )
                self._sleep(delay)
            else:
                with self._lock:
                    self._failures = 0
                    self._opened_at = None
                return result
        raise AssertionError("boucle de retry impossible")
