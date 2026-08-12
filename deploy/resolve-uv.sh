#!/usr/bin/env bash
# Resolve uv once so every later stage receives an absolute executable path.
set -euo pipefail

UV_COMMAND="${UV_BIN:-uv}"
UV_BIN="$(command -v "${UV_COMMAND}" 2>/dev/null || true)"

case "${UV_BIN}" in
  /*) ;;
  *)
    echo "uv introuvable ou non résolu vers un chemin absolu." >&2
    exit 1
    ;;
esac

[ -x "${UV_BIN}" ] || {
  echo "uv absent ou non exécutable : ${UV_BIN}" >&2
  exit 1
}

printf '%s\n' "${UV_BIN}"
