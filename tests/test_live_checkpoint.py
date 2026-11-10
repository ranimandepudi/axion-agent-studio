"""Checkpoint-client tests use explicit HTTP doubles, never live/model evidence."""
import json
from pathlib import Path

import httpx
import pytest

from scripts.verify_live_checkpoint import (Configuration, REQUIRED_CASES, loopback_url,
                                            endpoint_url, run_checkpoint, verified_acceptance)


CANDIDATE = 'raise RuntimeError("UNIT TEST: this generated candidate must never run on the host")\n'


class FakeCheckpointServices:
    def __init__(self):
        self.requests = []
        self.bad_health = False
        self.bad_isolation = False
        self.failed_model = False
        self.failed_acceptance = False
        self.incomplete_acceptance = False
        self.bad_quote = False
        self.metrics_missing = False

    def __call__(self, request):
        self.requests.append(request)
        port, path = request.url.port, request.url.path
        if path == "/health" and port == 18081:
            return httpx.Response(200, json={"status": "ready", "role": "backend", "architecture": "aarch64",
                "model": "EXPLICIT UNIT TEST DOUBLE", "machine_family": "n4a" if self.bad_health else "c4a"})
        if path == "/health" and port == 18082:
            return httpx.Response(200, json={"status": "ready", "architecture": "aarch64",
                "isolated_execution": not self.bad_isolation, "runtime": "gvisor"})
        if path == "/metrics":
            if self.metrics_missing:
                return httpx.Response(404)
            return httpx.Response(200, text="# EXPLICIT UNIT TEST DOUBLE\nllamacpp:tokens_predicted_total 123\n")
        if path == "/execute":
            assert json.loads(request.content)["role"] == "backend"
            return httpx.Response(200, json={"status": "failed" if self.failed_model else "completed", "role": "backend",
                "files": {} if self.failed_model else {"pricing.py": CANDIDATE},
                "model": "EXPLICIT UNIT TEST DOUBLE", "elapsed_seconds": 1.25,
                "metrics": {"source": "test_fixture", "cpu_seconds": None, "memory_peak_bytes": None}})
        if path == "/validate":
            body = json.loads(request.content)
            assert body["files"]["pricing.py"] == CANDIDATE
            assert "qa_tests.py" not in body["files"]
            names = {"one insufficient case"} if self.incomplete_acceptance else REQUIRED_CASES
            cases = [{"name": name, "passed": not self.failed_acceptance, "detail": "EXPLICIT TEST FIXTURE"}
                     for name in sorted(names)]
            return httpx.Response(200, json={"status": "failed" if self.failed_acceptance else "passed",
                "tests": {"total": len(cases), "passed": 0 if self.failed_acceptance else len(cases),
                          "failed": len(cases) if self.failed_acceptance else 0, "cases": cases}})
        if path.startswith("/quote/"):
            items = json.loads(request.content)["items"]
            quantity = items[0]["quantity"] if items else 0
            money = {0: [0, 0, 0, 0], 1: [3200, 0, 799, 3999],
                     4: [12800, 1280, 799, 12319], 5: [16000, 1600, 0, 14400]}[quantity]
            result = dict(zip(["subtotal_cents", "discount_cents", "shipping_cents", "total_cents"], money))
            result.update(discount_label="Test discount", shipping_label="Test shipping")
            if self.bad_quote:
                result["total_cents"] += 1
            return httpx.Response(200, json=result)
        raise AssertionError("Unexpected unit-test request")


@pytest.fixture
def configuration(tmp_path):
    shop = tmp_path / "sample_shop"
    shop.mkdir()
    (shop / "pricing.py").write_text("# Prepared baseline, not executed\n")
    (shop / "checkout.html").write_text("<p>Prepared checkout quote receiver</p>")
    (shop / "catalog.json").write_text(json.dumps([{"sku": "TOTE", "price_cents": 3200}]))
    return Configuration("http://127.0.0.1:18081", "http://127.0.0.1:18082",
                         tmp_path / "checkpoint.json", project=tmp_path, llama_url="http://127.0.0.1:18080")


def test_checkpoint_requires_remote_functional_validation_and_saves_real_response_shapes(configuration):
    fake = FakeCheckpointServices()
    evidence = run_checkpoint(configuration, transport=httpx.MockTransport(fake))
    assert evidence["checkpoint_passed"] is True
    assert evidence["full_demo_ready"] is False
    assert evidence["generated_code_executed_on_host"] is False
    assert evidence["infrastructure_verification"]["performed_by_this_script"] is False
    assert evidence["worker_result"]["model"] == "EXPLICIT UNIT TEST DOUBLE"
    assert len(evidence["quote_probes"]) == 4
    assert all(probe["passed"] for probe in evidence["quote_probes"])
    assert Path(evidence["candidate_path"]).read_text() == CANDIDATE
    assert json.loads(configuration.output.read_text())["protected_validation"]["status"] == "passed"
    assert sum(request.url.path == "/execute" for request in fake.requests) == 1
    assert sum(request.url.path == "/validate" for request in fake.requests) == 1
    assert "Python API process only" in evidence["metrics"]["worker_api_process"]["note"]


