"""Explicit offline fixtures for testing the UI and orchestration wiring."""
from .domain import Policy


def quote_fixture(items,catalog,policy: Policy):
    prices={p["sku"]:p["price_cents"] for p in catalog}
    subtotal=sum(prices[item["sku"]]*item["quantity"] for item in items)
    discount=0
    delivery=0
    if subtotal:
        if subtotal>=policy.discount_threshold_cents:
            discount=int(subtotal*policy.discount_percent//100)
        delivery=policy.base_shipping_cents
        if subtotal>=policy.free_shipping_threshold_cents:
            delivery=0
    return {"subtotal_cents":subtotal,"discount_cents":discount,"shipping_cents":delivery,"total_cents":subtotal-discount+delivery,"discount_label":f"{policy.discount_percent}% promotion","shipping_label":"Free shipping" if delivery==0 else "Standard delivery"}


def fixture_files(policy: Policy, original: dict):
    code='''def quote(items, catalog):
    prices = {p["sku"]: p["price_cents"] for p in catalog}
    subtotal = sum(prices[item["sku"]] * item["quantity"] for item in items)
    discount = subtotal * DISCOUNT_PERCENT // 100 if subtotal >= DISCOUNT_THRESHOLD and subtotal else 0
    shipping = 0 if not subtotal or subtotal >= FREE_SHIPPING_THRESHOLD else BASE_SHIPPING
    return {"subtotal_cents": subtotal, "discount_cents": discount, "shipping_cents": shipping, "total_cents": subtotal-discount+shipping, "discount_label": "Promotion", "shipping_label": "Free shipping" if shipping == 0 else "Standard delivery"}
'''
    prefix=f"# Offline rehearsal fixture, not AI-generated\nDISCOUNT_PERCENT={policy.discount_percent}\nDISCOUNT_THRESHOLD={policy.discount_threshold_cents}\nFREE_SHIPPING_THRESHOLD={policy.free_shipping_threshold_cents}\nBASE_SHIPPING={policy.base_shipping_cents}\n"
    markup='<div class="line" style="color:#087f68"><span>Promotion savings</span><span id="savings">—</span></div><p class="detail" id="promotion-status">Promotion eligibility</p>'
    script='document.getElementById("savings").textContent = "−" + currency(quote.discount_cents); document.getElementById("promotion-status").textContent = quote.discount_cents > 0 ? "Promotion applied to your order" : "Add more to qualify for the promotion";'
    html=original["checkout.html"].replace('<!-- AGENT_PROMOTION_DETAILS -->',markup).replace('// AGENT_PROMOTION_RENDER',script)
    return {"pricing.py":prefix+code,"checkout.html":html,"qa_tests.py":"# Offline rehearsal fixture. Independent protected checks run in the manager.\n"}
