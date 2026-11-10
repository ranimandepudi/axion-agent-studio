"""Manager live-path integration with explicitly mocked external services.

No real Gemini, Gemma, GKE, browser execution, or cloud telemetry is used here.
Fake responses prove orchestration contracts only, never demo readiness.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from studio import app as module
from studio.domain import EXAMPLES, Policy, reference_quote
from studio.planner import GeminiPlanner
from worker.service import ExecuteRequest


REAL_ASYNC_CLIENT = httpx.AsyncClient
REAL_SLEEP = asyncio.sleep
POLICY = Policy(discount_percent=10, discount_threshold_cents=10000,
                free_shipping_threshold_cents=15000)
ROLE_FILES = {"backend": "pricing.py", "frontend": "checkout.html", "qa": "qa_tests.py"}


class FakeServices:
    """Explicit, in-memory HTTP doubles; never serve or run a generated program."""

    def __init__(self):
        self.worker_calls = []
        self.validation_calls = []
        self.quote_calls = []
        self.failures = []
        self.worker_failure = None
        self.unsupported = False
        self.invalid_plan_files = False
        self.hold_backend = False
        self.backend_started = threading.Event()
        self.release_backend = threading.Event()
        self.metrics_calls = 0
        self.metrics_unavailable = False
        self.review_calls = 0
        self.task_instructions = None
        self.failure_detail = "Explicit validation test fixture"

    async def generate(self, prompt, schema=None):
        await REAL_SLEEP(0)
        if schema is None:
            self.review_calls += 1
            return "Explicit test fixture review. No real model was called."
        body = {"supported": not self.unsupported, "reason": "Unsupported external integration" if self.unsupported else "Supported test fixture",
                "policy": POLICY.model_dump(),
                "tasks": [{"role": role, "title": f"Test {role} task",
                           "instructions": self.task_instructions or f"EXPLICIT TEST FIXTURE: implement {role} changes.",
                           "files": [ROLE_FILES[role]]} for role in ROLE_FILES]}
        if self.invalid_plan_files:
            body["tasks"][0]["files"] = ["../outside.py"]
        return json.dumps(body)

    async def transport(self, request):
        host = request.url.host
        role = host.split(".")[0]
        if request.url.path == "/health":
            if role == "validator":
                return httpx.Response(200, json={"status": "ready", "isolated_execution": True,
                                                "test_fixture": True})
            return httpx.Response(200, json={"status": "ready", "model": "EXPLICIT TEST DOUBLE",
                                            "machine_family": "test-fixture-family", "sandbox": "test-fixture"})
        if request.url.path == "/execute":
            body = json.loads(request.content)
            ExecuteRequest.model_validate(body)
            self.worker_calls.append(deepcopy(body))
            assert body["role"] == role
            if role == "backend" and self.hold_backend:
                self.backend_started.set()
                while not self.release_backend.is_set():
                    await REAL_SLEEP(.005)
            if self.worker_failure == role:
                return httpx.Response(200, json={"status": "failed", "error": "Explicit simulated model failure",
                                                "role": role, "files": {}})
            count = sum(call["role"] == role for call in self.worker_calls)
            content = f"# EXPLICIT TEST FIXTURE {role} generation {count}; never executed\n"
            if role == "frontend":
                content = f"<!doctype html><p>EXPLICIT TEST FIXTURE checkout version {count}</p>"
            return httpx.Response(200, json={"status": "completed", "role": role,
                "files": {ROLE_FILES[role]: content}, "summary": f"EXPLICIT TEST FIXTURE {role}",
                "model": "EXPLICIT TEST DOUBLE", "metrics": {"source": "test_fixture", "cpu_seconds": None,
                                                            "memory_peak_bytes": None}})
        if request.url.path == "/validate":
            self.validation_calls.append(json.loads(request.content))
            index = len(self.validation_calls) - 1
            failed_names = self.failures[min(index, len(self.failures) - 1)] if self.failures else set()
            names = {"subtotal 10000 cents", "checkout preview contract", "supplemental QA tests"} | failed_names
            cases = [{"name": name, "passed": name not in failed_names, "detail": self.failure_detail}
                     for name in sorted(names)]
            return httpx.Response(200, json={"status": "failed" if failed_names else "passed",
                "tests": {"total": len(cases), "passed": len(cases) - len(failed_names),
                          "failed": len(failed_names), "cases": cases}})
        if request.url.path.startswith("/quote/"):
            body = json.loads(request.content)
            self.quote_calls.append(body)
            return httpx.Response(200, json=reference_quote(body["items"], module.catalog(), POLICY))
        raise AssertionError(f"Unexpected test-only external request: {request.url}")

    async def metrics(self):
        self.metrics_calls += 1
        if self.metrics_unavailable:
            return {"source": "unavailable", "samples": [], "note": "Explicit unavailable metrics test fixture"}
        return {"source": "kubernetes", "samples": [{"timestamp": f"fixture-tick-{self.metrics_calls}",
                "role": "backend", "cpu_cores": 0.125, "memory_bytes": 1024}],
                "note": "EXPLICIT TEST FIXTURE: these are not real cloud measurements."}


@pytest.fixture
def live_harness(tmp_path, monkeypatch):
    services = FakeServices()
    monkeypatch.setattr(module, "DATA", tmp_path)
    monkeypatch.setattr(module, "RUNS", {})
    monkeypatch.setattr(module, "BACKGROUND", set())
    monkeypatch.setattr(module, "ACTIVE", asyncio.Lock())
    for role in ROLE_FILES:
        monkeypatch.setenv(f"WORKER_{role.upper()}_URL", f"http://{role}.test.invalid")
    monkeypatch.setenv("VALIDATOR_URL", "http://validator.test.invalid")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "explicit-test-fixture-not-a-real-project")
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.delenv("STUDIO_ALLOW_GCLOUD_AUTH", raising=False)

    async def fake_generate(self, prompt, schema=None):
        return await services.generate(prompt, schema)

    monkeypatch.setattr(GeminiPlanner, "generate", fake_generate)
    monkeypatch.setattr(module, "collect_metrics", services.metrics)
    monkeypatch.setattr(module, "httpx", SimpleNamespace(
        AsyncClient=lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(services.transport), **kwargs)))

    # Speed only this module's five-second heartbeat; don't monkeypatch asyncio globally.
    async def accelerated_sleep(delay):
        await REAL_SLEEP(.02 if delay == 5 else delay)

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        create_task=asyncio.create_task, gather=asyncio.gather, sleep=accelerated_sleep,
        CancelledError=asyncio.CancelledError))
    with TestClient(module.app) as client:
        yield SimpleNamespace(client=client, services=services, data=tmp_path)
        services.release_backend.set()


def start(harness):
    response = harness.client.post("/api/runs", json={"prompt": EXAMPLES[0], "mode": "live"})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def wait_terminal(harness, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        run = harness.client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"passed", "failed"} and not module.ACTIVE.locked():
            return run
        time.sleep(.01)
    pytest.fail("Mocked manager workflow did not finish")


def test_live_success_integrates_actual_manager_paths_with_test_doubles(live_harness):
    h = live_harness
    run_id = start(h)
    run = wait_terminal(h, run_id)
    assert run["status"] == "passed", run
    assert Counter(c["role"] for c in h.services.worker_calls) == {"backend": 1, "frontend": 1, "qa": 1}
    qa_request = next(c for c in h.services.worker_calls if c["role"] == "qa")
    assert "backend generation 1" in qa_request["files"]["pricing.py"]
    assert "checkout version 1" in qa_request["files"]["checkout.html"]
    validation = h.services.validation_calls[0]
    assert set(validation["files"]) == {"pricing.py", "checkout.html", "qa_tests.py"}
    assert validation["policy"] == POLICY.model_dump()
    assert h.services.review_calls == 1
    assert all(agent["status"] == "completed" for agent in run["agents"])
    assert h.client.get(run["preview_url"]).status_code == 200
    assert h.client.post(f"/api/runs/{run_id}/quote", json={"items": []}).json()["total_cents"] == 0
    artifact = h.client.get(f"/api/runs/{run_id}/artifacts").json()
    assert len(artifact["files"]) == 3
    assert all("EXPLICIT TEST FIXTURE" in file["after"] for file in artifact["files"])
    assert "_started" not in run and "files" not in run and "_metrics" not in run


@pytest.mark.parametrize("failure,expected_counts", [
    ("subtotal 10000 cents", {"backend": 2, "frontend": 1, "qa": 2}),
    ("checkout preview contract", {"backend": 1, "frontend": 2, "qa": 2}),
    ("supplemental QA tests", {"backend": 1, "frontend": 1, "qa": 2}),
])
def test_independent_failure_repairs_responsible_role(live_harness, failure, expected_counts):
    h = live_harness
    h.services.failures = [{failure}, set()]
    run_id = start(h)
    run = wait_terminal(h, run_id)
    assert run["status"] == "passed", run
    assert Counter(c["role"] for c in h.services.worker_calls) == expected_counts
    assert len(h.services.validation_calls) == 2
    repair_calls = h.services.worker_calls[3:]
    assert any(failure in call["instructions"] for call in repair_calls)
    qa_calls = [call for call in h.services.worker_calls if call["role"] == "qa"]
    if expected_counts["backend"] == 2:
        assert "backend generation 2" in qa_calls[-1]["files"]["pricing.py"]
    if expected_counts["frontend"] == 2:
        assert "checkout version 2" in qa_calls[-1]["files"]["checkout.html"]


def test_long_plan_leaves_room_for_repair_feedback(live_harness):
    h = live_harness
    h.services.task_instructions = "A" * 4000
    h.services.failure_detail = "Independent failure evidence " * 200
    h.services.failures = [{"subtotal 10000 cents"}, set()]
    run = wait_terminal(h, start(h))
    assert run["status"] == "passed", run
    repaired = [c for c in h.services.worker_calls if "Fix these independently" in c["instructions"]]
    assert repaired
    assert all(4000 < len(c["instructions"]) <= 8000 for c in repaired)


def test_oversized_plan_rejected_before_dispatch(live_harness):
    h = live_harness
    h.services.task_instructions = "A" * 4001
    run = wait_terminal(h, start(h))
    assert run["status"] == "failed"
    assert not h.services.worker_calls


@pytest.mark.parametrize("unsupported,invalid_files", [(True, False), (False, True)])
def test_unsupported_or_invalid_plan_fails_before_any_worker(live_harness, unsupported, invalid_files):
    h = live_harness
    h.services.unsupported = unsupported
    h.services.invalid_plan_files = invalid_files
    run_id = start(h)
    run = wait_terminal(h, run_id)
    assert run["status"] == "failed"
    assert run["error"]
    assert h.services.worker_calls == []
    assert h.services.validation_calls == []
    assert run["preview_url"] is None
    assert h.client.get(f"/preview/{run_id}/").status_code == 409


def test_failed_validation_never_exposes_preview_and_repair_is_bounded(live_harness):
    h = live_harness
    h.services.failures = [{"subtotal 10000 cents"}]
    run_id = start(h)
    run = wait_terminal(h, run_id)
    assert run["status"] == "failed"
    assert run["preview_url"] is None
    assert len(h.services.validation_calls) == 2
    assert Counter(c["role"] for c in h.services.worker_calls) == {"backend": 2, "frontend": 1, "qa": 2}
    assert h.services.review_calls == 0
    assert h.client.get(f"/preview/{run_id}/").status_code == 409
    assert h.client.post(f"/api/runs/{run_id}/quote", json={"items": []}).status_code == 409


def test_worker_failure_cannot_become_a_successful_preview(live_harness):
    h = live_harness
    h.services.worker_failure = "backend"
    run_id = start(h)
    run = wait_terminal(h, run_id)
    assert run["status"] == "failed"
    assert "simulated model failure" in run["error"]
    assert h.services.validation_calls == []
    assert run["preview_url"] is None


def test_active_heartbeat_metrics_are_persisted_before_next_workflow_event(live_harness):
    h = live_harness
    h.services.hold_backend = True
    run_id = start(h)
    assert h.services.backend_started.wait(1)
    deadline = time.monotonic() + 1
    metrics = {}
    while time.monotonic() < deadline:
        metrics = h.client.get(f"/api/runs/{run_id}/metrics").json()
        if len(metrics.get("samples", [])) >= 3:
            break
        time.sleep(.01)
    assert len(metrics["samples"]) >= 3
    saved = json.loads((h.data / f"{run_id}.json").read_text())
    assert saved["status"] == "running"
    assert len(saved["_metrics"]["samples"]) >= 2, "Heartbeat samples must survive a restart while a worker is running"
    h.services.release_backend.set()
    assert wait_terminal(h, run_id)["status"] == "passed"


def test_historical_metrics_are_frozen_and_restore_from_disk(live_harness):
    h = live_harness
    run_id = start(h)
    assert wait_terminal(h, run_id)["status"] == "passed"
    first = h.client.get(f"/api/runs/{run_id}/metrics").json()
    assert first["samples"]
    assert "EXPLICIT TEST FIXTURE" in first["note"]
    count = h.services.metrics_calls
    h.services.metrics_unavailable = True
    time.sleep(.06)
    second = h.client.get(f"/api/runs/{run_id}/metrics").json()
    assert second == first
    assert h.services.metrics_calls == count
    saved = json.loads((h.data / f"{run_id}.json").read_text())
    assert saved["_metrics"] == first
    module.RUNS[run_id] = saved
    assert h.client.get(f"/api/runs/{run_id}/metrics").json() == first


def test_manager_cpu_is_recorded_without_adding_a_fourth_model_worker(live_harness,monkeypatch):
    h=live_harness
    async def four_role_metrics():
        data=await h.services.metrics()
        data["samples"].append({"timestamp":"explicit-manager-fixture","role":"manager",
            "cpu_cores":0.125,"cpu_limit_cores":1,"machine_family":"c4a",
            "machine_type":"c4a-standard-2","architecture":"arm64",
            "placement_source":"kubernetes_node","pod_uid":"fixture-manager"})
        return data
    monkeypatch.setattr(module,"collect_metrics",four_role_metrics)
    run_id=start(h)
    run=wait_terminal(h,run_id)
    assert run["status"]=="passed"
    assert {agent["role"] for agent in run["agents"]}=={"backend","frontend","qa"}
    readings=h.client.get(f"/api/runs/{run_id}/metrics").json()
    managers=[row for row in readings["samples"] if row["role"]=="manager"]
    assert managers and managers[0]["cpu_limit_cores"]==1
    assert managers[0]["machine_type"]=="c4a-standard-2"
    saved=json.loads((h.data/f"{run_id}.json").read_text())
    assert saved["_metrics"]==readings


def test_unavailable_metrics_remain_empty_not_fabricated_zero(live_harness):
    h = live_harness
    h.services.metrics_unavailable = True
    run_id = start(h)
    assert wait_terminal(h, run_id)["status"] == "passed"
    metrics = h.client.get(f"/api/runs/{run_id}/metrics").json()
    assert metrics["source"] == "unavailable"
    assert metrics["samples"] == []
