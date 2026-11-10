#!/usr/bin/env python3
"""Render Kubernetes JSON (also valid YAML) without contacting any cluster.

Direct v1beta1 Sandbox resources deliberately avoid warm-pool API assumptions.
The API server's dry-run is the authority for installed CRD admission/schema.
"""
import argparse
import json
import ipaddress
from pathlib import Path
import re

APP = "axion-agent-studio"
# The local review companion requires the learner's actual network ranges.
def network_ranges(config):
    result = {}
    private_ranges = tuple(ipaddress.ip_network(cidr) for cidr in
                           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    for key in ("node_cidrs", "pod_cidrs", "service_cidrs"):
        values = config.get(key)
        if not isinstance(values, list) or not 1 <= len(values) <= 8:
            raise ValueError(f"{key} must list one to eight actual IPv4 CIDRs")
        networks = [ipaddress.ip_network(value) for value in values]
        if any(net.version != 4 or not 8 <= net.prefixlen <= 30
               or not any(net.subnet_of(private) for private in private_ranges)
               for net in networks):
            raise ValueError(f"{key} must use bounded RFC1918 IPv4 networks")
        result[key] = networks
    flattened = [net for group in result.values() for net in group]
    if any(left.overlaps(right) for i, left in enumerate(flattened)
           for right in flattened[i + 1:]):
        raise ValueError("Node, Pod and Service network ranges must not overlap")
    return result

# This trusted init program copies data only. Generated source inside a saved run
# remains JSON text; only the isolated validator may execute candidate code.
RESTORE_RUNS_SCRIPT = r'''import hashlib, json, re, sys
from pathlib import Path
seed, destination = map(Path, sys.argv[1:])
expected = {}
for line in (seed / "SHA256SUMS").read_text().splitlines():
    match = re.fullmatch(r"([0-9a-f]{64})  ([0-9a-f]{12}\.json)", line)
    if not match or match[2] in expected:
        raise SystemExit("Invalid run archive checksum manifest")
    expected[match[2]] = match[1]
if not 1 <= len(expected) <= 50:
    raise SystemExit("Run archive must contain between one and fifty completed runs")
if {path.name for path in seed.glob("*.json")} != set(expected):
    raise SystemExit("Run archive file set does not match checksum manifest")
verified = {}
for name, digest in expected.items():
    raw = (seed / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise SystemExit("Run archive checksum mismatch")
    run = json.loads(raw)
    if (run.get("id") != name[:-5] or run.get("status") not in ("passed", "failed")
            or run.get("mode") not in ("live", "rehearsal")):
        raise SystemExit("Run archive contains an invalid or unfinished run")
    verified[name] = raw
if sum(map(len, verified.values())) > 1024 * 1024:
    raise SystemExit("Run archive exceeds the bounded snapshot size")
destination.mkdir(parents=True, exist_ok=True)
for name, raw in verified.items():
    target = destination / name
    if target.is_symlink() or (target.exists() and target.read_bytes() != raw):
        raise SystemExit("Refusing to replace an existing different run")
for name, raw in verified.items():
    target = destination / name
    if not target.exists():
        temporary = destination / (".restore-" + name + ".tmp")
        temporary.write_bytes(raw)
        temporary.chmod(0o600)
        temporary.replace(target)
print("Restored %d verified completed run records" % len(verified))
'''
# Preserve the exact legacy init command for one ConfigMap. A projected archive
# may combine up to eight ConfigMaps, each still subject to Kubernetes' 1 MiB cap.
RESTORE_PROJECTED_RUNS_SCRIPT = RESTORE_RUNS_SCRIPT.replace(
    "if sum(map(len, verified.values())) > 1024 * 1024:",
    "if sum(map(len, verified.values())) > 8 * 1024 * 1024:")


def metadata(name, namespace, labels=None):
    return {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/part-of": APP, **(labels or {})}}


def obj(kind, name, ns, spec=None, api="v1", **extra):
    result = {"apiVersion": api, "kind": kind, "metadata": metadata(name, ns), **extra}
    if spec is not None:
        result["spec"] = spec
    return result


def security():
    return {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}}


def env(values):
    return [{"name": k, "value": str(v)} for k, v in values.items()]


def node_metrics_addresses(config):
    """Validate the optional, operator-verified node names and primary IPv4s."""
    addresses = config.get("node_metrics_addresses", {})
    node_networks = network_ranges(config)["node_cidrs"]
    if not isinstance(addresses, dict) or len(addresses) > 8:
        raise ValueError("node_metrics_addresses must map at most eight verified demo node names to IPv4 addresses")
    result = {}
    for name, value in addresses.items():
        if (not isinstance(name, str) or len(name) > 253
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name)):
            raise ValueError("node_metrics_addresses contains a invalid Kubernetes node name")
        if not isinstance(value, str):
            raise ValueError("node_metrics_addresses values must be IPv4 strings, not ranges or URLs")
        address = ipaddress.ip_address(value)
        if address.version != 4 or not any(address in network
                and address not in (network.network_address, network.broadcast_address) for network in node_networks):
            raise ValueError("node_metrics_addresses must use verified IPv4s in the configured node subnets")
        result[name] = str(address)
    if len(set(result.values())) != len(result):
        raise ValueError("Each node_metrics_addresses node must have a distinct verified IPv4")
    return dict(sorted(result.items()))


