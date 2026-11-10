"""Starting checkout rules for the prepared Northstar Supply sample shop.

The demo backend specialist modifies this file. Money is represented as integer
cents. The baseline offers no promotions and charges $7.99 for nonempty carts.
"""


def quote(items: list[dict], catalog: list[dict]) -> dict:
    """Calculate a checkout quote for a valid list of SKU/quantity pairs."""
    prices = {product["sku"]: product["price_cents"] for product in catalog}
    subtotal = 0
    for item in items:
        sku = item.get("sku")
        quantity = item.get("quantity")
        if sku not in prices:
            raise ValueError(f"Unknown SKU: {sku}")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ValueError("Quantity must be a nonnegative integer")
        subtotal += prices[sku] * quantity
    shipping = 799 if subtotal else 0
    return {
        "subtotal_cents": subtotal,
        "discount_cents": 0,
        "shipping_cents": shipping,
        "total_cents": subtotal + shipping,
        "discount_label": "No promotion applied",
        "shipping_label": "Standard shipping" if subtotal else "Empty cart",
    }
