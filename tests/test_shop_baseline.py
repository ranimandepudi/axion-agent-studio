"""Meaningful checks for the prepared starting application's checkout rules."""
import json
from pathlib import Path

import pytest

from sample_shop.pricing import quote


CATALOG = json.loads((Path(__file__).parents[1] / "sample_shop" / "catalog.json").read_text())


def test_empty_cart_has_no_charge():
    result = quote([], CATALOG)
    assert result["subtotal_cents"] == result["shipping_cents"] == result["total_cents"] == 0


def test_starting_shop_has_no_promotion_even_above_future_threshold():
    result = quote([{"sku": "HEADPHONES", "quantity": 2}], CATALOG)
    assert result["subtotal_cents"] == 17800
    assert result["discount_cents"] == 0
    assert result["shipping_cents"] == 799
    assert result["total_cents"] == 18599


def test_default_demo_basket_is_between_proposed_thresholds():
    result = quote([{"sku": "HEADPHONES", "quantity": 1}, {"sku": "TOTE", "quantity": 1}], CATALOG)
    assert result["subtotal_cents"] == 12100
    assert result["total_cents"] == 12899


@pytest.mark.parametrize("quantity", [-1, 1.5, True, "2", None])
def test_invalid_quantities_are_rejected(quantity):
    with pytest.raises(ValueError):
        quote([{"sku": "TOTE", "quantity": quantity}], CATALOG)


def test_unknown_product_is_rejected():
    with pytest.raises(ValueError):
        quote([{"sku": "DOES-NOT-EXIST", "quantity": 1}], CATALOG)
