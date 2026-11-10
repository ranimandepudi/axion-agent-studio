"""Actual Kubernetes metrics; unavailable is never converted into zero."""
import os
import math
import asyncio
import copy
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx

CA_FILE="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
CONTAINER_METRICS_MAX_AGE_SECONDS=60
POD_METRICS_MAX_AGE_SECONDS=60
POD_METRICS_FUTURE_TOLERANCE_SECONDS=5
SUMMARY_MAX_BYTES=4*1024*1024
NODE_METRICS_TIMEOUT_SECONDS=5
WORKER_ROLES={"backend","frontend","qa"}
CURRENT_METRICS_CACHE_SECONDS=5
CURRENT_METRICS_TIMEOUT_SECONDS=10


def _telemetry_role(pod):
    labels=pod.get("metadata",{}).get("labels",{})
    if labels.get("studio.axion/role") in WORKER_ROLES:
        return labels["studio.axion/role"]
    # The coordinator is a trusted Deployment, not a fourth model worker.
    if (labels.get("app")=="axion-agent-studio" and labels.get("component")=="manager"
            and pod.get("spec",{}).get("serviceAccountName")=="studio-manager"):
        return "manager"
    return None


def _monitored_pods(pods):
    items=[pod for pod in pods.get("items",[]) if isinstance(pod,dict)
           and not pod.get("metadata",{}).get("deletionTimestamp")]
    managers=[pod for pod in items if _telemetry_role(pod)=="manager"]
    # A single-manager chart cannot silently combine multiple replicas.
    return [pod for pod in items if _telemetry_role(pod) in WORKER_ROLES
            or (_telemetry_role(pod)=="manager" and len(managers)==1)]


def _placement_role(pod):
    role=_telemetry_role(pod)
    if role is not None:
        return role
    labels=pod.get("metadata",{}).get("labels",{})
    spec=pod.get("spec",{})
    if (labels.get("app")=="axion-agent-studio" and labels.get("component")=="validator"
            and spec.get("serviceAccountName")=="sandbox-worker"
            and any(container.get("name")=="validator" for container in spec.get("containers",[]))):
        return "validator"
    return None


def _placement_pods(pods):
    items=[pod for pod in pods.get("items",[]) if isinstance(pod,dict)
           and not pod.get("metadata",{}).get("deletionTimestamp")]
    roles=[_placement_role(pod) for pod in items]
    # During replacement, do not silently select one of two role identities.
    return [pod for pod,role in zip(items,roles) if role is not None and roles.count(role)==1]


def quantity(value: str, cpu=False):
    units={"n":1e-9,"u":1e-6,"m":1e-3} if cpu else {"Ki":1024,"Mi":1024**2,"Gi":1024**3,"Ti":1024**4,"K":1000,"M":1000**2,"G":1000**3}
    for suffix,factor in units.items():
        if value.endswith(suffix):
            return float(value[:-len(suffix)])*factor
    return float(value)


