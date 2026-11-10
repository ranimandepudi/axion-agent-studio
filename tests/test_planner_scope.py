"""Inspect the planning request with an explicit hosted-model test double."""
import asyncio
import json

from studio.planner import GeminiPlanner


def test_planner_scopes_savings_display_and_compact_qa_without_substituting_plan(monkeypatch):
    captured = []
    generated = {
        "supported": True, "reason": "Explicit test-double plan",
        "policy": {"discount_percent": 7, "discount_threshold_cents": 12345,
                   "free_shipping_threshold_cents": 23456, "base_shipping_cents": 799},
        "tasks": [{"role": role, "title": f"Test-double {role} title",
                   "instructions": f"Model-returned test-double instruction for {role}", "files": [filename]}
                  for role, filename in (("backend", "pricing.py"), ("frontend", "checkout.html"), ("qa", "qa_tests.py"))],
    }

    async def fake_generate(self, prompt, schema=None):
        captured.append((prompt, schema))
        return json.dumps(generated)

    monkeypatch.setattr(GeminiPlanner, "generate", fake_generate)
    plan = asyncio.run(GeminiPlanner().plan("A supported example request", {"pricing.py": "# Test source"}))
    assert plan.model_dump() == generated  # The manager still uses the returned model plan.
    assert len(captured) == 1 and captured[0][1] is not None
    prompt = captured[0][0]
    assert "static visible Savings label and a separate amount span id=savings" in prompt
    assert "neither span has a class" in prompt and "Do not request new CSS classes or styling" in prompt
    assert "one table-driven subTest method" in prompt and "one separate empty-cart method" in prompt
    assert "BOTH thresholds with offsets -1,0,1" in prompt
    assert "price_cents:s" in prompt and "quantity:1" in prompt and "within 1..100" in prompt
    assert "not precomputed expected results" in prompt
    assert "discount=s*discount_percent//100" in prompt and "total=s-discount+shipping" in prompt
    assert "Do not request exact label assertions, extra invalid-input cases or other scenarios" in prompt
    assert "A supported example request" in prompt and "# Test source" in prompt
