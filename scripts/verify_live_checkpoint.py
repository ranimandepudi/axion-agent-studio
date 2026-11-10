"""Run one genuine backend task, then validate its code in the remote sandbox.

Default access uses existing HTTP loopback endpoints. Explicit --in-cluster
mode permits only the two named services from the regular manager Pod, because
GKE gVisor does not support kubectl port-forward. There is no rehearsal mode,
model fallback, deployment, cloud SDK invocation, or local execution of
generated code. A passing result covers this backend checkpoint, not the full
demo or independently verified infrastructure identity.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
import uuid

import httpx


MAX_RESPONSE_BYTES = 512_000
POLICY = {"discount_percent": 10, "discount_threshold_cents": 10000,
          "free_shipping_threshold_cents": 15000, "base_shipping_cents": 799}
INSTRUCTIONS = (
    "Implement quote(items, catalog) for this business request: apply a 10% discount "
    "when the original subtotal is $100 or more; provide free shipping when the "
    "original subtotal is $150 or more; otherwise charge $7.99 shipping. "
    "Calculate in integer cents and floor the discount. Empty carts have zero "
    "discount, shipping and total. Preserve input validation and return all existing "
    "quote fields, including meaningful discount_label and shipping_label strings. "
    "Return the complete changed pricing.py file. No other files or dependencies."
)
REQUIRED_CASES = {"empty cart", "subtotal 9999 cents", "subtotal 10000 cents",
                  "subtotal 10001 cents", "subtotal 14999 cents", "subtotal 15000 cents",
                  "subtotal 15001 cents", "multiple items and quantities"}
MONEY_FIELDS = ("subtotal_cents", "discount_cents", "shipping_cents", "total_cents")


@dataclass(frozen=True)
class Configuration:
    worker_url: str
    validator_url: str
    output: Path
    project: Path = Path(__file__).resolve().parents[1]
    expected_family: str = "c4a"
    llama_url: str | None = None
    max_worker_seconds: float = 180
    in_cluster: bool = False


def loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Endpoints must be HTTP loopback base URLs without credentials, paths or query strings")
    # Accessing port also rejects malformed port specifications.
    if parsed.port is None:
        raise ValueError("Specify an explicit local port-forward port")
    return value.rstrip("/")


def endpoint_url(value: str, service: str, *, in_cluster: bool) -> str:
    if not in_cluster:
        return loopback_url(value)
    allowed = {"worker": "http://worker-backend:8081", "validator": "http://validator:8081"}
    if (not os.getenv("KUBERNETES_SERVICE_HOST")
            or os.getenv("STUDIO_NAMESPACE") != "axion-agent-studio"):
        raise ValueError("--in-cluster requires Kubernetes service environment and STUDIO_NAMESPACE=axion-agent-studio")
    if value.rstrip("/") != allowed.get(service):
        raise ValueError("--in-cluster permits only http://worker-backend:8081 and http://validator:8081 in their assigned roles")
    return allowed[service]


def request_body(client: httpx.Client, method: str, url: str, *, payload=None, timeout=15) -> bytes:
    started = time.monotonic()
    with client.stream(method, url, json=payload, timeout=timeout) as response:
        if response.status_code >= 300:
            raise RuntimeError(f"{method} {urlsplit(url).path} returned HTTP {response.status_code}")
        result = bytearray()
        for part in response.iter_bytes():
            result.extend(part)
            if len(result) > MAX_RESPONSE_BYTES:
                raise ValueError("Service response exceeded the bounded response size")
            if time.monotonic() - started > timeout:
                raise TimeoutError("Service response exceeded its request time budget")
        return bytes(result)


def request_json(client, method, url, *, payload=None, timeout=15) -> dict:
    data = json.loads(request_body(client, method, url, payload=payload, timeout=timeout))
    if not isinstance(data, dict):
        raise ValueError("Service response must be a JSON object")
    return data


def metric_snapshot(client: httpx.Client, base: str | None) -> dict:
    if not base:
        return {"available": False, "source": "unavailable", "raw": None,
                "note": "No llama.cpp metrics port-forward supplied; inference metrics were not collected."}
    try:
        text = request_body(client, "GET", base + "/metrics", timeout=5).decode()
        counters = {}
        for line in text.splitlines():
            match = re.fullmatch(r"([a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[^\n]*\})?)\s+([-+0-9.eE]+)(?:\s+\d+)?", line)
            if match:
                value = float(match[2])
                if math.isfinite(value):
                    counters[match[1]] = value
        return {"available": True, "source": "llama_cpp_prometheus", "raw": text,
                "values": counters, "captured_at": datetime.now(timezone.utc).isoformat(),
                "note": "Server-wide counters/gauges, not CPU utilization. Keep this worker exclusive during the checkpoint."}
    except (httpx.HTTPError, RuntimeError, ValueError, TimeoutError) as exc:
        return {"available": False, "source": "unavailable", "raw": None,
                "note": f"Optional inference metrics unavailable: {type(exc).__name__}"}


def expected_quote(items: list[dict], catalog: list[dict]) -> dict:
    prices = {product["sku"]: product["price_cents"] for product in catalog}
    subtotal = sum(prices[item["sku"]] * item["quantity"] for item in items)
    discount = subtotal * 10 // 100 if subtotal >= 10000 else 0
    shipping = 0 if subtotal == 0 or subtotal >= 15000 else 799
    return dict(zip(MONEY_FIELDS, (subtotal, discount, shipping, subtotal - discount + shipping)))


def quote_probes(catalog: list[dict]) -> list[dict]:
    product = next((p for p in catalog if type(p.get("price_cents")) is int and p["price_cents"] > 0), None)
    if product is None:
        raise ValueError("The prepared catalog needs a product with a positive integer-cent price")
    quantities = {1, min(100, math.ceil(10000 / product["price_cents"])),
                  min(100, math.ceil(15000 / product["price_cents"]))}
    return [{"name": "empty basket", "items": []}] + [
        {"name": f"prepared catalog: {quantity} × {product['sku']}",
         "items": [{"sku": product["sku"], "quantity": quantity}]} for quantity in sorted(quantities)]


def verified_acceptance(result: dict) -> bool:
    tests = result.get("tests", {})
    if not isinstance(tests, dict):
        return False
    cases = tests.get("cases", [])
    if not isinstance(cases, list) or not cases or any(not isinstance(case, dict) for case in cases):
        return False
    if (any(not isinstance(case.get("name"), str) for case in cases)
            or any(type(tests.get(field)) is not int for field in ["total", "passed", "failed"])):
        return False
    names = {case.get("name") for case in cases}
    if len(names) != len(cases):
        return False
    return (result.get("status") == "passed" and tests.get("failed") == 0
            and tests.get("passed") == tests.get("total") == len(cases)
            and all(case.get("passed") is True for case in cases)
            and REQUIRED_CASES.issubset(names))


def save_evidence(path: Path, evidence: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2) + "\n")
    temporary.replace(path)


def run_checkpoint(config: Configuration, *, transport=None) -> dict:
    """transport is an injection point for unit tests; the CLI always uses real HTTP."""
    worker = endpoint_url(config.worker_url, "worker", in_cluster=config.in_cluster)
    validator = endpoint_url(config.validator_url, "validator", in_cluster=config.in_cluster)
    if config.in_cluster and config.llama_url:
        raise ValueError("--llama-url is unavailable in --in-cluster mode; collect model/Pod metrics separately")
    llama = loopback_url(config.llama_url) if config.llama_url else None
    if config.expected_family not in {"c4a", "n4a"} or not 1 <= config.max_worker_seconds <= 600:
        raise ValueError("Invalid machine family or worker latency target")
    started = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    evidence = {
        "schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "real_backend_worker_and_isolated_validator_http", "run_id": run_id,
        "access_mode": "in_cluster_services" if config.in_cluster else "loopback_port_forwards",
        "scope": "one backend specialist; no hosted Gemini, frontend agent or QA agent called",
        "policy": POLICY, "instructions": INSTRUCTIONS, "status": "failed",
        "checkpoint_passed": False, "full_demo_ready": False, "error": None,
        "worker_health": None, "validator_health": None, "worker_result": None,
        "protected_validation": None, "quote_probes": [], "latency": {},
        "metrics": {"worker_api_process": None, "inference_before": None, "inference_after": None},
        "infrastructure_verification": {
            "performed_by_this_script": False,
            "note": "Health reports do not independently prove GKE, Axion family, gVisor runtime or image/model digests. Preserve Kubernetes evidence separately."},
        "generated_code_executed_on_host": False,
        "frontend_source": "unchanged prepared sample_shop/checkout.html, not generated by this checkpoint",
    }
    try:
        files = {name: (config.project / "sample_shop" / name).read_text()
                 for name in ["pricing.py", "checkout.html", "catalog.json"]}
        catalog = json.loads(files["catalog.json"])
        if not isinstance(catalog, list):
            raise ValueError("Prepared catalog must be a JSON array")
        evidence["input_sha256"] = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False, transport=transport) as client:
            health = request_json(client, "GET", worker + "/health")
            evidence["worker_health"] = health
            if (health.get("status") != "ready" or health.get("role") != "backend"
                    or health.get("architecture") not in {"aarch64", "arm64"}
                    or health.get("machine_family") != config.expected_family):
                raise ValueError("Worker must report a ready backend model on the expected Arm machine family")
            validation_health = request_json(client, "GET", validator + "/health")
            evidence["validator_health"] = validation_health
            if (validation_health.get("status") != "ready" or validation_health.get("isolated_execution") is not True
                    or validation_health.get("architecture") not in {"aarch64", "arm64"}
                    or validation_health.get("runtime") not in {"gvisor", "runsc"}):
                raise ValueError("Validator must report ready isolated execution on Arm with gVisor")
            evidence["metrics"]["inference_before"] = metric_snapshot(client, llama)
            generation_started = time.monotonic()
            result = request_json(client, "POST", worker + "/execute", timeout=min(660, config.max_worker_seconds + 60),
                payload={"run_id": run_id, "role": "backend", "instructions": INSTRUCTIONS, "policy": POLICY, "files": files})
            worker_seconds = round(time.monotonic() - generation_started, 3)
            evidence["worker_result"] = result
            evidence["latency"].update(worker_wall_seconds=worker_seconds,
                worker_reported_seconds=result.get("elapsed_seconds"), target_seconds=config.max_worker_seconds,
                within_target=worker_seconds <= config.max_worker_seconds)
            evidence["metrics"]["worker_api_process"] = {
                "reported": result.get("metrics"),
                "note": "Worker response metrics cover the Python API process only; they exclude model CPU and memory."}
            evidence["metrics"]["inference_after"] = metric_snapshot(client, llama)
            output = result.get("files", {})
            if (result.get("status") != "completed" or result.get("role") != "backend"
                    or not isinstance(output, dict) or set(output) != {"pricing.py"}
                    or not isinstance(output["pricing.py"], str) or not output["pricing.py"].strip()
                    or len(output["pricing.py"].encode()) > 64_000):
                raise ValueError("Worker did not return one completed, bounded backend file")
            candidate = output["pricing.py"]
            if candidate == files["pricing.py"]:
                raise ValueError("Worker returned an unchanged starting file")
            candidate_path = config.output.with_suffix(".candidate.py")
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(candidate)
            evidence["candidate_path"] = str(candidate_path.resolve())
            evidence["candidate_sha256"] = hashlib.sha256(candidate.encode()).hexdigest()
            ast.parse(candidate, filename="pricing.py")  # Parse only; never import or execute generated code.
            evidence["python_syntax_valid"] = True
            validation_started = time.monotonic()
            validation = request_json(client, "POST", validator + "/validate", timeout=45,
                payload={"run_id": run_id, "policy": POLICY,
                         "files": {"pricing.py": candidate, "checkout.html": files["checkout.html"]}, "catalog": catalog})
            evidence["protected_validation"] = validation
            evidence["latency"]["validation_wall_seconds"] = round(time.monotonic() - validation_started, 3)
            if not verified_acceptance(validation):
                raise ValueError("Protected acceptance checks failed or returned incomplete/inconsistent test evidence")
            for probe in quote_probes(catalog):
                actual = request_json(client, "POST", validator + f"/quote/{run_id}", payload={"items": probe["items"]})
                expected = expected_quote(probe["items"], catalog)
                passed = (all(type(actual.get(key)) is int and actual[key] == expected[key] for key in MONEY_FIELDS)
                          and all(isinstance(actual.get(key), str) and bool(actual[key].strip())
                                  for key in ["discount_label", "shipping_label"]))
                evidence["quote_probes"].append({**probe, "expected": expected, "actual": actual, "passed": passed})
            if not all(probe["passed"] for probe in evidence["quote_probes"]):
                raise ValueError("A remotely executed checkout quote failed monetary or label checks")
            if not evidence["latency"]["within_target"]:
                raise ValueError("Code passed functional checks but exceeded the declared worker latency target")
            evidence.update(status="passed", checkpoint_passed=True)
    except (httpx.HTTPError, RuntimeError, ValueError, SyntaxError, TimeoutError, OSError, KeyError, TypeError) as exc:
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:700]}"
    finally:
        evidence["latency"]["total_wall_seconds"] = round(time.monotonic() - started, 3)
        save_evidence(config.output, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-url", required=True, help="Existing loopback endpoint or exact in-cluster worker service")
    parser.add_argument("--validator-url", required=True, help="Existing loopback endpoint or exact in-cluster validator service")
    parser.add_argument("--in-cluster", action="store_true", help="Opt into exact named services from the regular manager Pod only")
    parser.add_argument("--llama-url", help="Optional existing llama.cpp loopback port-forward for /metrics")
    parser.add_argument("--expect-family", choices=["c4a", "n4a"], default="c4a")
    parser.add_argument("--max-worker-seconds", type=float, default=180)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_checkpoint(Configuration(args.worker_url, args.validator_url, args.output, args.project,
                               args.expect_family, args.llama_url, args.max_worker_seconds, args.in_cluster))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"checkpoint_passed": result["checkpoint_passed"], "error": result["error"],
                      "latency": result["latency"], "evidence": str(args.output.resolve()),
                      "full_demo_ready": False}))
    return 0 if result["checkpoint_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
