"""内置价格生成脚本的字段转换回归。"""

import pytest

from scripts.sync_pricing import extract_prices, to_usd_per_m


def test_explicit_free_cache_prices_are_not_replaced_by_input_price():
    prices = extract_prices(
        {"prompt": "0.000002", "input_cache_read": "0", "input_cache_write": "0"}
    )
    assert prices["input"] == 2.0
    assert prices["input_cached"] == 0.0
    assert prices["cache_creation"] == 0.0


@pytest.mark.parametrize("invalid", ["nan", "inf", "-inf", -1, 1e308])
def test_generated_prices_are_finite_and_nonnegative(invalid):
    assert to_usd_per_m(invalid) is None