def node_metrics_rbac(ns, addresses):
    if not addresses:
        return []
    name = f"{ns}-node-stats"
    meta = {"name": name, "labels": {"app.kubernetes.io/part-of": APP}}
    return [
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": meta,
         "rules": [{"apiGroups": [""], "resources": ["nodes/stats"], "verbs": ["get"],
                    "resourceNames": sorted(addresses)},
                   {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get"],
                    "resourceNames": sorted(addresses)}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": meta.copy(),
         "subjects": [{"kind": "ServiceAccount", "name": "studio-manager", "namespace": ns}],
         "roleRef": {"kind": "ClusterRole", "name": name, "apiGroup": "rbac.authorization.k8s.io"}},
    ]


def resources(cpu, memory, cpu_limit, memory_limit, disk="1Gi"):
    return {"requests": {"cpu": cpu, "memory": memory, "ephemeral-storage": disk},
            "limits": {"cpu": cpu_limit, "memory": memory_limit, "ephemeral-storage": disk}}


def sandbox_spec(pool, containers, volumes, init=None):
    result = {
        "runtimeClassName": "gvisor", "restartPolicy": "OnFailure",
        "automountServiceAccountToken": False, "serviceAccountName": "sandbox-worker",
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
        "nodeSelector": {"kubernetes.io/arch": "arm64", "sandbox.gke.io/runtime": "gvisor",
                         "studio.axion.dev/pool": pool},
        "tolerations": [{"key": "sandbox.gke.io/runtime", "operator": "Equal", "value": "gvisor", "effect": "NoSchedule"},
                        {"key": "kubernetes.io/arch", "operator": "Equal", "value": "arm64", "effect": "NoSchedule"}],
        "containers": containers, "volumes": volumes,
    }
    if init:
        result["initContainers"] = init
    return result


def sandbox(name, ns, labels, podspec):
    return obj("Sandbox", name, ns, {"operatingMode": "Running", "podTemplate": {"metadata": {"labels": labels}, "spec": podspec}},
               api="agents.x-k8s.io/v1beta1")


def service(name, ns, labels, port):
    return obj("Service", name, ns, {"type": "ClusterIP", "selector": labels,
                                   "ports": [{"name": "http", "port": port, "targetPort": port}]})


