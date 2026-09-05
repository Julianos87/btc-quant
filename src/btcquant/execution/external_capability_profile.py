"""Explicit, non-activating external execution capability profiles.

Profiles describe what has been qualified by tests and configuration.  They
do not create a broker, load credentials, or authorize an exchange call.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ExternalAccountingMode(StrEnum):
    TERMINAL_IOC_SETTLEMENT = "TERMINAL_IOC_SETTLEMENT"


@dataclass(frozen=True)
class ExternalCapabilityProfile:
    """Closed capability description for one isolated venue/environment."""

    name: str
    venue: str
    environment: str
    engines: tuple[str, ...]
    order_style: str
    accounting_mode: ExternalAccountingMode | str
    supported_fee_assets: tuple[str, ...]
    automatic_retry_supported: bool
    automatic_retry_enabled: bool
    protective_stop_qualified: bool
    forbidden_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("name", "venue", "environment", "order_style"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be non-empty")
            object.__setattr__(self, field, value.strip())
        object.__setattr__(self, "accounting_mode", ExternalAccountingMode(self.accounting_mode))
        engines = tuple(dict.fromkeys(self.engines))
        fees = tuple(dict.fromkeys(asset.upper() for asset in self.supported_fee_assets))
        forbidden = tuple(dict.fromkeys(self.forbidden_capabilities))
        if not engines or any(not isinstance(item, str) or not item for item in engines):
            raise ValueError("engines must be non-empty strings")
        if not fees or any(not isinstance(item, str) or not item for item in fees):
            raise ValueError("supported_fee_assets must be non-empty strings")
        if not forbidden or any(not isinstance(item, str) or not item for item in forbidden):
            raise ValueError("forbidden_capabilities must be non-empty strings")
        object.__setattr__(self, "engines", engines)
        object.__setattr__(self, "supported_fee_assets", fees)
        object.__setattr__(self, "forbidden_capabilities", forbidden)
        if self.automatic_retry_enabled:
            raise ValueError("the qualified external profile cannot enable automatic retry")

    @property
    def technical_contract_passed(self) -> bool:
        return (
            self.venue == "hyperliquid"
            and self.environment == "testnet"
            and self.engines == ("trend",)
            and self.order_style == "IOC_MARKET"
            and self.accounting_mode == ExternalAccountingMode.TERMINAL_IOC_SETTLEMENT
            and "USDC" in self.supported_fee_assets
            and not self.automatic_retry_enabled
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "venue": self.venue,
            "environment": self.environment,
            "engines": list(self.engines),
            "order_style": self.order_style,
            "accounting_mode": ExternalAccountingMode(self.accounting_mode).value,
            "supported_fee_assets": list(self.supported_fee_assets),
            "automatic_retry_supported": self.automatic_retry_supported,
            "automatic_retry_enabled": self.automatic_retry_enabled,
            "protective_stop_qualified": self.protective_stop_qualified,
            "forbidden_capabilities": list(self.forbidden_capabilities),
        }


def hyperliquid_testnet_trend_ioc_v1() -> ExternalCapabilityProfile:
    """Return the only currently qualified external capability profile."""

    return ExternalCapabilityProfile(
        name="HYPERLIQUID_TESTNET_TREND_IOC_V1",
        venue="hyperliquid",
        environment="testnet",
        engines=("trend",),
        order_style="IOC_MARKET",
        accounting_mode=ExternalAccountingMode.TERMINAL_IOC_SETTLEMENT,
        supported_fee_assets=("USDC",),
        automatic_retry_supported=False,
        automatic_retry_enabled=False,
        protective_stop_qualified=True,
        forbidden_capabilities=(
            "CARRY",
            "GTC_STRATEGY_ORDER",
            "MULTI_LEG",
            "UNSUPPORTED_FEE_ASSET",
            "MAINNET",
            "UNKNOWN_DEX",
            "AUTOMATIC_RETRY",
        ),
    )


__all__ = [
    "ExternalAccountingMode",
    "ExternalCapabilityProfile",
    "hyperliquid_testnet_trend_ioc_v1",
]
