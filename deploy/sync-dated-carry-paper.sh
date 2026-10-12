#!/usr/bin/env bash
# Synchronize only the isolated dated-Carry PAPER services with an exact release.
set -euo pipefail

RELEASE="${1:?usage: sync-dated-carry-paper.sh RELEASE SHOULD_RUN}"
SHOULD_RUN="${2:?usage: sync-dated-carry-paper.sh RELEASE SHOULD_RUN}"
UNIT_DIR="${BTCQUANT_SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
UNITS=(btcquant-carry-dated-collector.service btcquant-carry-dated-experimental.service)

if [[ "${SHOULD_RUN}" != true && "${SHOULD_RUN}" != false ]]; then
  echo "SHOULD_RUN must be true or false" >&2
  exit 2
fi
if [ -L "${RELEASE}" ] || [ ! -d "${RELEASE}" ]; then
  echo "Refusing dated Carry service sync for an invalid release path" >&2
  exit 2
fi

if [ ! -f "${RELEASE}/deploy/${UNITS[0]}" ] ||
  [ ! -f "${RELEASE}/deploy/${UNITS[1]}" ] ||
  [ ! -f "${RELEASE}/environments/paper/carry_dated_experimental.yaml" ]; then
  systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
  rm -f -- "${UNIT_DIR}/${UNITS[0]}" "${UNIT_DIR}/${UNITS[1]}"
  systemctl daemon-reload
  exit 0
fi

"${RELEASE}/venv/bin/python" - \
  "${RELEASE}/environments/paper/carry_dated_experimental.yaml" \
  "${RELEASE}/deploy/${UNITS[0]}" <<'PY'
import re
import sys

config = {}
with open(sys.argv[1], encoding="utf-8") as handle:
    for raw_line in handle:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if not match or match.group(1) in config:
            raise SystemExit("Refusing an unsupported or duplicate PAPER profile field")
        value = match.group(2)
        if value == "true":
            parsed = True
        elif value.startswith('"') and value.endswith('"'):
            parsed = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            parsed = value[1:-1]
        elif value and not any(char.isspace() for char in value):
            parsed = value
        else:
            raise SystemExit("Refusing an unsupported PAPER profile value")
        config[match.group(1)] = parsed
if (
    config.get("strategy") != "carry_btc_dated"
    or config.get("mode") != "paper"
    or config.get("paper_only") is not True
    or config.get("database") != "state/carry-dated-experiment/carry_dated.db"
):
    raise SystemExit("Refusing to start a non-isolated or non-PAPER dated Carry profile")

def configured_bytes(name: str) -> int:
    value = config.get(name)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise SystemExit(f"Refusing an invalid {name} storage limit")
    return int(value)

with open(sys.argv[2], encoding="utf-8") as handle:
    collector_unit = handle.read()

def unit_bytes(name: str) -> int:
    matches = re.findall(
        rf"(?:^|\s)--{re.escape(name)} ([0-9]+)(?=\s|$)",
        collector_unit,
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise SystemExit(f"Refusing a collector unit with missing or duplicate --{name}")
    return int(matches[0])

if unit_bytes("min-free-bytes") != configured_bytes("market_evidence_min_free_bytes"):
    raise SystemExit("Refusing mismatched collector/profile minimum free-space limits")
if unit_bytes("max-total-bytes") != configured_bytes("market_evidence_quota_bytes"):
    raise SystemExit("Refusing mismatched collector/profile evidence quotas")
if configured_bytes("market_evidence_min_free_bytes") < 10737418240:
    raise SystemExit("Refusing a dated Carry collector free-space floor below 10 GiB")
if configured_bytes("market_evidence_quota_bytes") != 5368709120:
    raise SystemExit("Refusing a dated Carry evidence quota other than the v2 5 GiB budget")
PY

for unit in "${UNITS[@]}"; do
  if [ ! -f "${UNIT_DIR}/${unit}" ] || ! cmp -s "${RELEASE}/deploy/${unit}" "${UNIT_DIR}/${unit}"; then
    echo "Refusing dated Carry service start: installed unit differs from release (${unit})" >&2
    exit 2
  fi
done

if [ "${SHOULD_RUN}" = true ]; then
  systemctl enable "${UNITS[@]}"
  systemctl restart "${UNITS[@]}"
  for unit in "${UNITS[@]}"; do
    systemctl is-active --quiet "${unit}"
  done
else
  for unit in "${UNITS[@]}"; do
    if systemctl is-active --quiet "${unit}"; then
      systemctl restart "${unit}"
    fi
  done
fi