def _complete_total(expected, containers, resource):
    """Return a complete measured sum, never a partial sum or an absent zero."""
    if not expected or not isinstance(containers, list) or not containers:
        return None
    observed={}
    for container in containers:
        if not isinstance(container, dict):
            return None
        name=container.get("name")
        if not isinstance(name, str) or not name or name in observed:
            return None
        observed[name]=container
    if set(observed)!=expected:
        return None
    values=[]
    for container in observed.values():
        usage=container.get("usage")
        raw=usage.get(resource) if isinstance(usage, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            value=quantity(raw, cpu=resource=="cpu")
        except (ValueError, TypeError, AttributeError, OverflowError):
            return None
        if not math.isfinite(value) or value<0:
            return None
        values.append(value)
    total=sum(values)
    return total if math.isfinite(total) else None


def _declared_cpu_limit(spec, expected, containers):
    """Sum bounded active-container limits, never threads or node capacity.

    This is a denominator for comparing measured Pod CPU against its
    declared active-container limits. It is not a measurement or a guarantee
    of dedicated cores. Ordinary init containers and Pod overhead are not in
    that sum. Pod-level CPU limits have different semantics, so fail closed
    until a denominator for that configuration is explicitly supported.
    """
    pod_resources=spec.get("resources")
    pod_limits=pod_resources.get("limits") if isinstance(pod_resources,dict) else None
    if isinstance(pod_limits,dict) and "cpu" in pod_limits:
        return None
    if not expected or not containers:
        return None
    values=[]
    for container in containers:
        resources=container.get("resources")
        limits=resources.get("limits") if isinstance(resources,dict) else None
        raw=limits.get("cpu") if isinstance(limits,dict) else None
        if not isinstance(raw,str) or not raw.strip():
            return None
        try:
            value=quantity(raw,cpu=True)
        except (ValueError,TypeError,AttributeError,OverflowError):
            return None
        # Missing or zero limits do not establish a bounded CPU allocation.
        if not math.isfinite(value) or value<=0:
            return None
        values.append(value)
    total=sum(values)
    return total if math.isfinite(total) else None


def _timestamp(value):
    if not isinstance(value,str):
        return None
    try:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError,TypeError,OverflowError):
        return None


def _current_container_metrics(measurement,pod_metadata,now):
    """Fixed-name replacement Pods must not inherit a previous Pod's usage."""
    metadata=measurement.get("metadata",{})
    if "uid" in metadata and metadata["uid"]!=pod_metadata.get("uid"):
        return False
    if "namespace" in metadata and metadata["namespace"]!=pod_metadata.get("namespace"):
        return False
    observed=_timestamp(measurement.get("timestamp"))
    created=_timestamp(pod_metadata.get("creationTimestamp"))
    if observed is None or created is None or observed<created:
        return False
    age=(now-observed).total_seconds()
    return -POD_METRICS_FUTURE_TOLERANCE_SECONDS<=age<=CONTAINER_METRICS_MAX_AGE_SECONDS


def samples_from_responses(pods, metrics, now=None):
    """Join measurements to declared manager/worker Pods; keep missing values null."""
    now=now or datetime.now(timezone.utc)
    by_name={p.get("metadata",{}).get("name"):p for p in metrics.get("items",[]) if isinstance(p,dict)}
    samples=[]
    for pod in _monitored_pods(pods):
        metadata=pod.get("metadata",{})
        role=_telemetry_role(pod)
        spec=pod.get("spec",{})
        declared=list(spec.get("containers",[]))
        # Native sidecars remain active after init; ordinary model-copy init
        # containers are not part of steady-state worker/model utilization.
        declared.extend(c for c in spec.get("initContainers",[]) if c.get("restartPolicy")=="Always")
        declared.extend(spec.get("ephemeralContainers",[]))
        names=[c.get("name") for c in declared]
        expected=set(names) if all(isinstance(n,str) and n for n in names) and len(names)==len(set(names)) else set()
        # Manager CPU is coordination; only workers require a local model pair.
        required={"manager"} if role=="manager" else {"worker","llama"}
        if not required.issubset(expected):
            expected=set()
        cpu_limit=_declared_cpu_limit(spec,expected,declared)
        measurement=by_name.get(metadata.get("name"),{})
        containers=measurement.get("containers")
        current=_current_container_metrics(measurement,metadata,now)
        cpu=_complete_total(expected,containers,"cpu") if current else None
        runtime=spec.get("runtimeClassName") or "default"
        # GKE Sandbox does not support container memory metrics, for either
        # gVisor or microVM. A real supported Pod-memory source is still needed.
        # https://docs.cloud.google.com/kubernetes-engine/docs/concepts/sandbox-pods#incompatible_features
        standard_runtime=runtime in {"default","runc","containerd"}
        memory=_complete_total(expected,containers,"memory") if standard_runtime and current else None
        samples.append({
            "timestamp":measurement.get("timestamp") if current else "",
            "role":role,"pod":metadata.get("name"),"runtime":runtime,
            "namespace":metadata.get("namespace"),"pod_uid":metadata.get("uid"),"node":spec.get("nodeName"),
            "cpu_cores":cpu,"memory_bytes":memory,"window":measurement.get("window"),
            "cpu_limit_cores":cpu_limit,
            "cpu_limit_source":"declared_active_container_limits" if cpu_limit is not None else "unavailable",
            "cpu_source":"complete_container_sum" if cpu is not None else "unavailable",
            "cpu_timestamp":measurement.get("timestamp") if cpu is not None else None,
            "memory_timestamp":measurement.get("timestamp") if memory is not None else None,
            "machine_family":None,"machine_type":None,"architecture":None,
            "placement_source":"unavailable","placement_observed_at":None,"node_uid":None,
            "memory_source":"complete_container_sum" if memory is not None else "unavailable" if standard_runtime else "supported_pod_memory_source_pending",
            "containers_expected":sorted(expected),
            "containers_observed":[c.get("name") for c in containers if isinstance(c,dict)] if isinstance(containers,list) else [],
        })
    return samples