def worker(role, config):
    ns = config["namespace"]
    pool = config.get("qa_pool", "c4a-sandbox") if role == "qa" else "c4a-sandbox"
    family = pool.split("-", 1)[0]
    labels = {"app": APP, "component": "worker", "role": role, "studio.axion/role": role}
    name = f"worker-{role}"
    tmp = {"name": "tmp", "mountPath": "/tmp"}
    model = {"name": "model", "mountPath": "/models", "readOnly": True}
    model_path = "/models/" + config["model_filename"]
    containers = [
        {"name": "worker", "image": config["worker_image"], "securityContext": security(),
         "ports": [{"name": "http", "containerPort": 8081}],
         "env": env({"LLAMA_BASE_URL": "http://127.0.0.1:8080/v1", "MODEL_NAME": config["model_name"],
                     "MACHINE_FAMILY": family, "WORKER_ROLE": role, "WORKER_ISOLATION": "gvisor",
                     "LLAMA_CONTEXT": config["llama_context"]}),
         "resources": resources("250m", "512Mi", "500m", "1Gi"), "volumeMounts": [tmp],
         "readinessProbe": {"httpGet": {"path": "/health", "port": 8081}, "initialDelaySeconds": 5, "periodSeconds": 5, "timeoutSeconds": 3},
         "startupProbe": {"httpGet": {"path": "/health", "port": 8081}, "failureThreshold": 180, "periodSeconds": 5, "timeoutSeconds": 3}},
        {"name": "llama", "image": config["llama_image"], "securityContext": security(),
         "command": [config.get("llama_binary", "/app/llama-server")],
         "args": ["--model", model_path, "--alias", config["model_name"], "--host", "127.0.0.1", "--port", "8080",
                  "--threads", str(config["llama_threads"]), "--threads-batch", str(config["llama_threads"]),
                  "--ctx-size", str(config["llama_context"]), "--parallel", "1", "--n-gpu-layers", "0",
                  "--jinja", "--reasoning-budget", "0", "--metrics"],
         "env": env({"MODEL_PATH": model_path, "MODEL_NAME": config["model_name"], "LLAMA_THREADS": config["llama_threads"], "LLAMA_CONTEXT": config["llama_context"]}),
         "resources": resources("4", "8Gi", "6", "12Gi", "6Gi"), "volumeMounts": [model, tmp],
         # The worker uses loopback; no Service exposes model port 8080.
         },
    ]
    init = [{"name": "load-model", "image": config["model_image"], "securityContext": security(),
             "env": env({"MODEL_FILENAME": config["model_filename"]}),
             "resources": resources("250m", "256Mi", "500m", "512Mi", "6Gi"),
             "volumeMounts": [{"name": "model", "mountPath": "/models"}, tmp]}]
    volumes = [{"name": "model", "emptyDir": {"sizeLimit": "6Gi"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    return [sandbox(name, ns, labels, sandbox_spec(pool, containers, volumes, init)), service(name, ns, labels, 8081)]


def validator(config, phase):
    ns = config["namespace"]
    labels = {"app": APP, "component": "validator"}
    container = {"name": "validator", "image": config["studio_image"], "securityContext": security(),
                 "command": ["python", "-m", "uvicorn", "validation.app:app", "--host", "0.0.0.0", "--port", "8081"],
                 "ports": [{"name": "http", "containerPort": 8081}],
                 "env": env({"VALIDATOR_ISOLATED": "1", "SANDBOX_RUNTIME": "gvisor", "STUDIO_DATA_DIR": "/data"}),
                 "resources": resources("250m", "512Mi", "1", "2Gi", "2Gi"),
                 "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}, {"name": "data", "mountPath": "/data"}],
                 "readinessProbe": {"httpGet": {"path": "/health", "port": 8081}, "periodSeconds": 5}}
    pool = "c4a-sandbox" if phase == "checkpoint" else config.get("validator_pool", "n4a-sandbox")
    volumes = [{"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}, {"name": "data", "emptyDir": {"sizeLimit": "1Gi"}}]
    return [sandbox("validator", ns, labels, sandbox_spec(pool, [container], volumes)), service("validator", ns, labels, 8081)]


def manager(config, phase):
    ns = config["namespace"]
    labels = {"app": APP, "component": "manager"}
    values = {"GOOGLE_CLOUD_PROJECT": config["project"], "GOOGLE_CLOUD_LOCATION": config["google_cloud_location"],
              "GEMINI_BACKEND": config["gemini_backend"], "GEMINI_MODEL": config["gemini_model"], "STUDIO_DATA_DIR": "/data",
              "WORKER_BACKEND_URL": "http://worker-backend:8081", "VALIDATOR_URL": "http://validator:8081",
              "KUBERNETES_NAMESPACE": ns, "STUDIO_NAMESPACE": ns,
              "STUDIO_ENVIRONMENT": "GKE Agent Sandbox on Google Axion", "GEMMA_MODEL": config["model_name"]}
    presentation = {"featured_run_id": "STUDIO_FEATURED_RUN_ID", "worker_node_vcpus": "STUDIO_WORKER_NODE_VCPUS",
                    "manager_node_vcpus": "STUDIO_MANAGER_NODE_VCPUS", "llama_threads": "STUDIO_INFERENCE_THREADS"}
    for key, variable in presentation.items():
        if key in config:
            values[variable] = config[key]
    if phase == "full":
        values.update(WORKER_FRONTEND_URL="http://worker-frontend:8081", WORKER_QA_URL="http://worker-qa:8081")
    addresses = node_metrics_addresses(config)
    if addresses:
        values["STUDIO_NODE_METRICS_ADDRESSES"] = json.dumps(addresses, sort_keys=True, separators=(",", ":"))
    variables = env(values)
    if config["gemini_backend"] == "api":
        variables.append({"name": "GEMINI_API_KEY", "valueFrom": {"secretKeyRef": {"name": "gemini-api", "key": "api-key"}}})
    pod = {"serviceAccountName": "studio-manager", "automountServiceAccountToken": True,
           "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
           "nodeSelector": {"kubernetes.io/arch": "arm64", "studio.axion.dev/pool": config.get("manager_pool", "n4a-regular")},
           "tolerations": [{"key": "kubernetes.io/arch", "operator": "Equal", "value": "arm64", "effect": "NoSchedule"}],
           "containers": [{"name": "manager", "image": config.get("manager_image", config["studio_image"]), "env": variables, "securityContext": security(),
                           "ports": [{"name": "http", "containerPort": 8080}],
                           "resources": resources("250m", "512Mi", "1", "2Gi", "2Gi"),
                           "volumeMounts": [{"name": "data", "mountPath": "/data"}, {"name": "tmp", "mountPath": "/tmp"}],
                           "readinessProbe": {"httpGet": {"path": "/health", "port": 8080}, "periodSeconds": 5}}],
           "volumes": [{"name": "data", "emptyDir": {"sizeLimit": "1Gi"}}, {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}]}
    if config.get("manager_restore_configmap"):
        additional = config.get("manager_restore_additional_configmaps", [])
        if additional:
            # Split immutable snapshots use disjoint JSON keys and exactly one
            # complete SHA256SUMS manifest across all sources. Verify this union
            # against the current raw backup before a manager-only rollout.
            pod["volumes"].append({"name": "recorded-runs", "projected": {
                "defaultMode": 0o444, "sources": [{"configMap": {"name": name}}
                    for name in [config["manager_restore_configmap"], *additional]]}})
        else:
            pod["volumes"].append({"name": "recorded-runs", "configMap": {
                "name": config["manager_restore_configmap"], "defaultMode": 0o444}})
        # Use the unchanged studio runtime for byte restoration, independently
        # of a new manager/UI image. The seed is never mounted in the app.
        pod["initContainers"] = [{"name": "restore-recorded-runs", "image": config["studio_image"],
            "command": ["python", "-I", "-c", RESTORE_PROJECTED_RUNS_SCRIPT if additional else RESTORE_RUNS_SCRIPT, "/seed", "/data"],
            "securityContext": security(), "resources": resources("50m", "64Mi", "100m", "128Mi"),
            "volumeMounts": [{"name": "recorded-runs", "mountPath": "/seed", "readOnly": True},
                             {"name": "data", "mountPath": "/data"}]}]
    deployment = obj("Deployment", "studio-manager", ns,
                     {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": labels},
                      "template": {"metadata": {"labels": labels}, "spec": pod}}, api="apps/v1")
    return [deployment, service("studio-manager", ns, labels, 8080)]


def network_policies(ns, kube_api_cidr, node_addresses=None):
    api = "networking.k8s.io/v1"
    manager_peer = {"podSelector": {"matchLabels": {"app": APP, "component": "manager"}}}
    workers_peer = {"podSelector": {"matchLabels": {"app": APP, "component": "worker"}}}
    validator_peer = {"podSelector": {"matchLabels": {"app": APP, "component": "validator"}}}
    # No egress at all from worker/validator Pods. Localhost model inference stays inside each Pod.
    default = obj("NetworkPolicy", "default-deny", ns, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}, api=api)
    execution = obj("NetworkPolicy", "manager-to-execution", ns, {
        "podSelector": {"matchExpressions": [{"key": "component", "operator": "In", "values": ["worker", "validator"]}]},
        "policyTypes": ["Ingress"], "ingress": [{"from": [manager_peer], "ports": [{"protocol": "TCP", "port": 8081}]}]}, api=api)
    manager = obj("NetworkPolicy", "manager-egress", ns, {
        "podSelector": manager_peer["podSelector"], "policyTypes": ["Egress"], "egress": [
            {"to": [workers_peer, validator_peer], "ports": [{"protocol": "TCP", "port": 8081}]},
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
             "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
            # Hosted Gemini uses HTTPS. NetworkPolicy is L3/L4, not a DNS hostname firewall.
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}],
             "ports": [{"protocol": "TCP", "port": 443}]},
            # WIF metadata endpoint for trusted manager only; required with GKE Dataplane V2.
            {"to": [{"ipBlock": {"cidr": "169.254.169.254/32"}}], "ports": [{"protocol": "TCP", "port": 80}]},
            # Actual address behind default/kubernetes, verified after creation.
            {"to": [{"ipBlock": {"cidr": kube_api_cidr}}], "ports": [{"protocol": "TCP", "port": 443}]},
        ]}, api=api)
    if node_addresses:
        manager["spec"]["egress"].append({
            "to": [{"ipBlock": {"cidr": f"{address}/32"}} for address in sorted(node_addresses.values())],
            "ports": [{"protocol": "TCP", "port": 10250}]})
    # No public Service/Ingress. Operator accesses manager via authenticated kubectl port-forward.
    return [default, execution, manager]


def render(config, phase="full"):
    ns = config["namespace"]
    ranges = network_ranges(config)
    if ns != "axion-agent-studio":
        raise ValueError("This companion uses the dedicated namespace axion-agent-studio")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", ns):
        raise ValueError("Invalid namespace")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", config["model_filename"]):
        raise ValueError("Model filename must be a plain GGUF filename")
    if "manager_image" in config and (not isinstance(config["manager_image"], str)
            or not config["manager_image"] or re.search(r"\s", config["manager_image"])):
        raise ValueError("manager_image must be a nonempty container image reference")
    if "manager_restore_configmap" in config:
        name = config["manager_restore_configmap"]
        if (not isinstance(name, str) or len(name) > 63
                or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name)):
            raise ValueError("manager_restore_configmap must be a namespaced ConfigMap name of at most 63 characters")
    if "manager_restore_additional_configmaps" in config:
        names = config["manager_restore_additional_configmaps"]
        if not isinstance(names, list) or len(names) > 7 or not config.get("manager_restore_configmap"):
            raise ValueError("manager_restore_additional_configmaps requires a primary archive and a list of at most seven additional ConfigMap names")
        if any(not isinstance(name, str) or len(name) > 63
               or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name) for name in names):
            raise ValueError("manager_restore_additional_configmaps must contain namespaced ConfigMap names of at most 63 characters")
        if len(set(names)) != len(names) or config["manager_restore_configmap"] in names:
            raise ValueError("manager_restore_additional_configmaps must not repeat any archive ConfigMap name")
    if "featured_run_id" in config and (not isinstance(config["featured_run_id"], str)
            or not re.fullmatch(r"[0-9a-f]{12}", config["featured_run_id"])):
        raise ValueError("featured_run_id must identify an existing 12-character run")
    for key in ("worker_node_vcpus", "manager_node_vcpus", "llama_threads"):
        if key in config and (type(config[key]) is not int or not 1 <= config[key] <= 1024):
            raise ValueError(f"{key} must be a verified positive integer, or omitted when unknown")
    for key in ("qa_pool", "validator_pool"):
        if config.get(key, "n4a-sandbox") not in ("c4a-sandbox", "n4a-sandbox"):
            raise ValueError(f"{key} must be c4a-sandbox or n4a-sandbox")
    if config.get("manager_pool", "n4a-regular") not in ("regular", "n4a-regular"):
        raise ValueError("manager_pool must be regular or n4a-regular")
    if config["gemini_backend"] not in ("vertex", "api"):
        raise ValueError("gemini_backend must be vertex or api")
    if not config.get("kube_api_cidr"):
        raise ValueError("kube_api_cidr is required: provide the verified control-plane endpoint IPv4 /32")
    api_network = ipaddress.ip_network(config["kube_api_cidr"])
    if api_network.version != 4 or api_network.prefixlen != 32:
        raise ValueError("kube_api_cidr must be one verified control-plane endpoint IPv4 /32, not a Service IP or range")
    endpoint = api_network.network_address
    if (endpoint.is_unspecified or endpoint.is_loopback or endpoint.is_link_local or endpoint.is_multicast
            or endpoint.is_reserved or any(endpoint in network for network in ranges["pod_cidrs"] + ranges["service_cidrs"])):
        raise ValueError("kube_api_cidr must target the control-plane endpoint, not a Pod, Service, or special-purpose IP")
    addresses = node_metrics_addresses(config)
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {"app.kubernetes.io/part-of": APP}}}
    result = [namespace, obj("ServiceAccount", "sandbox-worker", ns, automountServiceAccountToken=False),
              obj("ServiceAccount", "studio-manager", ns)]
    result += [obj("Role", "studio-observer", ns, api="rbac.authorization.k8s.io/v1",
                   rules=[{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
                          {"apiGroups": ["metrics.k8s.io"], "resources": ["pods"], "verbs": ["get", "list"]}]),
               obj("RoleBinding", "studio-observer", ns, api="rbac.authorization.k8s.io/v1",
                   subjects=[{"kind": "ServiceAccount", "name": "studio-manager", "namespace": ns}],
                   roleRef={"kind": "Role", "name": "studio-observer", "apiGroup": "rbac.authorization.k8s.io"})]
    result += node_metrics_rbac(ns, addresses)
    result += network_policies(ns, str(api_network), addresses)
    roles = ["backend"] if phase == "checkpoint" else ["backend", "frontend", "qa"]
    for role in roles:
        result += worker(role, config)
    result += validator(config, phase)
    # gVisor does not support kubectl port-forward. The trusted regular-pool
    # manager also runs the checkpoint verifier, reaching only internal APIs.
    # Missing frontend/QA endpoints keep the full live workflow unavailable.
    result += manager(config, phase)
    # ResourceQuota constrains manifests/accidental replicas, not node billing or API charges.
    result.append(obj("ResourceQuota", "demo-bound", ns, {"hard": {"pods": "8", "requests.cpu": "20", "limits.cpu": "24", "requests.memory": "40Gi", "limits.memory": "48Gi", "requests.ephemeral-storage": "32Gi", "limits.ephemeral-storage": "40Gi", "services.loadbalancers": "0"}}))
    return {"apiVersion": "v1", "kind": "List", "items": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--phase", choices=["checkpoint", "full"], default="checkpoint")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-digests", action="store_true", help="Reject mutable image tags before deployment")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.require_digests:
        for key in ("worker_image", "studio_image", "model_image", "llama_image"):
            if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", config.get(key, "")):
                raise ValueError(f"{key} must be an immutable image digest")
    data = json.dumps(render(config, args.phase), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data)
    else:
        print(data, end="")


if __name__ == "__main__":
    main()
