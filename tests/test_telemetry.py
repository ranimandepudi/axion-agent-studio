"""Metric correctness checks with Kubernetes response fixtures, never cloud calls."""
import copy
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from studio import telemetry


def pod(role="backend", runtime="gvisor"):
    return {
        "metadata":{"name":f"worker-{role}","namespace":"axion-agent-studio","uid":f"uid-{role}","creationTimestamp":(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(),"labels":{"studio.axion/role":role}},
        "spec":{"runtimeClassName":runtime,"nodeName":"demo-node","containers":[
            {"name":"worker","resources":{"limits":{"cpu":"500m"}}},
            {"name":"llama","resources":{"limits":{"cpu":"6"}}},
        ]},
        "status":{"hostIP":"10.42.0.4"},
    }


def metric(role="backend"):
    return {
        "metadata":{"name":f"worker-{role}"},"timestamp":datetime.now(timezone.utc).isoformat(),"window":"30s",
        "containers":[
            {"name":"worker","usage":{"cpu":"20m","memory":"64Mi"}},
            {"name":"llama","usage":{"cpu":"2500m","memory":"2Gi"}},
        ],
    }


def manager_pod():
    definition=pod(runtime=None)
    definition["metadata"].update(name="studio-manager-example",uid="uid-manager",
        labels={"app":"axion-agent-studio","component":"manager"})
    definition["spec"].update(serviceAccountName="studio-manager",nodeName="manager-node",
        containers=[{"name":"manager","resources":{"limits":{"cpu":"1"}}}],
        initContainers=[{"name":"restore-recorded-runs","resources":{"limits":{"cpu":"100m"}}}])
    definition["status"]["hostIP"]="10.42.0.3"
    return definition


def manager_metric():
    measurement=metric()
    measurement["metadata"].update(name="studio-manager-example",uid="uid-manager")
    measurement["containers"]=[{"name":"manager","usage":{"cpu":"125m","memory":"96Mi"}}]
    return measurement


def validator_pod():
    definition=pod(runtime="gvisor")
    definition["metadata"].update(name="validator",uid="uid-validator",
        labels={"app":"axion-agent-studio","component":"validator"})
    definition["spec"].update(serviceAccountName="sandbox-worker",nodeName="validator-node",
        containers=[{"name":"validator","resources":{"limits":{"cpu":"1"}}}])
    definition["status"]["hostIP"]="10.42.0.5"
    return definition


def node_for(definition, machine_type="c4a-standard-16"):
    return {"metadata":{"name":definition["spec"]["nodeName"],"uid":"node-uid-"+definition["spec"]["nodeName"],
                        "labels":{"node.kubernetes.io/instance-type":machine_type,"kubernetes.io/arch":"arm64"}},
            "status":{"addresses":[{"type":"InternalIP","address":definition["status"]["hostIP"]}],
                      "nodeInfo":{"architecture":"arm64"}}}


def sample(p=None,m=None):
    return telemetry.samples_from_responses({"items":[p or pod()]},{"items":[m or metric()]})[0]


@pytest.mark.parametrize("runtime",["gvisor","microvm","custom-sandbox"])
def test_sandbox_memory_is_never_inferred_from_containers(runtime):
    result=sample(p=pod(runtime=runtime))
    assert result["cpu_cores"]==pytest.approx(2.52)
    assert result["cpu_source"]=="complete_container_sum"
    assert result["memory_bytes"] is None
    assert result["memory_source"]=="supported_pod_memory_source_pending"
    assert result["containers_expected"]==["llama","worker"]


@pytest.mark.parametrize("runtime",[None,"runc","containerd"])
def test_standard_runtime_retains_complete_container_memory_sum(runtime):
    result=sample(p=pod(runtime=runtime))
    assert result["memory_bytes"]==64*1024**2+2*1024**3
    assert result["memory_source"]=="complete_container_sum"
    assert result["memory_timestamp"]==result["cpu_timestamp"]==result["timestamp"]


@pytest.mark.parametrize("containers",[None,[],[metric()["containers"][0]],[metric()["containers"][1]]])
def test_missing_or_partial_container_metrics_do_not_become_zero(containers):
    measurement=metric();measurement["containers"]=containers
    result=sample(m=measurement)
    assert result["cpu_cores"] is None
    assert result["memory_bytes"] is None


@pytest.mark.parametrize("raw",[None,"","NaN","Infinity","-1m","bad",12,True])
def test_invalid_model_cpu_cannot_be_replaced_by_worker_cpu(raw):
    measurement=metric();measurement["containers"][1]["usage"]["cpu"]=raw
    assert sample(m=measurement)["cpu_cores"] is None


def test_observed_zero_is_distinct_from_absent_measurement():
    measurement=metric()
    for container in measurement["containers"]:
        container["usage"]["cpu"]="0"
    assert sample(m=measurement)["cpu_cores"]==0


def test_unknown_or_duplicate_container_prevents_complete_total():
    measurement=metric();measurement["containers"].append(copy.deepcopy(measurement["containers"][0]))
    assert sample(m=measurement)["cpu_cores"] is None
    measurement["containers"][-1]["name"]="unidentified"
    assert sample(m=measurement)["cpu_cores"] is None


def test_missing_model_declaration_does_not_prove_inference_usage():
    definition=pod();definition["spec"]["containers"]=[{"name":"worker"}]
    measurement=metric();measurement["containers"]=measurement["containers"][:1]
    assert sample(p=definition,m=measurement)["cpu_cores"] is None


def test_incomplete_memory_does_not_discard_complete_cpu_on_standard_runtime():
    measurement=metric();del measurement["containers"][1]["usage"]["memory"]
    result=sample(p=pod(runtime=None),m=measurement)
    assert result["cpu_cores"]==pytest.approx(2.52)
    assert result["memory_bytes"] is None


def test_missing_role_measurements_stay_null_without_hiding_other_roles():
    result=telemetry.samples_from_responses({"items":[pod(),pod("qa"),pod("manager")]},{"items":[metric()]})
    assert [s["role"] for s in result]==["backend","qa"]
    assert result[0]["cpu_cores"]==pytest.approx(2.52)
    assert result[1]["cpu_cores"] is None and result[1]["memory_bytes"] is None
    assert result[1]["timestamp"]==""


def test_model_copy_init_is_excluded_but_running_native_sidecar_is_required():
    definition=pod();definition["spec"]["initContainers"]=[{"name":"model-copy"}]
    assert sample(p=definition)["cpu_cores"]==pytest.approx(2.52)
    definition["spec"]["initContainers"].append({"name":"observer","restartPolicy":"Always"})
    assert sample(p=definition)["cpu_cores"] is None


def test_cpu_limit_is_complete_active_container_allocation_not_model_threads():
    definition=pod()
    definition["spec"]["containers"][1]["args"]=["--threads","4"]
    result=sample(p=definition)
    assert result["cpu_limit_cores"]==6.5
    assert result["cpu_limit_source"]=="declared_active_container_limits"
    assert result["cpu_cores"]/result["cpu_limit_cores"]*100==pytest.approx(38.76923077)


@pytest.mark.parametrize("raw",[None,"","NaN","Infinity","-1m","0","bad",12,True,"1e309"])
def test_missing_unbounded_or_invalid_cpu_limit_never_becomes_partial_limit(raw):
    definition=pod()
    definition["spec"]["containers"][1]["resources"]["limits"]["cpu"]=raw
    result=sample(p=definition)
    assert result["cpu_limit_cores"] is None
    assert result["cpu_limit_source"]=="unavailable"
    # Measured CPU remains useful even when its percentage cannot be computed.
    assert result["cpu_cores"]==pytest.approx(2.52)


@pytest.mark.parametrize("resources",[None,{},{"requests":{"cpu":"4"}},{"limits":{}},{"limits":None}])
def test_cpu_requests_cannot_substitute_for_missing_limits(resources):
    definition=pod()
    definition["spec"]["containers"][1]["resources"]=resources
    assert sample(p=definition)["cpu_limit_cores"] is None


def test_cpu_limit_excludes_ordinary_init_and_includes_running_native_sidecars():
    definition=pod()
    definition["spec"]["initContainers"]=[
        {"name":"load-model","resources":{"limits":{"cpu":"20"}}},
        {"name":"observer","restartPolicy":"Always","resources":{"limits":{"cpu":"250m"}}},
    ]
    measurement=metric()
    measurement["containers"].append({"name":"observer","usage":{"cpu":"100m"}})
    result=sample(p=definition,m=measurement)
    assert result["cpu_limit_cores"]==6.75
    assert result["cpu_cores"]==pytest.approx(2.62)
    del definition["spec"]["initContainers"][1]["resources"]
    assert sample(p=definition,m=measurement)["cpu_limit_cores"] is None


def test_unbounded_ephemeral_container_prevents_cpu_percentage():
    definition=pod()
    definition["spec"]["ephemeralContainers"]=[{"name":"debugger"}]
    measurement=metric()
    measurement["containers"].append({"name":"debugger","usage":{"cpu":"100m"}})
    result=sample(p=definition,m=measurement)
    assert result["cpu_cores"]==pytest.approx(2.62)
    assert result["cpu_limit_cores"] is None


def test_partial_or_absent_cpu_usage_does_not_erase_actual_limit():
    definition=pod()
    measurement=metric()
    measurement["containers"]=measurement["containers"][:1]
    result=sample(p=definition,m=measurement)
    assert result["cpu_cores"] is None
    assert result["cpu_limit_cores"]==6.5
    result=telemetry.samples_from_responses({"items":[definition]},{"items":[]})[0]
    assert result["cpu_cores"] is None
    assert result["cpu_limit_cores"]==6.5


def test_duplicate_or_missing_model_declaration_does_not_establish_cpu_limit():
    definition=pod()
    definition["spec"]["containers"].append(copy.deepcopy(definition["spec"]["containers"][0]))
    assert sample(p=definition)["cpu_limit_cores"] is None
    definition["spec"]["containers"]=definition["spec"]["containers"][:1]
    assert sample(p=definition)["cpu_limit_cores"] is None


def test_pod_level_cpu_limit_requires_a_separately_supported_denominator():
    definition=pod()
    definition["spec"]["resources"]={"limits":{"cpu":"4"}}
    result=sample(p=definition)
    assert result["cpu_limit_cores"] is None
    assert result["cpu_cores"]==pytest.approx(2.52)


def test_manager_cpu_uses_its_own_container_limit_and_excludes_restore_init():
    result=sample(p=manager_pod(),m=manager_metric())
    assert result["role"]=="manager"
    assert result["cpu_cores"]==0.125 and result["cpu_limit_cores"]==1
    assert result["cpu_cores"]/result["cpu_limit_cores"]*100==12.5
    assert result["containers_expected"]==["manager"]
    assert result["memory_bytes"]==96*1024**2
    assert result["machine_family"] is None and result["placement_source"]=="unavailable"


@pytest.mark.parametrize("case",["missing","stale","old-uid","missing-limit"])
def test_manager_missing_cpu_or_limit_is_never_inferred_from_workers(case):
    definition=manager_pod();measurement=manager_metric()
    if case=="missing": measurement["containers"]=[]
    elif case=="stale": measurement["timestamp"]=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
    elif case=="old-uid": measurement["metadata"]["uid"]="previous-manager"
    elif case=="missing-limit": definition["spec"]["containers"][0]["resources"]={}
    results=telemetry.samples_from_responses({"items":[definition,pod()]},{"items":[measurement,metric()]})
    manager,worker=results
    assert worker["cpu_cores"]==pytest.approx(2.52)
    if case=="missing-limit":
        assert manager["cpu_cores"]==0.125 and manager["cpu_limit_cores"] is None
    else:
        assert manager["cpu_cores"] is None and manager["cpu_limit_cores"]==1


def test_manager_identity_is_not_a_worker_role_label_or_an_ambiguous_replica():
    assert telemetry.samples_from_responses({"items":[pod("manager")]},{"items":[metric("manager")]})==[]
    first=manager_pod();second=copy.deepcopy(first)
    second["metadata"].update(name="another-manager",uid="another-uid")
    assert telemetry.samples_from_responses({"items":[first,second,pod()]},{"items":[manager_metric(),metric()]})[0]["role"]=="backend"
    second["metadata"]["deletionTimestamp"]=datetime.now(timezone.utc).isoformat()
    result=telemetry.samples_from_responses({"items":[first,second]},{"items":[manager_metric()]})
    assert len(result)==1 and result[0]["role"]=="manager"


def test_manager_declaration_without_manager_container_cannot_prove_cpu():
    definition=manager_pod();definition["spec"]["containers"][0]["name"]="unexpected"
    measurement=manager_metric();measurement["containers"][0]["name"]="unexpected"
    result=sample(p=definition,m=measurement)
    assert result["cpu_cores"] is None and result["cpu_limit_cores"] is None


def test_node_placement_uses_exact_assignment_and_node_labels():
    definition=manager_pod();node=node_for(definition,"c4a-standard-2")
    placement=telemetry.placement_from_node(definition,node,"axion-agent-studio",{"manager-node":"10.42.0.3"})
    assert placement["machine_family"]=="c4a" and placement["machine_type"]=="c4a-standard-2"
    assert placement["architecture"]=="arm64" and placement["source"]=="kubernetes_node"
    assert placement["pod_uid"]=="uid-manager" and placement["node_uid"]==node["metadata"]["uid"]
    assert placement["observed_at"]


@pytest.mark.parametrize("case",["no-allowlist","node-name","node-ip","pod-ip","namespace","no-pod-uid","no-node-uid","no-type","bad-type","no-arch","arch-mismatch"])
def test_unverified_node_placement_stays_unavailable(case):
    definition=manager_pod();node=node_for(definition,"c4a-standard-2");allowlist={"manager-node":"10.42.0.3"}
    if case=="no-allowlist": allowlist={}
    elif case=="node-name": node["metadata"]["name"]="different-node"
    elif case=="node-ip": node["status"]["addresses"][0]["address"]="10.42.0.5"
    elif case=="pod-ip": definition["status"]["hostIP"]="10.42.0.5"
    elif case=="namespace": definition["metadata"]["namespace"]="different-namespace"
    elif case=="no-pod-uid": definition["metadata"].pop("uid")
    elif case=="no-node-uid": node["metadata"].pop("uid")
    elif case=="no-type": node["metadata"]["labels"].pop("node.kubernetes.io/instance-type")
    elif case=="bad-type": node["metadata"]["labels"]["node.kubernetes.io/instance-type"]=True
    elif case=="no-arch": node["status"]["nodeInfo"].pop("architecture")
    elif case=="arch-mismatch": node["metadata"]["labels"]["kubernetes.io/arch"]="amd64"
    assert telemetry.placement_from_node(definition,node,"axion-agent-studio",allowlist) is None


def test_container_limit_denominator_does_not_absorb_pod_overhead_or_clamp_usage():
    definition=pod()
    definition["spec"]["overhead"]={"cpu":"250m"}
    measurement=metric()
    measurement["containers"][1]["usage"]["cpu"]="7000m"
    result=sample(p=definition,m=measurement)
    assert result["cpu_limit_cores"]==6.5
    assert result["cpu_cores"]==pytest.approx(7.02)


@pytest.mark.parametrize("age,accepted",[(0,True),(60,True),(60.01,False),(-5,True),(-5.01,False)])
def test_container_metrics_require_fresh_timestamp(age,accepted):
    now=datetime(2026,10,15,16,tzinfo=timezone.utc)
    definition=pod(runtime=None);definition["metadata"]["creationTimestamp"]=(now-timedelta(minutes=2)).isoformat()
    measurement=metric();measurement["timestamp"]=(now-timedelta(seconds=age)).isoformat()
    reading=telemetry.samples_from_responses({"items":[definition]},{"items":[measurement]},now)[0]
    assert (reading["cpu_cores"] is not None) is accepted
    assert (reading["memory_bytes"] is not None) is accepted
    assert reading["timestamp"]==(measurement["timestamp"] if accepted else "")


@pytest.mark.parametrize("field,stamp",[("measurement",None),("measurement","bad"),("measurement","2026-10-15T16:00:00"),("creation",None),("creation","bad"),("creation","2026-10-15T16:00:00")])
def test_missing_or_invalid_current_pod_timestamps_remain_unavailable(field,stamp):
    definition=pod();measurement=metric()
    if field=="measurement": measurement["timestamp"]=stamp
    else: definition["metadata"]["creationTimestamp"]=stamp
    assert sample(p=definition,m=measurement)["cpu_cores"] is None


def test_fixed_name_replacement_cannot_inherit_old_cpu_even_if_fresh():
    now=datetime.now(timezone.utc)
    definition=pod();definition["metadata"]["creationTimestamp"]=(now-timedelta(seconds=3)).isoformat()
    measurement=metric();measurement["timestamp"]=(now-timedelta(seconds=5)).isoformat()
    assert sample(p=definition,m=measurement)["cpu_cores"] is None
    measurement["timestamp"]=definition["metadata"]["creationTimestamp"]
    assert sample(p=definition,m=measurement)["cpu_cores"]==pytest.approx(2.52)


@pytest.mark.parametrize("field,value",[("uid","old-pod-uid"),("uid",None),("uid",""),("namespace","another-namespace")])
def test_metrics_identity_must_match_when_provided(field,value):
    measurement=metric();measurement["metadata"][field]=value
    assert sample(m=measurement)["cpu_cores"] is None


def test_matching_metrics_identity_is_accepted():
    measurement=metric();measurement["metadata"].update({"uid":"uid-backend","namespace":"axion-agent-studio"})
    assert sample(m=measurement)["cpu_cores"]==pytest.approx(2.52)


@pytest.mark.asyncio
async def test_collector_retains_cpu_and_explains_unsupported_memory(monkeypatch):
    monkeypatch.delenv("STUDIO_NODE_METRICS_ADDRESSES",raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST","127.0.0.1")
    monkeypatch.setattr(telemetry.Path,"exists",lambda self:True)
    monkeypatch.setattr(telemetry.Path,"read_text",lambda self:"fixture-token")
    real_client=httpx.AsyncClient
    def handler(request):
        body={"items":[metric()]} if "/apis/metrics.k8s.io/" in request.url.path else {"items":[pod()]}
        return httpx.Response(200,json=body)
    monkeypatch.setattr(telemetry.httpx,"AsyncClient",lambda **kwargs:real_client(transport=httpx.MockTransport(handler)))
    result=await telemetry.collect_metrics()
    assert result["source"]=="kubernetes"
    assert result["samples"][0]["cpu_cores"]==pytest.approx(2.52)
    assert result["samples"][0]["memory_bytes"] is None
    assert result["samples"][0]["cpu_limit_cores"]==6.5
    assert "declared active-container limits" in result["note"]
    assert "do not represent dedicated cores" in result["note"]
    assert "RAM remains unavailable" in result["note"]
    assert "supported Pod-memory source" in result["note"]


def summary(p=None, now=None, memory=67686400):
    definition=p or pod()
    metadata=definition["metadata"]
    return {
        "node":{"nodeName":definition["spec"]["nodeName"]},
        "pods":[{
            "podRef":{key:metadata[key] for key in ("namespace","name","uid")},
            "memory":{"time":(now or datetime.now(timezone.utc)).isoformat(),"workingSetBytes":memory},
            # This deliberately disagrees with Pod memory. Only the Pod
            # working set, including overhead, is the supported measurement.
            "containers":[{"name":"llama","memory":{"workingSetBytes":999999999}}],
        }],
    }


def parsed_summary(body, now=None):
    return telemetry._resources_from_summary(body,"demo-node",{("axion-agent-studio","worker-backend","uid-backend")},now or datetime.now(timezone.utc))


def test_node_allowlist_is_opt_in_and_uses_explicit_private_ip():
    assert telemetry.node_metrics_allowlist(None)=={}
    assert telemetry.node_metrics_allowlist("")=={}
    assert telemetry.node_metrics_allowlist("{}")=={}
    assert telemetry.node_metrics_allowlist('{"demo-node":"10.42.0.4"}')=={"demo-node":"10.42.0.4"}


@pytest.mark.parametrize("raw",[
    "not-json", "[]", "null", '{"demo-node":"https://10.42.0.4"}',
    '{"demo-node":"10.42.0.4/32"}', '{"demo-node":"127.0.0.1"}',
    '{"demo-node":"169.254.169.254"}', '{"demo-node":"8.8.8.8"}',
    '{"demo-node":"::1"}', '{"demo-node":true}', '{"../node":"10.42.0.4"}',
    '{"demo-node":"10.42.0.4","demo-node":"10.42.0.5"}',
    '{"node-a":"10.42.0.4","node-b":"10.42.0.4"}',
])
def test_invalid_node_allowlists_fail_closed(raw):
    with pytest.raises(ValueError):
        telemetry.node_metrics_allowlist(raw)


@pytest.mark.parametrize("value",[0,67686400])
def test_summary_accepts_real_pod_working_set_including_actual_zero(value):
    body=summary(memory=value)
    reading=parsed_summary(body)[("axion-agent-studio","worker-backend","uid-backend")]
    assert reading=={"memory_bytes":value,"memory_timestamp":body["pods"][0]["memory"]["time"],"memory_source":"kubelet_summary_pod_working_set"}


@pytest.mark.parametrize("value",[None,True,-1,1.5,float("nan"),float("inf"),"67686400",2**64])
def test_summary_rejects_absent_or_invalid_memory(value):
    assert parsed_summary(summary(memory=value))=={}


@pytest.mark.parametrize("field,wrong",[("namespace","other-namespace"),("name","other-pod"),("uid","replaced-pod-uid")])
def test_summary_cannot_attribute_another_or_replaced_pod(field,wrong):
    body=summary();body["pods"][0]["podRef"][field]=wrong
    assert parsed_summary(body)=={}


def test_summary_cannot_attribute_another_node_or_duplicate_pod():
    body=summary();body["node"]["nodeName"]="another-node"
    assert parsed_summary(body)=={}
    body=summary();body["pods"].append(copy.deepcopy(body["pods"][0]))
    assert parsed_summary(body)=={}


@pytest.mark.parametrize("age,accepted",[(0,True),(60,True),(60.01,False),(-5,True),(-5.01,False)])
def test_summary_freshness_uses_memory_timestamp(age,accepted):
    now=datetime(2026,10,15,16,tzinfo=timezone.utc)
    body=summary(now=now-timedelta(seconds=age))
    assert bool(parsed_summary(body,now)) is accepted


@pytest.mark.parametrize("stamp",[None,"invalid","2026-10-15T16:00:00",""])
def test_summary_rejects_missing_invalid_or_timezone_free_timestamp(stamp):
    body=summary();body["pods"][0]["memory"]["time"]=stamp
    assert parsed_summary(body)=={}


def test_summary_never_substitutes_container_memory_rss_or_usage():
    body=summary();memory=body["pods"][0]["memory"]
    del memory["workingSetBytes"]
    memory.update({"rssBytes":123,"usageBytes":456})
    assert parsed_summary(body)=={}


def with_cpu(body,value=3900000000,stamp=None):
    body["pods"][0]["cpu"]={"time":stamp or datetime.now(timezone.utc).isoformat(),"usageNanoCores":value,"usageCoreNanoSeconds":999999999999999}
    return body


@pytest.mark.parametrize("value,expected",[(0,0),(3900000000,3.9)])
def test_pod_cpu_rate_uses_nanocores_not_cumulative_nanoseconds(value,expected):
    body=with_cpu(summary(),value)
    reading=next(iter(parsed_summary(body).values()))
    assert reading["cpu_cores"]==expected
    assert reading["cpu_timestamp"]==body["pods"][0]["cpu"]["time"]
    assert reading["cpu_source"]=="kubelet_summary_pod_cpu"
    assert reading["memory_bytes"]==67686400


@pytest.mark.parametrize("value",[None,True,-1,1.5,float("nan"),float("inf"),"3900000000",2**64])
def test_invalid_pod_cpu_never_discards_valid_memory(value):
    reading=next(iter(parsed_summary(with_cpu(summary(),value)).values()))
    assert "cpu_cores" not in reading and reading["memory_bytes"]==67686400


@pytest.mark.parametrize("age,accepted",[(0,True),(60,True),(60.01,False),(-5,True),(-5.01,False)])
def test_pod_cpu_freshness_is_independent_from_memory(age,accepted):
    now=datetime(2026,10,15,16,tzinfo=timezone.utc)
    body=with_cpu(summary(now=now),stamp=(now-timedelta(seconds=age)).isoformat())
    reading=next(iter(parsed_summary(body,now).values()))
    assert ("cpu_cores" in reading) is accepted
    assert reading["memory_bytes"]==67686400


def test_fresh_pod_cpu_survives_stale_memory():
    now=datetime.now(timezone.utc)
    body=with_cpu(summary(now=now-timedelta(minutes=10)),stamp=now.isoformat())
    reading=next(iter(parsed_summary(body,now).values()))
    assert reading["cpu_cores"]==3.9 and "memory_bytes" not in reading


def test_cumulative_cpu_alone_cannot_be_presented_as_a_rate():
    body=with_cpu(summary());del body["pods"][0]["cpu"]["usageNanoCores"]
    reading=next(iter(parsed_summary(body).values()))
    assert "cpu_cores" not in reading and reading["memory_bytes"]==67686400


@pytest.mark.parametrize("field",["namespace","name","uid","node"])
def test_cpu_and_memory_reject_other_pod_or_node_identity(field):
    body=with_cpu(summary())
    if field=="node": body["node"]["nodeName"]="other-node"
    else: body["pods"][0]["podRef"][field]="other-identity"
    assert parsed_summary(body)=={}


def test_duplicate_summary_cannot_supply_either_cpu_or_memory():
    body=with_cpu(summary());body["pods"].append(copy.deepcopy(body["pods"][0]))
    assert parsed_summary(body)=={}


@pytest.mark.asyncio
@pytest.mark.parametrize("change",["empty-allowlist","node","ip","namespace","uid","role"])
async def test_collector_never_requests_unmatched_inventory(change):
    definition=pod();allowlist={"demo-node":"10.42.0.4"};requests=[]
    if change=="empty-allowlist": allowlist={}
    elif change=="node": definition["spec"]["nodeName"]="not-allowlisted"
    elif change=="ip": definition["status"]["hostIP"]="10.42.0.5"
    elif change=="namespace": definition["metadata"]["namespace"]="other-namespace"
    elif change=="uid": definition["metadata"].pop("uid")
    elif change=="role": definition["metadata"]["labels"]["studio.axion/role"]="manager"
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json=summary())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await telemetry.collect_pod_resources(client,{}, {"items":[definition]},"axion-agent-studio",allowlist)
    assert result=={} and requests==[]


@pytest.mark.asyncio
async def test_collector_reads_node_once_and_filters_other_pods():
    definitions=[pod(),pod("frontend")];body=summary();body["pods"].extend(summary(pod("frontend"))["pods"])
    other=summary(pod("qa"))["pods"][0];other["podRef"]["namespace"]="other-namespace";body["pods"].append(other)
    requests=[]
    def handler(request):
        requests.append(request)
        assert str(request.url)=="https://10.42.0.4:10250/stats/summary"
        assert request.headers["Authorization"]=="Bearer fixture-token"
        return httpx.Response(200,json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await telemetry.collect_pod_resources(client,{"Authorization":"Bearer fixture-token"},{"items":definitions},"axion-agent-studio",{"demo-node":"10.42.0.4"})
    assert len(requests)==1
    assert set(result)=={("axion-agent-studio","worker-backend","uid-backend"),("axion-agent-studio","worker-frontend","uid-frontend")}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["tls","timeout","unauthorized","redirect","malformed","oversize"])
async def test_node_failures_remain_missing_without_redirect_or_unbounded_body(monkeypatch,failure):
    requests=[]
    def handler(request):
        requests.append(request)
        if failure=="tls": raise httpx.ConnectError("certificate verify failed",request=request)
        if failure=="timeout": raise httpx.ReadTimeout("timeout",request=request)
        if failure=="unauthorized": return httpx.Response(403)
        if failure=="redirect": return httpx.Response(302,headers={"Location":"https://untrusted.example/stats/summary"})
        if failure=="malformed": return httpx.Response(200,content=b"not-json")
        return httpx.Response(200,content=b"x"*65)
    monkeypatch.setattr(telemetry,"SUMMARY_MAX_BYTES",64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=True) as client:
        result=await telemetry.collect_pod_resources(client,{}, {"items":[pod()]},"axion-agent-studio",{"demo-node":"10.42.0.4"})
    assert result=={} and len(requests)==1


@pytest.mark.asyncio
async def test_complete_node_fetch_has_wall_clock_deadline(monkeypatch):
    cancelled=asyncio.Event()
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield b"{"
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    def handler(request):
        return httpx.Response(200,stream=SlowBody())
    monkeypatch.setattr(telemetry,"NODE_METRICS_TIMEOUT_SECONDS",0.01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result=await asyncio.wait_for(telemetry.collect_pod_resources(client,{}, {"items":[pod()]},"axion-agent-studio",{"demo-node":"10.42.0.4"}),timeout=0.5)
    assert result=={} and cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("case",["valid","kubelet-failed","collector-failed","metrics-failed","metrics-stale","invalid-allowlist","disabled","partial-with-pod-cpu","partial-with-stale-cpu","partial-with-invalid-cpu","complete-and-pod-cpu","missing-api-with-pod-cpu"])
async def test_full_collection_preserves_independent_metrics_and_strict_tls_configuration(monkeypatch,case):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST","127.0.0.1")
    monkeypatch.setenv("STUDIO_NAMESPACE","axion-agent-studio")
    monkeypatch.setenv("STUDIO_NODE_METRICS_ADDRESSES",json.dumps({"demo-node":"10.42.0.4"}))
    if case=="disabled": monkeypatch.delenv("STUDIO_NODE_METRICS_ADDRESSES")
    if case=="invalid-allowlist": monkeypatch.setenv("STUDIO_NODE_METRICS_ADDRESSES",'{"demo-node":"https://malicious.example"}')
    monkeypatch.setattr(telemetry.Path,"exists",lambda self:True)
    monkeypatch.setattr(telemetry.Path,"read_text",lambda self:"fixture-token")
    real_client=httpx.AsyncClient;options=[];requests=[]
    def handler(request):
        requests.append(request)
        if request.url.port==10250:
            body=summary()
            if case in {"partial-with-pod-cpu","complete-and-pod-cpu","missing-api-with-pod-cpu"}: with_cpu(body)
            if case=="partial-with-stale-cpu": with_cpu(body,stamp=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat())
            if case=="partial-with-invalid-cpu": with_cpu(body,None)
            return httpx.Response(403) if case=="kubelet-failed" else httpx.Response(200,json=body)
        if "/apis/metrics.k8s.io/" in request.url.path:
            measurement=metric()
            if case=="metrics-stale": measurement["timestamp"]=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
            if case.startswith("partial-"): measurement["containers"]=measurement["containers"][1:]
            return httpx.Response(503) if case in {"metrics-failed","missing-api-with-pod-cpu"} else httpx.Response(200,json={"items":[measurement]})
        return httpx.Response(200,json={"items":[pod()]})
    def client_factory(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(telemetry.httpx,"AsyncClient",client_factory)
    if case=="collector-failed":
        async def fail(*args): raise RuntimeError("unexpected optional collector failure")
        monkeypatch.setattr(telemetry,"collect_pod_resources",fail)
    result=await telemetry.collect_metrics()
    assert options==[{"verify":telemetry.CA_FILE,"timeout":8,"follow_redirects":False,"trust_env":False}]
    assert result["source"]=="kubernetes"
    reading=result["samples"][0]
    assert reading["cpu_limit_cores"]==6.5
    assert reading["cpu_limit_source"]=="declared_active_container_limits"
    if case in {"partial-with-pod-cpu","missing-api-with-pod-cpu"}:
        assert reading["cpu_cores"]==3.9 and reading["cpu_source"]=="kubelet_summary_pod_cpu"
        assert reading["cpu_timestamp"] and reading["window"] is None
        assert reading["timestamp"]==reading["cpu_timestamp"]
        assert "usageNanoCores" in result["note"]
    elif case in {"metrics-failed","metrics-stale","partial-with-invalid-cpu","partial-with-stale-cpu"}:
        assert reading["cpu_cores"] is None and reading["cpu_source"]=="unavailable"
    else:
        assert reading["cpu_cores"]==pytest.approx(2.52) and reading["cpu_source"]=="complete_container_sum"
        assert reading["window"]=="30s"
    if case in {"valid","metrics-failed","metrics-stale","partial-with-pod-cpu","partial-with-stale-cpu","partial-with-invalid-cpu","complete-and-pod-cpu","missing-api-with-pod-cpu"}:
        assert reading["memory_bytes"]==67686400
        assert reading["memory_source"]=="kubelet_summary_pod_working_set"
        assert reading["memory_timestamp"]
    else:
        assert reading["memory_bytes"] is None
    if case in {"disabled","invalid-allowlist","collector-failed"}:
        assert all(request.url.port!=10250 for request in requests)


def four_role_kubernetes(monkeypatch,case="valid"):
    """Only test fixtures: current manager and three workers on two known nodes."""
    definitions=[manager_pod(),pod(),pod("frontend"),pod("qa")]
    if case=="n4a-migration":definitions.append(validator_pod())
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST","127.0.0.1")
    monkeypatch.setenv("STUDIO_NAMESPACE","axion-agent-studio")
    monkeypatch.setenv("STUDIO_NODE_METRICS_ADDRESSES",json.dumps({definition["spec"]["nodeName"]:definition["status"]["hostIP"] for definition in definitions}))
    monkeypatch.setattr(telemetry.Path,"exists",lambda self:True)
    monkeypatch.setattr(telemetry.Path,"read_text",lambda self:"fixture-token")
    requests=[];options=[];real_client=httpx.AsyncClient
    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"]=="Bearer fixture-token"
        if request.url.port==10250:
            selected=[definition for definition in definitions if definition["status"]["hostIP"]==request.url.host]
            if case=="manager-all-missing" and len(selected)==1:
                return httpx.Response(503)
            body=summary(selected[0])
            body["pods"]=[with_cpu(summary(definition),value=200000000)["pods"][0] for definition in selected]
            return httpx.Response(200,json=body)
        if request.url.path.startswith("/api/v1/nodes/"):
            if case=="node-denied": return httpx.Response(403)
            definition=next(definition for definition in definitions if request.url.path.endswith("/"+definition["spec"]["nodeName"]))
            machine_type="c4a-standard-2" if definition is definitions[0] else "c4a-standard-16"
            if case=="n4a-migration" and definition is definitions[0]:machine_type="n4a-standard-2"
            if definition["spec"]["nodeName"]=="validator-node":machine_type="n4a-standard-8"
            return httpx.Response(200,json=node_for(definition,machine_type))
        if "/apis/metrics.k8s.io/" in request.url.path:
            readings=[metric(role) for role in ("backend","frontend","qa")]
            if case not in {"manager-metrics-missing","manager-all-missing"}:
                readings.append(manager_metric())
            return httpx.Response(200,json={"items":readings})
        assert request.url.path=="/api/v1/namespaces/axion-agent-studio/pods"
        return httpx.Response(200,json={"items":definitions})
    def client_factory(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(telemetry.httpx,"AsyncClient",client_factory)
    return requests,options


@pytest.mark.asyncio
@pytest.mark.parametrize("case",["valid","node-denied","manager-metrics-missing","manager-all-missing"])
async def test_four_role_collection_keeps_manager_cpu_placement_and_missing_values_distinct(monkeypatch,case):
    requests,options=four_role_kubernetes(monkeypatch,case)
    result=await telemetry.collect_metrics()
    assert result["source"]=="kubernetes"
    readings={row["role"]:row for row in result["samples"]}
    assert set(readings)=={"manager","backend","frontend","qa"}
    manager=readings["manager"]
    assert manager["cpu_limit_cores"]==1 and manager["containers_expected"]==["manager"]
    if case=="manager-all-missing":
        assert manager["cpu_cores"] is None and manager["cpu_source"]=="unavailable"
    elif case=="manager-metrics-missing":
        assert manager["cpu_cores"]==0.2 and manager["cpu_source"]=="kubelet_summary_pod_cpu"
    else:
        assert manager["cpu_cores"]==0.125 and manager["cpu_source"]=="complete_container_sum"
    for role in ("backend","frontend","qa"):
        assert readings[role]["cpu_cores"]==pytest.approx(2.52)
        assert readings[role]["cpu_limit_cores"]==6.5
    if case=="node-denied":
        assert all(row["machine_family"] is None and row["placement_source"]=="unavailable" for row in readings.values())
    else:
        assert all(row["machine_family"]=="c4a" and row["architecture"]=="arm64"
                   and row["placement_source"]=="kubernetes_node" for row in readings.values())
        assert manager["machine_type"]=="c4a-standard-2"
        assert readings["backend"]["machine_type"]=="c4a-standard-16"
    assert options==[{"verify":telemetry.CA_FILE,"timeout":8,"follow_redirects":False,"trust_env":False}]
    node_reads=[request.url.path for request in requests if request.url.path.startswith("/api/v1/nodes")]
    assert sorted(node_reads)==["/api/v1/nodes/demo-node","/api/v1/nodes/manager-node"]
    assert "Manager samples measure coordination, not hosted Gemini inference" in result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case",["valid","node-denied"])
async def test_current_placement_is_available_without_collecting_or_rewriting_run_metrics(monkeypatch,case):
    requests,_=four_role_kubernetes(monkeypatch,case)
    placement=await telemetry.collect_placement()
    assert set(placement)=={"manager","backend","frontend","qa","validator"}
    assert placement["validator"]==telemetry._unavailable_placement()
    assert not any("metrics.k8s.io" in request.url.path or request.url.port==10250 for request in requests)
    manager=placement["manager"]
    if case=="valid":
        assert manager["machine_family"]=="c4a" and manager["machine_type"]=="c4a-standard-2"
        assert manager["architecture"]=="arm64" and manager["source"]=="kubernetes_node"
    else:
        assert manager==telemetry._unavailable_placement()


@pytest.mark.asyncio
async def test_observed_n4a_manager_and_validator_do_not_relabel_c4a_workers_or_add_a_chart_role(monkeypatch):
    requests,_=four_role_kubernetes(monkeypatch,"n4a-migration")
    placement=await telemetry.collect_placement()
    assert placement["manager"]["machine_type"]=="n4a-standard-2"
    assert placement["manager"]["runtime"]=="default"
    assert placement["validator"]["machine_type"]=="n4a-standard-8"
    assert placement["validator"]["runtime"]=="gvisor"
    for role in ("manager","validator"):
        assert placement[role]["machine_family"]=="n4a"
        assert placement[role]["source"]=="kubernetes_node"
        assert placement[role]["pod_uid"]=="uid-"+role
        assert placement[role]["node_uid"]=="node-uid-"+role+"-node"
    for role in ("backend","frontend","qa"):
        assert placement[role]["machine_family"]=="c4a"
    requests.clear()
    metrics=await telemetry.collect_metrics()
    assert {sample["role"] for sample in metrics["samples"]}=={"manager","backend","frontend","qa"}
    assert not any(request.url.path.endswith("/validator-node") or request.url.host=="10.42.0.5" for request in requests)


@pytest.mark.parametrize("case",["wrong-app","wrong-component","wrong-account","missing-container","wrong-ip","wrong-node","wrong-namespace"])
def test_validator_placement_requires_its_own_trusted_pod_and_node_identity(case):
    definition=validator_pod()
    node=node_for(definition,"n4a-standard-8")
    if case=="wrong-app":definition["metadata"]["labels"]["app"]="other"
    if case=="wrong-component":definition["metadata"]["labels"]["component"]="other"
    if case=="wrong-account":definition["spec"]["serviceAccountName"]="other"
    if case=="missing-container":definition["spec"]["containers"]=[]
    if case=="wrong-ip":definition["status"]["hostIP"]="10.42.0.6"
    if case=="wrong-node":node["metadata"]["name"]="other"
    if case=="wrong-namespace":definition["metadata"]["namespace"]="other"
    assert telemetry.placement_from_node(definition,node,"axion-agent-studio",{"validator-node":"10.42.0.5"}) is None


def test_validator_rollout_does_not_choose_between_two_pod_identities():
    old=validator_pod();new=copy.deepcopy(old)
    new["metadata"]["uid"]="replacement-validator"
    new["spec"]["nodeName"]="replacement-node"
    inventory={"items":[old,new,manager_pod()]}
    assert [telemetry._placement_role(pod) for pod in telemetry._placement_pods(inventory)]==["manager"]
    old["metadata"]["deletionTimestamp"]=datetime.now(timezone.utc).isoformat()
    selected=telemetry._placement_pods(inventory)
    assert [pod["metadata"]["uid"] for pod in selected]==["replacement-validator","uid-manager"]
