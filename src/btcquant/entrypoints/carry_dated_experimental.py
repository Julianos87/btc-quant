"""Run the isolated BTC dated-carry PAPER experiment using public data only."""

from __future__ import annotations

import argparse
import logging
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from btcquant.domain.carry_dated_policy import FeeSchedule, MarginPolicy
from btcquant.execution.carry_dated_market import DeribitPublicClient
from btcquant.execution.carry_dated_runner import CarryDatedRunner
from btcquant.execution.state_store import StateStore

ROOT = Path(os.environ.get("BTCQUANT_ROOT", Path.cwd())).resolve()
CONFIG_KEYS = {
    "strategy",
    "mode",
    "paper_only",
    "capital",
    "database",
    "status_file",
    "poll_seconds",
    "max_book_age_seconds",
    "max_book_skew_seconds",
    "max_metadata_age_seconds",
}


def _decimal(config: dict[str, Any], key: str) -> Decimal:
    try:
        value = Decimal(str(config[key]))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a finite decimal string") from error
    if not value.is_finite():
        raise ValueError(f"{key} must be a finite decimal string")
    return value


def load_experimental_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("carry-dated config must be a YAML mapping")
    unknown = config.keys() - CONFIG_KEYS
    missing = CONFIG_KEYS - config.keys()
    if unknown or missing:
        raise ValueError(
            f"carry-dated config keys mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if (
        config["strategy"] != "carry_btc_dated"
        or config["mode"] != "paper"
        or config["paper_only"] is not True
    ):
        raise ValueError("dated carry runner accepts only strategy=carry_btc_dated, mode=paper")
    database = Path(str(config["database"]))
    if (
        database.is_absolute()
        or ".." in database.parts
        or database.parts[:2] != ("state", "carry-dated-experiment")
    ):
        raise ValueError(
            "dated carry database must be relative and isolated under carry-dated-experiment"
        )
    if database.suffix != ".db":
        raise ValueError("dated carry database must use an explicit .db path")
    status_file = Path(str(config["status_file"]))
    if (
        status_file.is_absolute()
        or ".." in status_file.parts
        or status_file.parts[:2] != ("state", "carry-dated-experiment")
    ):
        raise ValueError("dated carry status file must be isolated under carry-dated-experiment")
    if database == status_file:
        raise ValueError("dated carry database and status snapshot must be different files")
    if not isinstance(config["poll_seconds"], int) or config["poll_seconds"] < 1:
        raise ValueError("poll_seconds must be a positive integer")
    for key in (
        "capital",
        "max_book_age_seconds",
        "max_book_skew_seconds",
        "max_metadata_age_seconds",
    ):
        value = _decimal(config, key)
        if value <= 0:
            raise ValueError(f"{key} must be positive")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=(ROOT / "current" if (ROOT / "current").is_dir() else ROOT)
        / "environments"
        / "paper"
        / "carry_dated_experimental.yaml",
    )
    args = parser.parse_args()
    config = load_experimental_config(args.config)
    state_root = ROOT / "state" / "carry-dated-experiment"
    if state_root.is_symlink():
        raise ValueError("dated carry state directory must not be a symlink")
    state_root.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                state_root / "runner.log",
                encoding="utf-8",
                maxBytes=5_000_000,
                backupCount=3,
            ),
        ],
    )
    database = ROOT / str(config["database"])
    if not database.resolve().is_relative_to(state_root.resolve()):
        raise ValueError("dated carry database resolves outside its isolated state directory")
    store = StateStore(database, initialize=True, allow_migration=False)
    fee_schedule = FeeSchedule.standard_deribit()
    margin_policy = MarginPolicy.deribit_ssm_linear_btc()
    status_override = os.environ.get("BTCQUANT_CARRY_DATED_STATUS_PATH")
    if status_override and not Path(status_override).is_absolute():
        raise ValueError("BTCQUANT_CARRY_DATED_STATUS_PATH must be absolute")
    runner = CarryDatedRunner(
        store=store,
        market=DeribitPublicClient(),
        fee_schedule=fee_schedule,
        margin_policy=margin_policy,
        capital=_decimal(config, "capital"),
        status_path=Path(status_override) if status_override else ROOT / str(config["status_file"]),
        poll_seconds=config["poll_seconds"],
        max_book_age_seconds=_decimal(config, "max_book_age_seconds"),
        max_book_skew_seconds=_decimal(config, "max_book_skew_seconds"),
        max_metadata_age_seconds=_decimal(config, "max_metadata_age_seconds"),
    )
    runner.run_forever()


if __name__ == "__main__":
    main()
