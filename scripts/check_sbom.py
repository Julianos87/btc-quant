"""Vérifie que le SBOM CycloneDX suivi correspond toujours à uv.lock."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SBOM = ROOT / "sbom.cdx.json"


def _normalized(path: Path) -> dict[str, Any]:
    content = json.loads(path.read_text(encoding="utf-8"))
    content.pop("serialNumber", None)
    content.get("metadata", {}).pop("timestamp", None)
    return content


def main() -> None:
    if not SBOM.exists():
        raise SystemExit("SBOM absent : générer sbom.cdx.json avec uv export")
    with tempfile.TemporaryDirectory() as directory:
        generated = Path(directory) / "sbom.cdx.json"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--preview-features",
                "sbom-export",
                "--quiet",
                "--format",
                "cyclonedx1.5",
                "--no-default-groups",
                "--group",
                "exchange",
                "--group",
                "dashboard",
                "--output-file",
                str(generated),
            ],
            cwd=ROOT,
            check=True,
        )
        if _normalized(SBOM) != _normalized(generated):
            raise SystemExit("SBOM périmé : régénérer sbom.cdx.json avec uv export")


if __name__ == "__main__":
    main()
