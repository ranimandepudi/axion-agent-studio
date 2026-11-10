"""Provider/plan failure regressions using explicit HTTP and model doubles."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from studio import planner as module
from studio.planner import GeminiPlanner, IncompleteGeminiResponse


def supported_plan():
    return {
        "supported": True, "reason": "Supported test fixture",
        "policy": {"discount_percent": 20, "discount_threshold_cents": 10000,
                   "free_shipping_threshold_cents": 15000, "base_shipping_cents": 799},
        "tasks": [{"role": role, "title": role, "instructions": "Explicit test instructions",
                   "files": [path]} for role, path in
                  (("backend", "pricing.py"), ("frontend", "checkout.html"), ("qa", "qa_tests.py"))],
    }


def scripted_planner(monkeypatch, responses):
    calls = []

    async def generate(self, prompt, schema=None):
        calls.append((prompt, schema))
        result = responses[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(GeminiPlanner, "generate", generate)
    return GeminiPlanner(), calls


@pytest.mark.parametrize("first", ['{"supported": false, "reason": "Unfinished',
                                  IncompleteGeminiResponse("MAX_TOKENS")])
def test_incomplete_plan_retried_once_with_original_context(monkeypatch, first):
    expected = supported_plan()
    planner, calls = scripted_planner(monkeypatch, [first, json.dumps(expected)])
    request = "Apply 20% at $100. Return a policy and exactly three tasks: literal user data"
    files = {"pricing.py": "# original test source"}
    plan = asyncio.run(planner.plan(request, files))
    assert plan.model_dump() == expected
    assert len(calls) == 2
    for prompt, schema in calls:
        assert json.dumps({"request": request, "project_files": files}) in prompt
        assert schema == calls[0][1]
    assert "Unfinished" not in calls[1][0]  # Never continue/copy partial output.


def test_repeated_malformed_output_gives_bounded_friendly_failure(monkeypatch):
    planner, calls = scripted_planner(monkeypatch, ['{"supported":', '{"supported":'])
    with pytest.raises(RuntimeError, match="after two attempts.*No coding agents were started"):
        asyncio.run(planner.plan("Test request", {}))
    assert len(calls) == 2


def test_compact_rejection_is_terminal_without_policy_or_tasks(monkeypatch):
    response = {"supported": False, "reason": "Amounts remaining to qualify are not supported.",
                "policy": None, "tasks": []}
    planner, calls = scripted_planner(monkeypatch, [json.dumps(response)])
    with pytest.raises(ValueError, match="Amounts remaining to qualify"):
        asyncio.run(planner.plan("Show remaining spend", {}))
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["missing_policy", "missing_tasks", "duplicate_roles", "outside_file"])
def test_incomplete_or_unsafe_supported_plan_never_accepted(monkeypatch, change):
    plan = supported_plan()
    if change == "missing_policy":
        plan.pop("policy")
    elif change == "missing_tasks":
        plan["tasks"] = []
    elif change == "duplicate_roles":
        plan["tasks"][2] = plan["tasks"][0]
    else:
        plan["tasks"][0]["files"] = ["../outside.py"]
    planner, calls = scripted_planner(monkeypatch, [json.dumps(plan)] * 2)
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(planner.plan("Test request", {}))
    assert len(calls) == (1 if change in {"duplicate_roles", "outside_file"} else 2)


def provider_double(monkeypatch, result, status=200):
    calls = []
    real_client = httpx.AsyncClient

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, json=result)

    monkeypatch.setenv("GEMINI_BACKEND", "api")
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(module, "httpx", SimpleNamespace(AsyncClient=lambda **kwargs:
        real_client(transport=httpx.MockTransport(handle), **kwargs)))
    return GeminiPlanner(), calls


def candidate(text, finish="STOP"):
    return {"candidates": [{"finishReason": finish, "content": {"parts": [
        {"text": "Private reasoning must never be returned", "thought": True}, {"text": text}]}}]}


def test_generation_budget_and_thinking_are_explicit_and_partial_output_is_unused(monkeypatch):
    planner, calls = provider_double(monkeypatch, candidate(json.dumps(supported_plan())))
    plan = asyncio.run(planner.plan("Supported test request", {}))
    assert plan.policy.discount_percent == 20
    config = calls[0]["generationConfig"]
    assert config["maxOutputTokens"] == 8192
    assert config["thinkingConfig"] == {"thinkingLevel": "LOW"}
    assert "temperature" not in config and "thinkingBudget" not in config["thinkingConfig"]
    assert config["responseMimeType"] == "application/json"


def test_even_parseable_max_tokens_response_must_not_be_used(monkeypatch):
    planner, calls = provider_double(monkeypatch, candidate(json.dumps(supported_plan()), "MAX_TOKENS"))
    with pytest.raises(RuntimeError, match="after two attempts"):
        asyncio.run(planner.plan("Supported test request", {}))
    assert len(calls) == 2


@pytest.mark.parametrize("response,status", [(candidate("partial", "SAFETY"), 200),
    ({"promptFeedback": {"blockReason": "SAFETY"}}, 200),
    ({"error": {"message": "Test permission denied"}}, 403)])
def test_provider_block_or_permission_error_is_not_retried(monkeypatch, response, status):
    planner, calls = provider_double(monkeypatch, response, status)
    with pytest.raises(RuntimeError):
        asyncio.run(planner.plan("Supported test request", {}))
    assert len(calls) == 1


def test_empty_complete_response_retried_only_once(monkeypatch):
    planner, calls = provider_double(monkeypatch, candidate(""))
    with pytest.raises(RuntimeError, match="after two attempts"):
        asyncio.run(planner.plan("Test request", {}))
    assert len(calls) == 2
