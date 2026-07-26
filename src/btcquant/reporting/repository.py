"""Accès en lecture aux données de reporting, avec fallback legacy et cache."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from btcquant.execution.state_store import StateStore


class ReportingReadError(RuntimeError):
    """La source de vérité SQLite existe mais ne peut pas être lue."""


class ReportingRepository:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.database = state_dir / "btcquant.db"
        self._cache: dict[str, tuple[object, Any]] = {}

    @staticmethod
    def file_key(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def database_key(self) -> tuple[object, object]:
        return (
            self.file_key(self.database),
            self.file_key(self.database.with_name(f"{self.database.name}-wal")),
        )

    def _parsed(self, path: Path, parser):
        key = self.file_key(path)
        hit = self._cache.get(str(path))
        if hit is not None and hit[0] == key:
            return hit[1]
        value = parser(path)
        self._cache[str(path)] = (key, value)
        return value

    @staticmethod
    def _engine_for(path: Path) -> str | None:
        return {
            "live_state_4x.json": "trend",
            "live_state.json": "trend",
            "carry_state.json": "carry",
        }.get(path.name)

    def read_json(self, path: Path) -> dict | None:
        engine = self._engine_for(path)
        if engine and self.database.exists():
            try:
                state = StateStore(self.database, initialize=False).load_engine_state(engine)
                if state is not None:
                    return state
            except Exception as exc:
                raise ReportingReadError(f"état SQLite illisible pour {engine}") from exc
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def age_seconds(self, path: Path) -> float | None:
        engine = self._engine_for(path)
        if engine and self.database.exists():
            try:
                return StateStore(self.database, initialize=False).engine_age_seconds(engine)
            except Exception as exc:
                raise ReportingReadError(f"horodatage SQLite illisible pour {engine}") from exc
        return time.time() - path.stat().st_mtime if path.exists() else None

    def read_equity(self, name: str) -> pd.Series:
        engine = "trend" if "trend" in name else "carry"
        if self.database.exists():
            try:
                rows = StateStore(self.database, initialize=False).read_equity(engine)
                if rows:
                    frame = pd.DataFrame(rows)
                    index = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
                    return pd.Series(frame["equity"].values, index=index).sort_index()
            except Exception as exc:
                raise ReportingReadError(f"equity SQLite illisible pour {engine}") from exc
        return self._parsed(self.state_dir / name, self._parse_equity)

    @staticmethod
    def _parse_equity(path: Path) -> pd.Series:
        if not path.exists():
            return pd.Series(dtype=float)
        try:
            frame = pd.read_csv(path, on_bad_lines="skip")
            index = pd.to_datetime(frame["ts"], utc=True, format="ISO8601", errors="coerce")
            series = pd.Series(frame["equity"].values, index=index)
            return series[series.index.notna()].sort_index()
        except Exception:
            return pd.Series(dtype=float)

    def read_trades(self) -> pd.DataFrame:
        if self.database.exists():
            try:
                rows = StateStore(self.database, initialize=False).read_trades()
                if rows:
                    return pd.DataFrame(rows).drop(columns=["id"], errors="ignore")
            except Exception as exc:
                raise ReportingReadError("trades SQLite illisibles") from exc
        return self._parsed(self.state_dir / "trades.csv", self._parse_trades)

    @staticmethod
    def _parse_trades(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path, on_bad_lines="skip") if path.exists() else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    def read_flows(self) -> pd.DataFrame:
        if self.database.exists():
            try:
                rows = StateStore(self.database, initialize=False).read_flows()
                if rows:
                    frame = pd.DataFrame(rows).drop(columns=["id"], errors="ignore")
                    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
                    return frame.sort_values("ts")
            except Exception as exc:
                raise ReportingReadError("flux SQLite illisibles") from exc
        return self._parsed(self.state_dir / "flows.csv", self._parse_flows)

    @staticmethod
    def _parse_flows(path: Path) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["ts", "kind", "trend_flow", "carry_flow"])
        if not path.exists():
            return empty
        try:
            frame = pd.read_csv(path, on_bad_lines="skip")
            frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601", errors="coerce")
            valid = frame["ts"].notna() & frame["trend_flow"].notna() & frame["carry_flow"].notna()
            return frame[valid].sort_values("ts")
        except Exception:
            return empty