@pytest.mark.parametrize("failure_flag", ["failed_model", "failed_acceptance", "incomplete_acceptance", "bad_quote"])
def test_syntax_or_claimed_success_alone_does_not_pass(configuration, failure_flag):
    fake = FakeCheckpointServices()
    setattr(fake, failure_flag, True)
    evidence = run_checkpoint(configuration, transport=httpx.MockTransport(fake))
    assert evidence["checkpoint_passed"] is False
    assert evidence["error"]
    assert configuration.output.exists()
    if failure_flag != "bad_quote":
        assert not any(request.url.path.startswith("/quote/") for request in fake.requests)


@pytest.mark.parametrize("failure_flag", ["bad_health", "bad_isolation"])
def test_wrong_deployment_metadata_stops_before_generation(configuration, failure_flag):
    fake = FakeCheckpointServices()
    setattr(fake, failure_flag, True)
    evidence = run_checkpoint(configuration, transport=httpx.MockTransport(fake))
    assert evidence["checkpoint_passed"] is False
    assert not any(request.url.path == "/execute" for request in fake.requests)


def test_optional_metrics_failure_is_explicit_without_fabricating_zeros(configuration):
    fake = FakeCheckpointServices()
    fake.metrics_missing = True
    evidence = run_checkpoint(configuration, transport=httpx.MockTransport(fake))
    assert evidence["checkpoint_passed"] is True
    assert evidence["metrics"]["inference_before"]["available"] is False
    assert evidence["metrics"]["inference_before"]["raw"] is None
    assert "values" not in evidence["metrics"]["inference_after"]


@pytest.mark.parametrize("endpoint", ["https://example.com", "http://169.254.169.254:80",
    "http://user:secret@localhost:18081", "http://localhost:18081/v1", "http://localhost:18081?key=x", "http://localhost"])
def test_only_explicit_existing_loopback_base_endpoints_are_allowed(endpoint):
    with pytest.raises(ValueError):
        loopback_url(endpoint)


def test_acceptance_result_cannot_hide_a_failing_case():
    cases = [{"name": name, "passed": True} for name in REQUIRED_CASES]
    cases[0]["passed"] = False
    assert verified_acceptance({"status": "passed", "tests": {"total": len(cases), "passed": len(cases),
                                                              "failed": 0, "cases": cases}}) is False


@pytest.mark.parametrize("tests", [None, [], "passed", {"cases": [{"name": {}, "passed": True}]},
    {"total": True, "passed": True, "failed": False, "cases": [{"name": "empty cart", "passed": True}]}])
def test_malformed_validator_evidence_is_rejected_without_crashing(tests):
    assert verified_acceptance({"status": "passed", "tests": tests}) is False


def test_duplicate_acceptance_case_names_do_not_inflate_coverage():
    cases = [{"name": name, "passed": True} for name in REQUIRED_CASES]
    cases.append(dict(cases[0]))
    assert verified_acceptance({"status": "passed", "tests": {"total": len(cases), "passed": len(cases),
                                                              "failed": 0, "cases": cases}}) is False


def test_internal_services_require_explicit_cluster_opt_in(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("STUDIO_NAMESPACE", "axion-agent-studio")
    with pytest.raises(ValueError):
        endpoint_url("http://worker-backend:8081", "worker", in_cluster=False)
    assert endpoint_url("http://worker-backend:8081", "worker", in_cluster=True) == "http://worker-backend:8081"
    assert endpoint_url("http://validator:8081", "validator", in_cluster=True) == "http://validator:8081"


@pytest.mark.parametrize("missing", ["KUBERNETES_SERVICE_HOST", "STUDIO_NAMESPACE"])
def test_cluster_opt_in_requires_expected_pod_environment(monkeypatch, missing):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("STUDIO_NAMESPACE", "axion-agent-studio")
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError, match="requires Kubernetes"):
        endpoint_url("http://worker-backend:8081", "worker", in_cluster=True)


@pytest.mark.parametrize("url", ["http://validator:8081", "http://worker-backend:8080",
    "http://worker-backend.other-namespace:8081", "http://example.com:8081", "http://127.0.0.1:18081",
    "http://user:secret@worker-backend:8081", "http://worker-backend:8081/execute",
    "http://worker-backend:8081?target=other"])
def test_cluster_worker_allowlist_rejects_other_roles_hosts_ports_paths_and_credentials(monkeypatch, url):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("STUDIO_NAMESPACE", "axion-agent-studio")
    with pytest.raises(ValueError, match="permits only"):
        endpoint_url(url, "worker", in_cluster=True)


def test_actual_client_path_supports_exact_cluster_service_pair(configuration, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("STUDIO_NAMESPACE", "axion-agent-studio")
    fake = FakeCheckpointServices()
    hosts = set()
    def route(request):
        hosts.add(request.url.host)
        port = {"worker-backend": 18081, "validator": 18082}[request.url.host]
        forwarded = httpx.Request(request.method, f"http://127.0.0.1:{port}{request.url.path}", content=request.content)
        return fake(forwarded)
    config = Configuration("http://worker-backend:8081", "http://validator:8081", configuration.output,
                           project=configuration.project, in_cluster=True)
    result = run_checkpoint(config, transport=httpx.MockTransport(route))
    assert result["checkpoint_passed"] is True
    assert result["access_mode"] == "in_cluster_services"
    assert hosts == {"worker-backend", "validator"}
