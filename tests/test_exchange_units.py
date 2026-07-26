from __future__ import annotations

from decimal import Decimal

import pytest

from btcquant.execution.units import decimal_notional, decimal_value, exchange_float


def test_decimal_notional_does_not_use_binary_float_arithmetic():
    assert decimal_notional("0.1", "0.2") == Decimal("0.02")


def test_exchange_number_rejects_non_finite_and_non_positive_values():
    for value in ("NaN", "Infinity", "-1", "0"):
        with pytest.raises(ValueError):
            exchange_float(value, name="qty", positive=True)


def test_decimal_value_preserves_exchange_precision_string():
    assert decimal_value("0.00001000", name="tick") == Decimal("0.00001000")
