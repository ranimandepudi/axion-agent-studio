import asyncio
from fastapi.testclient import TestClient
import pytest
from studio import app as module
from studio.domain import EXAMPLES, Policy, acceptance_cases, check_results, reference_quote
from studio.telemetry import quantity


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(module,"DATA",tmp_path)
    module.RUNS.clear()
    module.ACTIVE=asyncio.Lock()
    for key in ("WORKER_BACKEND_URL","WORKER_FRONTEND_URL","WORKER_QA_URL","VALIDATOR_URL"):
        monkeypatch.delenv(key,raising=False)
    monkeypatch.delenv("STUDIO_ENABLE_TEST_FIXTURES",raising=False)
    with TestClient(module.app) as client:yield client


def test_live_refuses_missing_execution_services(client):
    assert client.get("/api/config").json()["live_available"] is False
    assert client.get("/api/config").json()["default_mode"] == "live"
    assert client.post("/api/runs",json={"mode":"live","prompt":EXAMPLES[0]}).status_code==503


def test_requests_default_to_real_execution_without_fixture_fallback(client):
    result=client.post("/api/runs",json={"prompt":EXAMPLES[0]})
    assert result.status_code==503
    assert module.RUNS=={}


def test_fixture_workflows_are_disabled_in_the_visitor_service(client):
    result=client.post("/api/runs",json={"mode":"rehearsal","prompt":EXAMPLES[0]})
    assert result.status_code==403
    assert module.RUNS=={}


def test_booth_capacity_remains_unknown_without_explicit_configuration(client,monkeypatch):
    for name in ("STUDIO_WORKER_NODE_VCPUS","STUDIO_MANAGER_NODE_VCPUS","STUDIO_INFERENCE_THREADS","STUDIO_FEATURED_RUN_ID"):
        monkeypatch.delenv(name,raising=False)
    config=client.get("/api/config").json()["booth"]
    assert config["worker_node_vcpus"] is None
    assert config["manager_node_vcpus"] is None
    assert config["inference_threads_per_worker"] is None
    monkeypatch.setenv("STUDIO_WORKER_NODE_VCPUS","16")
    monkeypatch.setenv("STUDIO_MANAGER_NODE_VCPUS","2")
    monkeypatch.setenv("STUDIO_INFERENCE_THREADS","4")
    config=client.get("/api/config").json()["booth"]
    assert (config["worker_node_vcpus"],config["manager_node_vcpus"],config["inference_threads_per_worker"])==(16,2,4)
    assert "configuration" in config["source"]


@pytest.mark.parametrize("invalid",["0","-1","4.5","unknown","999999","４"])
def test_booth_invalid_configuration_cannot_claim_cpu_capacity(client,monkeypatch,invalid):
    monkeypatch.setenv("STUDIO_WORKER_NODE_VCPUS",invalid)
    assert client.get("/api/config").json()["booth"]["worker_node_vcpus"] is None


def test_current_manager_placement_is_explicit_and_does_not_retrofit_old_metrics(client,monkeypatch):
    recorded={"source":"kubernetes","samples":[{"role":"backend","cpu_cores":2.5}],
              "note":"Historical worker-only measurement fixture"}
    module.RUNS["old-run"]={"id":"old-run","mode":"live","status":"passed","_metrics":recorded}
    placement={"manager":{"machine_family":"c4a","machine_type":"c4a-standard-2",
                          "architecture":"arm64","source":"kubernetes_node","pod_uid":"current-manager"}}
    async def observed_placement():return placement
    monkeypatch.setattr(module,"collect_placement",observed_placement)
    config=client.get("/api/config").json()
    assert config["placement"]==placement
    assert config["live_available"] is False, "Placement alone cannot establish model readiness"
    assert client.get("/api/runs/old-run/metrics").json()==recorded
    assert all(row["role"]!="manager" and "machine_family" not in row for row in recorded["samples"])


def test_manager_placement_is_unknown_without_runtime_node_evidence(client):
    manager=client.get("/api/config").json()["placement"]["manager"]
    assert manager["source"]=="unavailable"
    assert manager["machine_family"] is None and manager["architecture"] is None


def test_rehearsal_rejects_unsupported_prompt(client,monkeypatch):
    monkeypatch.setenv("STUDIO_ENABLE_TEST_FIXTURES","1")
    result=client.post("/api/runs",json={"mode":"rehearsal","prompt":"Deploy arbitrary production credentials"})
    assert result.status_code==422


def test_cross_origin_run_blocked(client):
    assert client.post("/api/runs",headers={"Origin":"https://external.invalid"},json={"prompt":EXAMPLES[0]}).status_code==403


def test_rehearsal_complete_and_honest(client,monkeypatch):
    monkeypatch.setenv("STUDIO_ENABLE_TEST_FIXTURES","1")
    import time
    result=client.post("/api/runs",json={"prompt":EXAMPLES[0],"mode":"rehearsal"})
    assert result.status_code==202
    run_id=result.json()["id"]
    for _ in range(50):
        run=client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("passed","failed"):break
        time.sleep(.05)
    assert run["status"]=="passed",run
    assert run["tests"]["total"]>=10
    assert "not evidence" in run["review"]
    assert all("no inference" in a["model"] for a in run["agents"])
    metrics=client.get(f"/api/runs/{run_id}/metrics").json()
    assert metrics["source"]=="unavailable" and metrics["samples"]==[]
    preview=client.get(run["preview_url"])
    assert preview.status_code==200
    assert "sandbox allow-scripts" in preview.headers["Content-Security-Policy"]
    assert "allow-same-origin" not in preview.headers["Content-Security-Policy"]
    assert client.get(f"/api/runs/{run_id}/artifacts").json()["provenance"].startswith("trusted offline")


def test_quote_requires_passed_run(client):
    module.RUNS["a"*12]={"status":"running"}
    assert client.post("/api/runs/aaaaaaaaaaaa/quote",json={"items":[]}).status_code==409


def test_boundaries_use_original_subtotal_and_floor():
    p=Policy(discount_percent=15,discount_threshold_cents=10000,free_shipping_threshold_cents=15000)
    c=[{"sku":"a","price_cents":15001}]
    q=reference_quote([{"sku":"a","quantity":1}],c,p)
    assert q==dict(subtotal_cents=15001,discount_cents=2250,shipping_cents=0,total_cents=12751)
    c[0]["price_cents"]=14999
    assert reference_quote([{"sku":"a","quantity":1}],c,p)["shipping_cents"]==799


def test_acceptance_detects_wrong_threshold_comparison():
    p=Policy(discount_percent=10,discount_threshold_cents=10000,free_shipping_threshold_cents=15000)
    cases=acceptance_cases(p)
    outputs=[reference_quote(c["items"],c["catalog"],p) for c in cases]
    index=next(i for i,c in enumerate(cases) if c["name"]=="subtotal 15000 cents")
    outputs[index]["shipping_cents"]=799
    tests=check_results(cases,outputs,p)
    assert tests["failed"]==1


def test_quantities():
    assert quantity("250m",True)==.25
    assert quantity("150000000n",True)==pytest.approx(.15)
    assert quantity("1Gi")==1073741824