def node_metrics_allowlist(raw):
    """An absent allowlist makes no kubelet requests; malformed config fails closed."""
    if not raw:
        return {}
    if len(raw)>16384:
        raise ValueError("Node metrics allowlist is too large")
    def unique_pairs(pairs):
        result={}
        for name,address in pairs:
            if name in result:
                raise ValueError("Duplicate node name")
            result[name]=address
        return result
    entries=json.loads(raw,object_pairs_hook=unique_pairs)
    if not isinstance(entries,dict) or len(entries)>32:
        raise ValueError("Node metrics allowlist must be a bounded node-to-IP object")
    result={}
    for name,address in entries.items():
        if not isinstance(name,str) or len(name)>253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?",name):
            raise ValueError("Invalid allowlisted node name")
        if not isinstance(address,str):
            raise ValueError("Node metrics destination must be an IPv4 address")
        ip=ipaddress.IPv4Address(address)
        if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("Node metrics destination must be a private node address")
        if address in result.values():
            raise ValueError("An address cannot identify multiple allowlisted nodes")
        result[name]=str(ip)
    return result


def _pod_identity(pod):
    metadata=pod.get("metadata",{})
    return (metadata.get("namespace"),metadata.get("name"),metadata.get("uid"))


def _fresh_pod_stat(stats,key,now):
    """Summary fields are optional uint64 measurements with their own time."""
    if not isinstance(stats,dict):
        return None
    value=stats.get(key)
    if type(value) is not int or not 0<=value<=2**64-1:
        return None
    stamp=stats.get("time")
    observed=_timestamp(stamp)
    if observed is None:
        return None
    age=(now-observed).total_seconds()
    if not -POD_METRICS_FUTURE_TOLERANCE_SECONDS<=age<=POD_METRICS_MAX_AGE_SECONDS:
        return None
    return value,stamp


def _resources_from_summary(summary,node_name,expected,now):
    """Accept independent Pod CPU rate and working set for current identity."""
    if not isinstance(summary,dict) or not isinstance(summary.get("node"),dict) or summary["node"].get("nodeName")!=node_name:
        return {}
    if not isinstance(summary.get("pods"),list):
        return {}
    found={}
    duplicates=set()
    for entry in summary["pods"]:
        if not isinstance(entry,dict) or not isinstance(entry.get("podRef"),dict):
            continue
        reference=entry["podRef"]
        identity=(reference.get("namespace"),reference.get("name"),reference.get("uid"))
        if not all(isinstance(value,str) and value for value in identity) or identity not in expected:
            continue
        if identity in found:
            duplicates.add(identity)
        # Mark even an invalid first occurrence so conflicting duplicate
        # records cannot later supply an apparently valid replacement.
        reading={}
        memory=_fresh_pod_stat(entry.get("memory"),"workingSetBytes",now)
        if memory is not None:
            reading.update(memory_bytes=memory[0],memory_timestamp=memory[1],memory_source="kubelet_summary_pod_working_set")
        # usageNanoCores is the Pod CPU rate (nanocores / 1e9 = cores).
        # usageCoreNanoSeconds is cumulative consumption and is never a rate.
        cpu=_fresh_pod_stat(entry.get("cpu"),"usageNanoCores",now)
        if cpu is not None:
            reading.update(cpu_cores=cpu[0]/1e9,cpu_timestamp=cpu[1],cpu_source="kubelet_summary_pod_cpu")
        found[identity]=reading
    return {identity:reading for identity,reading in found.items() if reading and identity not in duplicates}


