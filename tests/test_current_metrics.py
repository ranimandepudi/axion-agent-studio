"""Current readings stay separate from saved runs and bounded under polling."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from studio import app as manager
from studio.telemetry import CurrentMetricsCache, current_metrics_view


class Clock:
    def __init__(self):
        self.seconds=0
        self.start=datetime(2026,10,20,12,tzinfo=timezone.utc)

    def monotonic(self):return self.seconds
    def now(self):return self.start+timedelta(seconds=self.seconds)


def snapshot(clock,cpu=0.125,pod_uid="manager-before",family="c4a"):
    stamp=clock.now().isoformat()
    return {"source":"kubernetes","note":"Observed test response","samples":[{
        "role":"manager","namespace":"axion-agent-studio","pod":"manager-pod","pod_uid":pod_uid,
        "node":"assigned-node","node_uid":"node-"+pod_uid,"runtime":"default",
        "timestamp":stamp,"cpu_timestamp":stamp,"memory_timestamp":stamp,
        "cpu_cores":cpu,"cpu_source":"kubelet_summary_pod_cpu","cpu_limit_cores":1,
        "cpu_limit_source":"declared_active_container_limits","memory_bytes":1024,
        "memory_source":"kubelet_summary_pod_working_set","machine_family":family,
        "machine_type":family+"-standard-2","architecture":"arm64",
        "placement_source":"kubernetes_node","placement_observed_at":stamp,
    }]}


@pytest.mark.asyncio
async def test_simultaneous_visitors_share_one_collection_and_receive_independent_copies():
    clock=Clock();started=asyncio.Event();release=asyncio.Event();calls=0
    original=snapshot(clock)
    async def collect():
        nonlocal calls
        calls+=1;started.set()
        await release.wait()
        return original
    cache=CurrentMetricsCache(collect,monotonic=clock.monotonic,utc_now=clock.now)
    readers=[asyncio.create_task(cache.get()) for _ in range(20)]
    await started.wait()
    assert calls==1
    release.set()
    results=await asyncio.gather(*readers)
    assert calls==1 and all(result==results[0] for result in results)
    assert results[0]["scope"]=="current" and results[0]["poll_after_seconds"]==5
    assert results[0]["samples"][0]["cpu_fresh"] is True
    results[0]["samples"][0]["cpu_cores"]=99
    assert (await cache.get())["samples"][0]["cpu_cores"]==0.125
    assert original["samples"][0]["cpu_cores"]==0.125 and "scope" not in original
    await cache.close()


@pytest.mark.asyncio
async def test_expiry_observes_new_pod_node_and_family_instead_of_reusing_previous_identity():
    clock=Clock();calls=0
    async def collect():
        nonlocal calls
        calls+=1
        return snapshot(clock,cpu=0.125 if calls==1 else 0.25,
                        pod_uid="old" if calls==1 else "new",family="c4a" if calls==1 else "n4a")
    cache=CurrentMetricsCache(collect,monotonic=clock.monotonic,utc_now=clock.now)
    before=await cache.get()
    clock.seconds=4.99
    cached=await cache.get()
    assert calls==1 and cached["cache_age_seconds"]==4.99
    assert cached["collected_at"]==before["collected_at"]
    assert cached["samples"][0]["cpu_timestamp"]==before["samples"][0]["cpu_timestamp"]
    clock.seconds=5
    after=await cache.get()
    assert calls==2 and after["cache_age_seconds"]==0
    assert after["samples"][0]["pod_uid"]=="new"
    assert after["samples"][0]["node_uid"]=="node-new"
    assert after["samples"][0]["machine_family"]=="n4a"
    assert after["samples"][0]["cpu_cores"]==0.25
    assert before["samples"][0]["pod_uid"]=="old"
    await cache.close()


@pytest.mark.asyncio
async def test_cached_reading_expires_at_measurement_age_without_erasing_independent_memory():
    clock=Clock();original=snapshot(clock)
    original["samples"][0]["cpu_timestamp"]=(clock.now()-timedelta(seconds=58)).isoformat()
    async def collect():return original
    cache=CurrentMetricsCache(collect,monotonic=clock.monotonic,utc_now=clock.now)
    before=await cache.get()
    assert before["samples"][0]["cpu_fresh"] is True
    clock.seconds=3
    row=(await cache.get())["samples"][0]
    assert row["cpu_cores"] is None and row["cpu_source"]=="unavailable"
    assert row["cpu_fresh"] is False and row["cpu_age_seconds"]==61
    assert row["cpu_timestamp"]==original["samples"][0]["cpu_timestamp"]
    assert row["memory_bytes"]==1024 and row["memory_fresh"] is True
    assert row["pod_uid"]==original["samples"][0]["pod_uid"]
    assert original["samples"][0]["cpu_cores"]==0.125
    await cache.close()


@pytest.mark.parametrize("kind,value_key",[("cpu","cpu_cores"),("memory","memory_bytes")])
@pytest.mark.parametrize("case",["missing-time","naive-time","future-time","stale","missing-value","negative","nan","boolean","missing-source","zero"])
def test_current_freshness_is_explicit_per_resource_and_never_manufactures_a_zero(kind,value_key,case):
    clock=Clock();original=snapshot(clock);row=original["samples"][0]
    if case=="missing-time":row.pop(kind+"_timestamp")
    if case=="naive-time":row[kind+"_timestamp"]="2026-10-20T12:00:00"
    if case=="future-time":row[kind+"_timestamp"]=(clock.now()+timedelta(seconds=6)).isoformat()
    if case=="stale":row[kind+"_timestamp"]=(clock.now()-timedelta(seconds=61)).isoformat()
    if case=="missing-value":row[value_key]=None
    if case=="negative":row[value_key]=-1
    if case=="nan":row[value_key]=float("nan")
    if case=="boolean":row[value_key]=True
    if case=="missing-source":row.pop(kind+"_source")
    if case=="zero":row[value_key]=0
    result=current_metrics_view(original,clock.now().isoformat(),0,clock.now())["samples"][0]
    if case=="zero":
        assert result[value_key]==0 and result[kind+"_fresh"] is True
    else:
        assert result[value_key] is None and result[kind+"_fresh"] is False
        assert result[kind+"_source"]=="unavailable"
    other="memory" if kind=="cpu" else "cpu"
    assert result[other+"_fresh"] is True


@pytest.mark.asyncio
async def test_failed_refresh_replaces_old_values_and_caches_failure_to_bound_retry_load():
    clock=Clock();calls=0
    async def collect():
        nonlocal calls
        calls+=1
        if calls>1:raise RuntimeError("Private connection details must not be returned")
        return snapshot(clock)
    cache=CurrentMetricsCache(collect,monotonic=clock.monotonic,utc_now=clock.now)
    assert (await cache.get())["samples"]
    clock.seconds=5
    missing=await cache.get()
    assert missing["source"]=="unavailable" and missing["samples"]==[]
    assert "Private connection" not in missing["note"]
    clock.seconds=9
    assert (await cache.get())["samples"]==[] and calls==2
    await cache.close()


@pytest.mark.asyncio
async def test_slow_collection_is_cancelled_at_deadline_and_returns_unavailable():
    cancelled=asyncio.Event()
    async def collect():
        try:await asyncio.Event().wait()
        finally:cancelled.set()
    cache=CurrentMetricsCache(collect,timeout=0.01)
    result=await asyncio.wait_for(cache.get(),timeout=0.5)
    assert result["source"]=="unavailable" and result["samples"]==[]
    assert cancelled.is_set()
    await cache.close()


@pytest.mark.asyncio
async def test_disconnected_visitor_does_not_cancel_collection_shared_with_another_visitor():
    clock=Clock();started=asyncio.Event();release=asyncio.Event();calls=0
    async def collect():
        nonlocal calls
        calls+=1;started.set()
        await release.wait()
        return snapshot(clock)
    cache=CurrentMetricsCache(collect)
    first=asyncio.create_task(cache.get())
    await started.wait()
    second=asyncio.create_task(cache.get())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):await first
    release.set()
    assert (await second)["source"]=="kubernetes" and calls==1
    await cache.close()


@pytest.mark.asyncio
async def test_app_shutdown_cancels_the_owned_collection_task():
    started=asyncio.Event();cancelled=asyncio.Event()
    async def collect():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:cancelled.set()
    cache=CurrentMetricsCache(collect)
    reader=asyncio.create_task(cache.get())
    await started.wait()
    await cache.close()
    with pytest.raises(asyncio.CancelledError):await reader
    assert cancelled.is_set()


def test_current_endpoint_reads_idle_cluster_without_altering_or_using_saved_run_metrics(tmp_path,monkeypatch):
    clock=Clock();calls=0
    clock.start=datetime.now(timezone.utc)
    live=snapshot(clock,cpu=0.4,pod_uid="current-manager",family="n4a")
    recorded={"source":"kubernetes","samples":[{"role":"backend","cpu_cores":4.2}],"note":"Archived run"}
    historical={"id":"a"*12,"status":"passed","mode":"live","_metrics":copy.deepcopy(recorded)}
    monkeypatch.setattr(manager,"DATA",tmp_path)
    monkeypatch.setattr(manager,"RUNS",{historical["id"]:historical})
    async def collect():
        nonlocal calls
        calls+=1
        return live
    monkeypatch.setattr(manager,"collect_metrics",collect)
    with TestClient(manager.app) as client:
        response=client.get("/api/metrics/current")
        assert response.status_code==200 and response.headers["Cache-Control"]=="no-store"
        body=response.json()
        assert body["scope"]=="current" and body["samples"][0]["pod_uid"]=="current-manager"
        assert body["samples"][0]["machine_family"]=="n4a"
        assert body["samples"][0]["cpu_cores"]==0.4 and body["samples"][0]["cpu_fresh"] is True
        assert body["collected_at"] and body["max_sample_age_seconds"]==60
        assert client.get("/api/metrics/current").json()["collected_at"]==body["collected_at"]
        assert calls==1
        assert client.get("/api/runs/"+historical["id"]+"/metrics").json()==recorded
        assert historical["_metrics"]==recorded and list(tmp_path.iterdir())==[]
    # A restarted process/lifespan must not retain an earlier Pod snapshot.
    with TestClient(manager.app) as client:
        assert client.get("/api/metrics/current").status_code==200
        assert calls==2


def test_current_endpoint_never_falls_back_to_recorded_run_when_cluster_is_unavailable(tmp_path,monkeypatch):
    monkeypatch.setattr(manager,"DATA",tmp_path)
    monkeypatch.setattr(manager,"RUNS",{"a"*12:{"_metrics":{"source":"kubernetes","samples":[{"cpu_cores":3}]}}})
    async def missing():return {"source":"unavailable","samples":[],"note":"No cluster access"}
    monkeypatch.setattr(manager,"collect_metrics",missing)
    with TestClient(manager.app) as client:
        body=client.get("/api/metrics/current").json()
        assert body["source"]=="unavailable" and body["samples"]==[]
        assert body["scope"]=="current" and body["poll_after_seconds"]==5
