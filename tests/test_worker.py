"""Contract tests: HTTP model responses below are deliberately fake test fixtures.

These tests validate service behavior, not Gemma quality, Axion performance, or
GKE isolation. Generated test content is never executed here.
"""
import json
import platform

from fastapi.testclient import TestClient
import httpx
import pytest

from worker.service import (ALLOWED_OUTPUT, ExecuteRequest, Settings, WorkerFailure,
                            create_app, parse_output)


POLICY = {"discount_percent": 10, "discount_threshold_cents": 10000,
          "free_shipping_threshold_cents": 15000, "base_shipping_cents": 799}
CHECKOUT_FIXTURE = '''<p>Checkout</p>
<!-- AGENT_PROMOTION_DETAILS -->
<!-- END_AGENT_PROMOTION_DETAILS -->
<script>
// AGENT_PROMOTION_RENDER
// END_AGENT_PROMOTION_RENDER
</script>'''


def task(role="backend"):
    return {"run_id": "test-123", "role": role, "instructions": "Apply the requested policy.",
            "policy": POLICY, "files": {"pricing.py": "def quote(items, catalog): pass\n",
                                        "checkout.html": CHECKOUT_FIXTURE}}


def fixture_output(role="backend"):
    if role == "frontend":
        return json.dumps({"savings_markup": '<div class="line"><span>Savings</span><span id="savings">—</span></div>',
                           "label_markup": '<p id="discount-label" class="detail">—</p>',
                           "savings_script": 'document.getElementById("savings").textContent = currency(quote.discount_cents);',
                           "label_script": 'document.getElementById("discount-label").textContent = String(quote.discount_label || "No promotion applied");',
                           "summary": "Test fixture fragments, never rendered or executed."})
    return json.dumps({"files": {ALLOWED_OUTPUT[role]: "# test fixture output, never executed\n"},
                       "summary": "Generated a candidate for independent validation."})


class FakeLLM:
    """Explicit test double implementing local llama.cpp HTTP endpoints."""
    def __init__(self, role="backend", content=None, token_count=100):
        self.content = content if content is not None else fixture_output(role)
        self.token_count = token_count
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "test formatted prompt"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * self.token_count})
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": self.content}}]})


@pytest.mark.parametrize("role", ["backend", "frontend", "qa"])
def test_role_success_is_file_generation_not_validation(role):
    fake = FakeLLM(role)
    with TestClient(create_app(Settings(worker_role=role), transport=httpx.MockTransport(fake))) as client:
        response = client.post("/execute", json=task(role))
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "completed"
    assert set(result["files"]) == {ALLOWED_OUTPUT[role]}
    assert result["metrics"]["cpu_seconds"] >= 0
    assert result["metrics"]["memory_peak_bytes"] > 0
    assert result["metrics"]["inference_included"] is False
    assert result["metrics"]["scope"] == "worker_api_process"
    payload = json.loads(fake.requests[-1].content)
    assert payload["stream"] is False
    assert payload["max_tokens"] == {"frontend": 1024, "qa": 1536, "backend": 3072}[role]
    assert payload["reasoning_effort"] == "none"
    if role == "frontend":
        assert payload["response_format"]["schema"]["required"] == ["savings_markup", "label_markup", "savings_script", "label_script", "summary"]
    else:
        assert payload["response_format"]["schema"]["properties"]["files"]["required"] == [ALLOWED_OUTPUT[role]]


def test_health_reports_real_architecture_and_distinguishes_declared_machine(monkeypatch):
    monkeypatch.setenv("ARCHITECTURE", "pretend-axion")
    fake = FakeLLM()
    with TestClient(create_app(Settings(), transport=httpx.MockTransport(fake))) as client:
        data = client.get("/health").json()
    assert data["status"] == "ready"
    assert data["architecture"] == platform.machine()
    assert data["machine_family"] == "unknown"
    assert data["machine_family_source"] == "unavailable"
    assert data["model_source"] == "configured_llama_alias"


def test_health_checks_model_readiness_not_only_python_api():
    def loading(request):
        return httpx.Response(503, json={"error": "private upstream details"})
    with TestClient(create_app(Settings(machine_family="c4a"), transport=httpx.MockTransport(loading))) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["machine_family_source"] == "deployment_metadata"
    assert "private upstream" not in response.text


@pytest.mark.parametrize("path", ["../pricing.py", "/pricing.py", "a/pricing.py", "a\\pricing.py", ".env", "pricing.py\x00"])
def test_input_paths_cannot_escape_workspace(path):
    body = task()
    body["files"] = {path: "some code"}
    with TestClient(create_app(Settings(), transport=httpx.MockTransport(FakeLLM()))) as client:
        assert client.post("/execute", json=body).status_code == 422


