"""QA prompt/contract tests use explicit fake model replies, never execute generated tests."""
import json

from fastapi.testclient import TestClient
import httpx

from worker.service import ExecuteRequest, Settings, create_app, make_messages


POLICY = {"discount_percent": 7, "discount_threshold_cents": 12345,
          "free_shipping_threshold_cents": 23456, "base_shipping_cents": 499}


def task(role="qa"):
    return {"run_id": "qa-filter-test", "role": role, "instructions": "Verify the supplied policy independently.",
            "policy": POLICY, "files": {
                "pricing.py": "# Relevant pricing source\ndef quote(items, catalog): pass\n",
                "checkout.html": "<style>FRONTEND_CSS_MUST_NOT_REACH_QA</style>",
                "catalog.json": '[{"sku":"CATALOG_MUST_NOT_REACH_QA"}]',
                "qa_tests.py": "# Relevant earlier QA source for repair\n",
            }}


def test_qa_filters_irrelevant_files_but_keeps_repair_source_and_policy():
    body = ExecuteRequest.model_validate(task())
    messages = make_messages(body)
    payload = json.loads(messages[-1]["content"])
    assert payload["project_files"] == {name: body.files[name] for name in ("pricing.py", "qa_tests.py")}
    assert payload["policy"] == POLICY
    assert "FRONTEND_CSS_MUST_NOT_REACH_QA" not in json.dumps(messages)
    assert "CATALOG_MUST_NOT_REACH_QA" not in json.dumps(messages)
    assert "checkout.html" in body.files  # Filtering must not mutate the external request.


def test_backend_input_filtering_remains_unchanged():
    body = ExecuteRequest.model_validate(task("backend"))
    payload = json.loads(make_messages(body)[-1]["content"])
    assert payload["project_files"] == body.files


def test_qa_guidance_requires_exact_boundary_catalogs_and_monetary_oracle():
    guidance = make_messages(ExecuteRequest.model_validate(task()))[0]["content"]
    assert "subTest" in guidance
    assert "'price_cents':s" in guidance and "'quantity':1" in guidance
    assert "1 through 100" in guidance
    assert "s*discount_percent//100" in guidance
    assert "total=s-discount+shipping" in guidance
    assert "ONLY subtotal integers, never precomputed expected-value tuples" in guidance
    assert "sorted({max(0,t+d) for t in (discount_threshold_cents,free_shipping_threshold_cents)" in guidance
    assert "for d in (-1,0,1)})" in guidance
    assert "Inside each subTest loop" in guidance
    assert "shipping=0 if s==0 or s>=free_shipping_threshold_cents else base_shipping_cents" in guidance
    assert "assert all four monetary fields are zero" in guidance
    assert "Do not assert exact display labels" in guidance
    assert "separate empty-cart test" in guidance


def test_actual_qa_request_path_uses_filtered_context_and_smaller_output_budget():
    completions = []

    def fake_model(request):
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "Explicit test template"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * 300})
        assert request.url.path == "/v1/chat/completions"
        completions.append(json.loads(request.content))
        fixture = {"files": {"qa_tests.py": "# Explicit test fixture; never executed\n"},
                   "summary": "Generated a test fixture for contract verification only."}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(fixture)}}]})

    with TestClient(create_app(Settings(worker_role="qa"), transport=httpx.MockTransport(fake_model))) as client:
        response = client.post("/execute", json=task()).json()
    assert response["status"] == "completed" and set(response["files"]) == {"qa_tests.py"}
    assert len(completions) == 1 and completions[0]["max_tokens"] == 1536
    payload = json.loads(completions[0]["messages"][-1]["content"])
    assert set(payload["project_files"]) == {"pricing.py", "qa_tests.py"}
    assert "CATALOG_MUST_NOT_REACH_QA" not in json.dumps(completions)
