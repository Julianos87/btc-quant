"""Compaction des historiques d'équity (appelé avant la sauvegarde quotidienne).

Garde la pleine résolution sur les 7 derniers jours, agrège le plus ancien à
l'heure. Écriture atomique (fichier temporaire puis remplacement) : au pire,
un tick en cours d'écriture par le runner est perdu — sans conséquence.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"

import pandas as pd

KEEP_FULL_DAYS = 7


def compact(path: Path) -> None:
    if not path.exists():
        return
    # lecture tolérante : le runner appende en continu — une ligne tronquée
    # ou corrompue ne doit pas bloquer la compaction (sinon elle échouerait
    # silencieusement à chaque sauvegarde, le fichier grossissant sans fin)
    df = pd.read_csv(path, on_bad_lines="skip")
    if len(df) < 5000:  # rien à gagner en dessous
        print(f"{path.name} : {len(df)} lignes, pas de compaction nécessaire")
        return
    ts = pd.to_datetime(df["ts"], utc=True, format="ISO8601", errors="coerce")
    s = pd.Series(df["equity"].values, index=ts)
    s = s[s.index.notna()].sort_index()
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=KEEP_FULL_DAYS)
    old = s[s.index < cutoff].resample("1h").last().dropna()
    recent = s[s.index >= cutoff]
    out = pd.concat([old, recent])
    tmp = path.with_suffix(".tmp")
    out.rename("equity").to_csv(tmp, index_label="ts")
    tmp.replace(path)
    print(f"{path.name} : {len(df)} → {len(out)} lignes")


if __name__ == "__main__":
    compact(STATE / "equity_trend.csv")
    compact(STATE / "equity_carry.csv")
