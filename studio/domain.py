"""Stable request contract and independent checkout oracle (integer currency)."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discount_percent: int = Field(ge=0, le=50)
    discount_threshold_cents: int = Field(ge=0, le=100000)
    free_shipping_threshold_cents: int = Field(ge=0, le=100000)
    base_shipping_cents: int = Field(default=799, ge=0, le=5000)


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1, le=100)


class QuoteRequest(BaseModel):
    items: list[Item] = Field(max_length=30)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=10, max_length=2000)
    mode: Literal["live", "rehearsal"] = "live"


EXAMPLES = [
    "Apply a 10% discount on orders of $100 or more and free shipping on orders of $150 or more. Show savings and delivery charges at checkout.",
    "Apply a 15% discount on orders of $80 or more and free shipping on orders of $120 or more. Show savings and delivery charges at checkout.",
    "Apply a 5% discount on orders of $50 or more and free shipping on orders of $100 or more. Show savings and delivery charges at checkout.",
]


def rehearsal_policy(prompt: str) -> Policy:
    """Only explicit known scenarios; no misleading interpretation of free text."""
    try:
        idx = EXAMPLES.index(prompt.strip())
    except ValueError as exc:
        raise ValueError("Offline rehearsal supports the three example requests only. Live mode uses Gemini to interpret supported requests.") from exc
    pct, discount, shipping = [(10,10000,15000),(15,8000,12000),(5,5000,10000)][idx]
    return Policy(discount_percent=pct,discount_threshold_cents=discount,free_shipping_threshold_cents=shipping)


def reference_quote(items: list[dict], catalog: list[dict], policy: Policy) -> dict:
    prices = {p["sku"]: p["price_cents"] for p in catalog}
    subtotal = 0
    for raw in items:
        item = Item.model_validate(raw)
        if item.sku not in prices:
            raise ValueError(f"Unknown product: {item.sku}")
        subtotal += prices[item.sku] * item.quantity
    discount = (subtotal * policy.discount_percent // 100) if subtotal and subtotal >= policy.discount_threshold_cents else 0
    shipping = 0 if subtotal == 0 or subtotal >= policy.free_shipping_threshold_cents else policy.base_shipping_cents
    return dict(subtotal_cents=subtotal,discount_cents=discount,shipping_cents=shipping,total_cents=subtotal-discount+shipping)


def acceptance_cases(policy: Policy) -> list[dict]:
    """Boundary and rounding coverage, independent of the demo's normal catalog."""
    values = {1, 99, 10001, 17501}
    for threshold in [policy.discount_threshold_cents, policy.free_shipping_threshold_cents]:
        values.update(max(1,threshold+delta) for delta in [-1,0,1])
    cases = [{"name":"empty cart", "items":[],"catalog":[]}]
    for value in sorted(values):
        cases.append({"name":f"subtotal {value} cents", "items":[{"sku":"boundary","quantity":1}], "catalog":[{"sku":"boundary","price_cents":value}]})
    cases.append({"name":"multiple items and quantities", "items":[{"sku":"a","quantity":3},{"sku":"b","quantity":2}],"catalog":[{"sku":"a","price_cents":3999},{"sku":"b","price_cents":1551}]})
    return cases


def check_results(cases: list[dict], outputs: list[dict], policy: Policy) -> dict:
    results=[]
    for index,case in enumerate(cases):
        expected=reference_quote(case["items"],case["catalog"],policy)
        actual=outputs[index] if index<len(outputs) else {}
        ok=all(type(actual.get(k)) is int and actual.get(k)==value for k,value in expected.items())
        results.append({"name":case["name"],"passed":ok,"detail":"All monetary values match" if ok else f"Expected {expected}; got {actual}"})
    passed=sum(item["passed"] for item in results)
    return {"passed":passed,"failed":len(results)-passed,"total":len(results),"cases":results}