@pytest.mark.parametrize("content", [
    "```json\n{}\n```", "{} trailing", "[]",
    '{"files":{"../pricing.py":"pass"},"summary":"ok"}',
    '{"files":{"pricing.py":"pass","checkout.html":"hello"},"summary":"ok"}',
    '{"files":{"pricing.py":""},"summary":"ok"}',
    '{"files":{"pricing.py":12},"summary":"ok"}',
    '{"files":{"pricing.py":"pass"},"summary":""}',
    '{"files":{"pricing.py":"pass"},"summary":"ok","tests_passed":true}',
    '{"files":{"pricing.py":"first","pricing.py":"second"},"summary":"ok"}',
    '{"files":{"pricing.py":"pass"},"summary":NaN}',
])
def test_output_parser_rejects_untrusted_shapes(content):
    with pytest.raises(WorkerFailure):
        parse_output(content, "backend")


def test_output_byte_limit():
    content = json.dumps({"files": {"pricing.py": "é" * 24001}, "summary": "ok"})
    with pytest.raises(WorkerFailure):
        parse_output(content, "backend")


@pytest.mark.parametrize("role", ["backend", "qa"])
def test_python_output_with_stray_quote_is_not_marked_complete(role):
    content = json.dumps({"files": {ALLOWED_OUTPUT[role]: 'def example():\n    return 1\n"'},
                          "summary": "Candidate with a syntax error."})
    with pytest.raises(WorkerFailure, match="invalid Python syntax"):
        parse_output(content, role)


def test_python_syntax_check_does_not_execute_candidate():
    code = "raise RuntimeError('This candidate must never execute in the worker')\n"
    content = json.dumps({"files": {"pricing.py": code}, "summary": "Syntax-only candidate."})
    assert parse_output(content, "backend")[0]["pricing.py"] == code


def test_invalid_python_syntax_uses_existing_bounded_generation_retry():
    fake = FakeLLM()
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path == "/v1/chat/completions":
            count += 1
            if count == 1:
                content = json.dumps({"files": {"pricing.py": 'pass\n"'}, "summary": "Invalid fixture."})
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                              "message": {"content": content}}]})
        return fake(request)

    with TestClient(create_app(Settings(), transport=httpx.MockTransport(handler))) as client:
        result = client.post("/execute", json=task()).json()
    assert result["status"] == "completed"
    assert count == 2


def test_bad_generation_fails_after_bounded_retry_without_fake_patch():
    fake = FakeLLM(content="not JSON")
    with TestClient(create_app(Settings(), transport=httpx.MockTransport(fake))) as client:
        result = client.post("/execute", json=task()).json()
    assert result["status"] == "failed"
    assert result["files"] == {}
    assert "output contract" not in result.get("summary", "")
    assert sum(r.url.path == "/v1/chat/completions" for r in fake.requests) == 2


def test_retry_can_recover_format_violation():
    fake = FakeLLM()
    count = 0
    def handler(request):
        nonlocal count
        if request.url.path == "/v1/chat/completions":
            count += 1
            if count == 1:
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "bad json"}}]})
        return fake(request)
    with TestClient(create_app(Settings(), transport=httpx.MockTransport(handler))) as client:
        result = client.post("/execute", json=task()).json()
    assert result["status"] == "completed"
    assert count == 2


def test_large_context_rejected_before_inference():
    fake = FakeLLM(token_count=6000)
    with TestClient(create_app(Settings(), transport=httpx.MockTransport(fake))) as client:
        result = client.post("/execute", json=task()).json()
    assert result["status"] == "failed"
    assert "context budget" in result["error"]
    assert not any(r.url.path == "/v1/chat/completions" for r in fake.requests)


def test_role_pin_and_missing_source_rejected():
    with TestClient(create_app(Settings(worker_role="qa"), transport=httpx.MockTransport(FakeLLM()))) as client:
        assert client.post("/execute", json=task("backend")).status_code == 422
        body = task("qa")
        body["files"].pop("checkout.html")
        assert client.post("/execute", json=body).status_code == 422


@pytest.mark.parametrize("url", ["https://api.example.com/v1", "http://169.254.169.254/v1",
                                 "http://llama.example.com/v1", "http://user:pass@localhost/v1",
                                 "http://localhost/v1?secret=x"])
def test_remote_llm_endpoints_are_not_accepted(url):
    with pytest.raises(ValueError, match="loopback"):
        Settings(llama_base_url=url)


def test_timeout_error_is_clear_and_does_not_contain_upstream_secrets():
    def unavailable(request):
        raise httpx.ReadTimeout("secret credential should never be returned", request=request)
    with TestClient(create_app(Settings(), transport=httpx.MockTransport(unavailable))) as client:
        result = client.post("/execute", json=task()).json()
    assert result["status"] == "failed"
    assert "time budget" in result["error"]
    assert "secret credential" not in json.dumps(result)


def test_invalid_policy_is_not_silently_coerced():
    body = task()
    body["policy"] = {**POLICY, "discount_percent": True}
    with pytest.raises(ValueError):
        ExecuteRequest.model_validate(body)


def test_truncated_generation_is_not_accepted():
    fake = FakeLLM()
    def handler(request):
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": fixture_output()}}]})
        return fake(request)
    with TestClient(create_app(Settings(max_attempts=1), transport=httpx.MockTransport(handler))) as client:
        result = client.post("/execute", json=task()).json()
    assert result["status"] == "failed"
    assert result["files"] == {}
    assert "token budget" in result["error"]