async def collect_pod_resources(client,headers,pods,namespace,allowlist):
    """Read only explicitly allowlisted kubelets named by trusted Pod inventory."""
    targets={}
    for pod in _monitored_pods(pods):
        metadata=pod.get("metadata",{})
        if metadata.get("namespace")!=namespace:
            continue
        identity=_pod_identity(pod)
        if not all(isinstance(value,str) and value for value in identity):
            continue
        node=pod.get("spec",{}).get("nodeName")
        address=pod.get("status",{}).get("hostIP")
        if node not in allowlist or address!=allowlist[node]:
            continue
        targets.setdefault(node,set()).add(identity)

    async def read_node(node,expected):
        try:
            # The complete read has a wall-clock bound as well as httpx's
            # per-I/O timeout, including a response that trickles indefinitely.
            async with asyncio.timeout(NODE_METRICS_TIMEOUT_SECONDS):
                # Never follow a redirect with the service-account token. The
                # caller configures this client with strict mounted-CA validation.
                async with client.stream("GET",f"https://{allowlist[node]}:10250/stats/summary",headers=headers,timeout=NODE_METRICS_TIMEOUT_SECONDS,follow_redirects=False) as response:
                    response.raise_for_status()
                    chunks=[];size=0
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>SUMMARY_MAX_BYTES:
                            return {}
                        chunks.append(chunk)
                summary=json.loads(b"".join(chunks))
                return _resources_from_summary(summary,node,expected,datetime.now(timezone.utc))
        except Exception:
            # TLS, authorization, timeout, schema, and freshness failures
            # remain missing measurements. Never log credentials or raw bodies.
            return {}

    readings={}
    for result in await asyncio.gather(*(read_node(node,expected) for node,expected in targets.items())):
        readings.update(result)
    return readings


def _unavailable_placement():
    return {"machine_family":None,"machine_type":None,"architecture":None,
            "node":None,"node_uid":None,"pod":None,"pod_uid":None,
            "namespace":None,"runtime":None,"source":"unavailable","observed_at":None}


def placement_from_node(pod,node,namespace,allowlist,now=None):
    """Join an assigned Pod to its actual allowlisted Node, never a pool name."""
    if not isinstance(node,dict) or _placement_role(pod) is None:
        return None
    metadata=pod.get("metadata",{})
    node_name=pod.get("spec",{}).get("nodeName")
    address=pod.get("status",{}).get("hostIP")
    if (metadata.get("namespace")!=namespace or node_name not in allowlist
            or address!=allowlist[node_name]
            or not all(isinstance(value,str) and value for value in _pod_identity(pod))):
        return None
    node_metadata=node.get("metadata",{})
    labels=node_metadata.get("labels",{})
    status=node.get("status",{})
    if (node_metadata.get("name")!=node_name or not node_metadata.get("uid")
            or address not in {item.get("address") for item in status.get("addresses",[])
                               if isinstance(item,dict) and item.get("type")=="InternalIP"}):
        return None
    machine_type=labels.get("node.kubernetes.io/instance-type")
    architecture=status.get("nodeInfo",{}).get("architecture")
    family=re.fullmatch(r"([a-z][a-z0-9]*)-(?:[a-z0-9]+-)*[a-z0-9]+",machine_type) if isinstance(machine_type,str) else None
    if (family is None or architecture not in {"arm64","amd64"}
            or labels.get("kubernetes.io/arch")!=architecture):
        return None
    return {"machine_family":family[1],"machine_type":machine_type,"architecture":architecture,
            "node":node_name,"node_uid":node_metadata["uid"],"pod":metadata["name"],
            "pod_uid":metadata["uid"],"namespace":namespace,
            "runtime":pod.get("spec",{}).get("runtimeClassName") or "default","source":"kubernetes_node",
            "observed_at":(now or datetime.now(timezone.utc)).isoformat()}


