#!/usr/bin/env python3
"""Génère le manifeste non secret d'une release déjà construite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from btcquant.deployment import build_release_manifest, validate_full_sha, write_release_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--git-sha", required=True)
    parser.add_argument("--git-tree", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--uv-version", required=True)
    args = parser.parse_args()
    validate_full_sha(args.git_sha)
    manifest = build_release_manifest(
        args.release,
        git_sha=args.git_sha,
        git_tree=args.git_tree,
        origin=args.origin,
        python_version=args.python_version,
        uv_version=args.uv_version,
    )
    path = write_release_manifest(args.release, manifest)
    print(json.dumps({"manifest": str(path), "git_sha": args.git_sha}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
