"""Frontend fragment contract checks; all model replies below are explicit test doubles.

No generated HTML or JavaScript is rendered or executed on the test host.
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from worker.service import (ExecuteRequest, FRONTEND_MARKERS, Settings, WorkerFailure,
                            create_app, frontend_regions, make_messages, parse_frontend_output)

BASELINE = (Path(__file__).resolve().parents[1] / "sample_shop/checkout.html").read_text()
POLICY = {"discount_percent": 10, "discount_threshold_cents": 10000,
          "free_shipping_threshold_cents": 15000, "base_shipping_cents": 799}


def fragments():
    return {"savings_markup": '<div class="line"><span>Savings</span><span id="savings">—</span></div>',
            "label_markup": '<p id="discount-label" class="detail">—</p>',
            "savings_script": 'document.getElementById("savings").textContent = currency(quote.discount_cents);',
            "label_script": 'document.getElementById("discount-label").textContent = String(quote.discount_label || "No promotion applied");',
            "summary": "Explicit test fixture fragments."}


def task(source=BASELINE):
    return ExecuteRequest(run_id="test-fragments", role="frontend", instructions="Display savings and promotion status.",
                          policy=POLICY, files={"checkout.html": source})


def outside_regions(source):
    for start, end in FRONTEND_MARKERS.values():
        before, rest = source.split(start, 1)
        _, after = rest.split(end, 1)
        source = before + start + end + after
    return source


def test_assembly_preserves_every_byte_outside_editable_regions():
    files, summary = parse_frontend_output(json.dumps(fragments()), BASELINE)
    candidate = files["checkout.html"]
    assert outside_regions(candidate) == outside_regions(BASELINE)
    assert 'id="subtotal"' in candidate and 'id="shipping"' in candidate and 'id="total"' in candidate
    assert frontend_regions(candidate) == {"markup": fragments()["savings_markup"] + "\n" + fragments()["label_markup"], "script": fragments()["savings_script"] + "\n" + fragments()["label_script"]}
    assert summary == fragments()["summary"]


def test_repair_replaces_regions_instead_of_duplicating_prior_markup_or_updates():
    initial, _ = parse_frontend_output(json.dumps(fragments()), BASELINE)
    replacement = fragments()
    replacement["savings_markup"] = replacement["savings_markup"].replace("Savings</span>", "Your savings</span>")
    replacement["label_script"] = replacement["label_script"].replace("No promotion applied", "No savings yet")
    repaired, _ = parse_frontend_output(json.dumps(replacement), initial["checkout.html"])
    candidate = repaired["checkout.html"]
    assert candidate.count('id="savings"') == 1
    assert candidate.count('getElementById("savings")') == 1
    assert "Your savings" in candidate and "No savings yet" in candidate
    assert outside_regions(candidate) == outside_regions(BASELINE)


def test_prompt_contains_only_editable_context_and_not_immutable_css():
    messages = make_messages(task())
    payload = json.loads(messages[1]["content"])
    assert "project_files" not in payload
    assert payload["existing_editable_regions"] == {"markup": "", "script": ""}
    assert "<style>" not in messages[1]["content"]
    assert "quote" in payload["fixed_context"]
    assert "savings_markup, label_markup, savings_script, label_script and summary" in messages[0]["content"]
    assert payload["interface_example"]["savings_markup"] == "<div class='line'><span>Savings</span><span id='savings'></span></div>"
    assert "Neither inner span may have a class" in messages[0]["content"]


@pytest.mark.parametrize("markup", [
    '<div class="line"><span id="savings"></span></div>',
    '<div class="line"><span></span><span id="savings">Savings</span></div>',
    '<span>Savings</span><div class="line"><span id="savings"></span></div>',
    '<div><span>Savings</span><span id="savings"></span></div>',
    '<div class="line"><span>Savings</span><span id="savings"></span><span>extra column</span></div>',
])
def test_savings_requires_visible_separate_label_in_two_column_row(markup):
    generated = {**fragments(), "savings_markup": markup}
    with pytest.raises(WorkerFailure, match="separate static Savings label"):
        parse_frontend_output(json.dumps(generated), BASELINE)


@pytest.mark.parametrize("source", [
    BASELINE.replace("<!-- END_AGENT_PROMOTION_DETAILS -->", ""),
    BASELINE + "<!-- AGENT_PROMOTION_DETAILS -->",
    BASELINE.replace("<!-- AGENT_PROMOTION_DETAILS -->", "<!-- END_AGENT_PROMOTION_DETAILS -->", 1),
])
def test_missing_or_ambiguous_regions_fail_before_model_request(source):
    def unexpected(request):
        pytest.fail("Model must not be called for an invalid template")
    with TestClient(create_app(Settings(worker_role="frontend"), transport=httpx.MockTransport(unexpected))) as client:
        result = client.post("/execute", json=task(source).model_dump()).json()
    assert result["status"] == "failed" and result["files"] == {}
    assert "marker" in result["error"]


@pytest.mark.parametrize("replacement", [
    {"label_markup": '<p id="wrong-label">status</p>'},
    {"savings_markup": '<p id="savings">0</p><p id="savings">duplicate</p>'},
    {"savings_markup": '<p id="savings" onclick="alert(1)">0</p>'},
    {"savings_markup": '<style>body{display:none}</style><p id="savings">0</p>'},
    {"savings_markup": '<p id="savings">0</div>'},
    {"savings_markup": '<p class="nonexistent-style" id="savings">0</p>'},
    {"savings_markup": '<div id="savings"><span>Amount</span></div>'},
    {"label_script": 'document.querySelector(".missing").textContent = quote.discount_label;'},
    {"savings_script": 'document.getElementById("savings").innerHTML = quote.discount_label;'},
    {"savings_script": 'document.getElementById("savings").textContent = currency(quote.total_cents);'},
    {"label_script": 'fetch("https://example.test");'},
    {"label_script": fragments()["label_script"] + "\n// END_AGENT_PROMOTION_RENDER"},
    {"files": {"checkout.html": "unbounded full-document replacement"}},
])
def test_invalid_or_unbounded_fragments_are_rejected(replacement):
    with pytest.raises(WorkerFailure):
        parse_frontend_output(json.dumps({**fragments(), **replacement}), BASELINE)


def test_missing_label_script_is_rejected_even_if_markup_contains_its_id():
    incomplete = fragments()
    incomplete.pop("label_script")
    with pytest.raises(WorkerFailure, match="label_script"):
        parse_frontend_output(json.dumps(incomplete), BASELINE)


def test_plain_literal_label_fallback_is_supported_without_executing_script():
    generated = fragments()
    generated["label_script"] = "document.getElementById('discount-label').textContent = quote.discount_label || 'Promotion Applied';"
    files, _ = parse_frontend_output(json.dumps(generated), BASELINE)
    assert generated["label_script"] in files["checkout.html"]


def test_label_fallback_cannot_call_another_function():
    generated = fragments()
    generated["label_script"] = "document.getElementById('discount-label').textContent = quote.discount_label || fetch('https://example.test');"
    with pytest.raises(WorkerFailure):
        parse_frontend_output(json.dumps(generated), BASELINE)


@pytest.mark.parametrize("failure", ["missing_status_id", "missing_savings_label", "invented_class"])
def test_actual_request_path_repairs_invalid_fragments_and_returns_full_file(failure):
    completions = []
    bad = fragments()
    if failure == "missing_status_id":
        bad["label_markup"] = bad["label_markup"].replace('id="discount-label"', 'id="missing-label"')
        expected_feedback = "label_markup must define discount-label exactly once"
    elif failure == "missing_savings_label":
        bad["savings_markup"] = '<div class="line"><span id="savings"></span></div>'
        expected_feedback = "separate static Savings label"
    else:
        bad["savings_markup"] = bad["savings_markup"].replace("<span>Savings", '<span class="label">Savings')
        expected_feedback = "Promotion markup may use only existing line and detail classes"

    def fake_model(request):
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "explicit test template"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * 200})
        assert request.url.path == "/v1/chat/completions"
        completions.append(json.loads(request.content))
        content = bad if len(completions) == 1 else fragments()
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}]})

    with TestClient(create_app(Settings(worker_role="frontend"), transport=httpx.MockTransport(fake_model))) as client:
        result = client.post("/execute", json=task().model_dump()).json()
    assert result["status"] == "completed" and set(result["files"]) == {"checkout.html"}
    assert outside_regions(result["files"]["checkout.html"]) == outside_regions(BASELINE)
    assert len(completions) == 2
    assert all(c["max_tokens"] == 1024 for c in completions)
    assert expected_feedback in completions[1]["messages"][-1]["content"]
    assert "No classes on either span" in completions[1]["messages"][-1]["content"]


def test_added_end_markers_preserve_rehearsal_start_marker_replacement():
    from studio.domain import Policy
    from studio.rehearsal import fixture_files
    # Only inspect the existing deterministic rehearsal fixture; never execute its code.
    original = {"pricing.py": "# Test baseline, never executed\n", "checkout.html": BASELINE}
    result = fixture_files(Policy(**POLICY), original)
    assert 'id="savings"' in result["checkout.html"]
    assert "// END_AGENT_PROMOTION_RENDER" in result["checkout.html"]
    assert "<!-- END_AGENT_PROMOTION_DETAILS -->" in result["checkout.html"]
