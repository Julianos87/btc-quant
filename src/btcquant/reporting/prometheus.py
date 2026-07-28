"""Rendu minimal du format texte Prometheus, sans dépendance runtime."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


def render_prometheus(metrics: Mapping[str, int | float | None]) -> str:
    """Rend uniquement les valeurs numériques finies et les noms valides."""

    lines: list[str] = []
    for name, value in sorted(metrics.items()):
        if not _METRIC_NAME.fullmatch(name) or value is None:
            continue
        numeric = float(value)
        if not math.isfinite(numeric):
            continue
        rendered = str(int(numeric)) if numeric.is_integer() else format(numeric, ".15g")
        lines.append(f"{name} {rendered}")
    return "\n".join(lines) + ("\n" if lines else "")