async def collect_node_placements(client,headers,pods,namespace,allowlist,base,include_validator=False):
    """Read only assigned allowlisted Node objects with name-scoped GET access."""
    candidates=[pod for pod in (_placement_pods(pods) if include_validator else _monitored_pods(pods))
                if pod.get("metadata",{}).get("namespace")==namespace
                and all(_pod_identity(pod))
                and pod.get("spec",{}).get("nodeName") in allowlist
                and pod.get("status",{}).get("hostIP")==allowlist[pod["spec"]["nodeName"]]]
    names=sorted({pod["spec"]["nodeName"] for pod in candidates})
    async def read_node(name):
        try:
            async with asyncio.timeout(NODE_METRICS_TIMEOUT_SECONDS):
                response=await client.get(base+f"/api/v1/nodes/{name}",headers=headers,
                                          timeout=NODE_METRICS_TIMEOUT_SECONDS,follow_redirects=False)
                response.raise_for_status()
                return name,response.json()
        except Exception:
            return name,None
    nodes=dict(await asyncio.gather(*(read_node(name) for name in names)))
    result={}
    for pod in candidates:
        placement=placement_from_node(pod,nodes[pod["spec"]["nodeName"]],namespace,allowlist)
        if placement is not None:
            result[_pod_identity(pod)]=placement
    return result


async def collect_placement():
    """Current config placement; never written back into historical run samples."""
    result={role:_unavailable_placement() for role in ("manager","backend","frontend","qa","validator")}
    namespace=os.getenv("STUDIO_NAMESPACE","axion-agent-studio")
    token_path=Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    if not os.getenv("KUBERNETES_SERVICE_HOST") or not token_path.exists():
        return result
    try:
        allowlist=node_metrics_allowlist(os.getenv("STUDIO_NODE_METRICS_ADDRESSES"))
        if not allowlist:
            return result
        base="https://"+os.environ["KUBERNETES_SERVICE_HOST"]+":"+os.getenv("KUBERNETES_SERVICE_PORT_HTTPS","443")
        async with httpx.AsyncClient(verify=CA_FILE,timeout=8,follow_redirects=False,trust_env=False) as client:
            headers={"Authorization":"Bearer "+token_path.read_text().strip()}
            response=await client.get(base+f"/api/v1/namespaces/{namespace}/pods",headers=headers)
            response.raise_for_status()
            inventory=response.json()
            placements=await collect_node_placements(client,headers,inventory,namespace,allowlist,base,include_validator=True)
            for pod in _placement_pods(inventory):
                placement=placements.get(_pod_identity(pod))
                if placement is not None:
                    result[_placement_role(pod)]=placement
    except Exception:
        pass
    return result


async def collect_metrics():
    namespace=os.getenv("STUDIO_NAMESPACE","axion-agent-studio")
    token_path=Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    if not os.getenv("KUBERNETES_SERVICE_HOST") or not token_path.exists():
        return {"source":"unavailable","samples":[],"note":"Live CPU/memory metrics require the manager running in GKE with metrics-reader permission. No values are simulated."}
    base="https://"+os.environ["KUBERNETES_SERVICE_HOST"]+":"+os.getenv("KUBERNETES_SERVICE_PORT_HTTPS","443")
    try:
        async with httpx.AsyncClient(verify=CA_FILE,timeout=8,follow_redirects=False,trust_env=False) as client:
            headers={"Authorization":"Bearer "+token_path.read_text().strip()}
            pods=await client.get(base+f"/api/v1/namespaces/{namespace}/pods",headers=headers)
            pods.raise_for_status()
            inventory=pods.json()
            metrics_note=""
            try:
                metrics=await client.get(base+f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods",headers=headers)
                metrics.raise_for_status()
                observed=metrics.json()
            except Exception:
                observed={"items":[]}
                metrics_note=" Container metrics API is unavailable; CPU requires a validated whole-Pod reading from the kubelet Summary API."
            samples=samples_from_responses(inventory,observed)
            resource_note=""
            try:
                allowlist=node_metrics_allowlist(os.getenv("STUDIO_NODE_METRICS_ADDRESSES"))
            except (ValueError,TypeError):
                allowlist={}
                resource_note=" Node-resource collection is disabled because its explicit allowlist is invalid."
            if allowlist:
                try:
                    placements=await collect_node_placements(client,headers,inventory,namespace,allowlist,base)
                except Exception:
                    placements={}
                for sample in samples:
                    placement=placements.get((sample["namespace"],sample["pod"],sample["pod_uid"]))
                    if placement:
                        sample.update({key:placement[key] for key in ("machine_family","machine_type","architecture","node_uid")})
                        sample.update(placement_source=placement["source"],placement_observed_at=placement["observed_at"])
                try:
                    readings=await collect_pod_resources(client,headers,inventory,namespace,allowlist)
                except Exception:
                    # Optional Pod-resource collection never hides independently
                    # valid CPU metrics, even if an unexpected schema fails.
                    readings={}
                for sample in samples:
                    reading=readings.get((sample["namespace"],sample["pod"],sample["pod_uid"]))
                    if reading:
                        if "memory_bytes" in reading:
                            sample.update({key:reading[key] for key in ("memory_bytes","memory_timestamp","memory_source")})
                        if sample["cpu_cores"] is None and "cpu_cores" in reading:
                            sample.update({key:reading[key] for key in ("cpu_cores","cpu_timestamp","cpu_source")})
                            # An incomplete container-metrics window does not
                            # describe the Summary API's separate CPU rate.
                            sample["window"]=None
                        sample["timestamp"]=sample.get("cpu_timestamp") or sample.get("memory_timestamp") or sample["timestamp"]
                resource_note=" Direct kubelet Pod-resource readings require an allowlisted node and matching current Pod identity; CPU and memory are independently checked for freshness. Missing, stale, or failed readings remain unavailable."
        note="CPU uses fresh complete-container metrics when every active container is observed at or after current Pod creation. Manager samples measure coordination, not hosted Gemini inference; worker samples include the API and local model. If that complete sum is unavailable, a fresh identity-verified whole-Pod CPU rate from the kubelet Summary API may be used. Missing measurements remain unavailable."
        if any(sample["cpu_source"]=="complete_container_sum" for sample in samples):
            note+=" Complete-container CPU is averaged over its reported metrics window."
        if any(sample["cpu_source"]=="kubelet_summary_pod_cpu" for sample in samples):
            note+=" Whole-Pod CPU is kubelet usageNanoCores divided by one billion, including Pod overhead; its own timestamp is retained and its averaging window is not reported."
        if any(sample["cpu_limit_cores"] is not None for sample in samples):
            note+=" CPU-limit denominators sum the current Pod's declared active-container limits, including native sidecars, only when every active container has a valid positive limit. They exclude ordinary init containers and Pod overhead and do not represent dedicated cores, model threads, or node capacity. Whole-Pod usage may include overhead outside that denominator."
        if any(sample["memory_source"]=="supported_pod_memory_source_pending" for sample in samples):
            note+=" Sandbox container-memory metrics are unsupported; RAM remains unavailable pending a validated supported Pod-memory source. Custom runtimes also require a verified memory source."
        if any(sample["memory_source"]=="complete_container_sum" for sample in samples):
            note+=" Standard-runtime memory is a complete container-memory sum, not total Pod memory including overhead."
        if any(sample["memory_source"]=="kubelet_summary_pod_working_set" for sample in samples):
            note+=" RAM is fresh Pod-cgroup working-set memory from the TLS-verified kubelet Summary API, including Pod overhead; its own timestamp is retained."
        note+=metrics_note+resource_note
        return {"source":"kubernetes","samples":samples,"note":note}
    except Exception:
        return {"source":"unavailable","samples":[],"note":"Kubernetes metrics are not available yet. Check metrics API and read permissions."}


def current_metrics_view(snapshot,collected_at,cache_age_seconds,now=None):
    """Recheck each measurement's age without rewriting its original identity/time."""
    result=copy.deepcopy(snapshot)
    now=now or datetime.now(timezone.utc)
    for sample in result.get("samples",[]):
        for kind,value_key,max_age in (("cpu","cpu_cores",CONTAINER_METRICS_MAX_AGE_SECONDS),
                                       ("memory","memory_bytes",POD_METRICS_MAX_AGE_SECONDS)):
            observed=_timestamp(sample.get(kind+"_timestamp"))
            age=(now-observed).total_seconds() if observed is not None else None
            value=sample.get(value_key)
            fresh=(type(value) in {int,float} and math.isfinite(value) and value>=0
                   and sample.get(kind+"_source") not in {None,"unavailable","supported_pod_memory_source_pending"}
                   and age is not None and -POD_METRICS_FUTURE_TOLERANCE_SECONDS<=age<=max_age)
            sample[kind+"_age_seconds"]=round(age,3) if age is not None else None
            sample[kind+"_fresh"]=fresh
            if not fresh:
                sample[value_key]=None
                sample[kind+"_source"]="unavailable"
    result.update(scope="current",collected_at=collected_at,
                  cache_age_seconds=round(max(0,cache_age_seconds),3),
                  poll_after_seconds=CURRENT_METRICS_CACHE_SECONDS,
                  max_sample_age_seconds=CONTAINER_METRICS_MAX_AGE_SECONDS)
    return result


class CurrentMetricsCache:
    """One bounded snapshot and one in-flight read per manager process.

    An expired snapshot is never served as a fallback. A failed refresh is
    briefly cached as unavailable, preventing a polling burst from retrying
    the cluster immediately. Request cancellation does not cancel a shared
    collection; the app lifespan owns that task and cancels it on shutdown.
    """
    def __init__(self,collector,ttl=CURRENT_METRICS_CACHE_SECONDS,
                 timeout=CURRENT_METRICS_TIMEOUT_SECONDS,monotonic=time.monotonic,utc_now=None):
        self.collector=collector
        self.ttl=ttl
        self.timeout=timeout
        self.monotonic=monotonic
        self.utc_now=utc_now or (lambda:datetime.now(timezone.utc))
        self._snapshot=None
        self._collected_at=None
        self._completed_at=None
        self._task=None
        self._closed=False

    async def _refresh(self):
        try:
            async with asyncio.timeout(self.timeout):
                result=await self.collector()
        except Exception:
            result={"source":"unavailable","samples":[],
                    "note":"Current metrics collection failed or timed out. No saved run readings are substituted."}
        self._snapshot=copy.deepcopy(result)
        self._collected_at=self.utc_now().isoformat()
        self._completed_at=self.monotonic()

    async def get(self):
        if self._closed:
            raise RuntimeError("Current metrics collector is closed")
        if self._snapshot is None or self.monotonic()-self._completed_at>=self.ttl:
            # No await between checking and assigning: all callers in this
            # event loop share the same task without a cross-loop global lock.
            if self._task is None or self._task.done():
                self._task=asyncio.create_task(self._refresh())
            await asyncio.shield(self._task)
        return current_metrics_view(self._snapshot,self._collected_at,
                                    self.monotonic()-self._completed_at,self.utc_now())

    async def close(self):
        self._closed=True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task,return_exceptions=True)
