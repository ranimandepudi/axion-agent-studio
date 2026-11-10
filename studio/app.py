from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import difflib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time
import uuid
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .domain import EXAMPLES, Policy, QuoteRequest, RunRequest, acceptance_cases, check_results, now, rehearsal_policy
from .planner import GeminiPlanner
from .rehearsal import fixture_files, quote_fixture
from .telemetry import CurrentMetricsCache, collect_metrics, collect_placement

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.getenv("STUDIO_DATA_DIR",str(ROOT/"data")))
RUNS:dict[str,dict]={}
BACKGROUND:set[asyncio.Task]=set()
ACTIVE=asyncio.Lock()
ROLES=("backend","frontend","qa")
WEB_ASSETS={"styles.css":"text/css","booth.css":"text/css","app.js":"text/javascript"}


def catalog():
    data=json.loads((ROOT/"sample_shop/catalog.json").read_text())
    return data if isinstance(data,list) else data.get("products",data.get("catalog",[]))


def originals():
    return {name:(ROOT/"sample_shop"/name).read_text() for name in ("pricing.py","checkout.html")}


def baseline(items):
    spec=importlib.util.spec_from_file_location("shop_baseline",ROOT/"sample_shop/pricing.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.quote(items,catalog())


def persist(run):
    DATA.mkdir(parents=True,exist_ok=True)
    target=DATA/(run["id"]+".json")
    tmp=target.with_suffix(".tmp")
    tmp.write_text(json.dumps(run,indent=2))
    tmp.replace(target)


def event(run,event_type,message,role=None,data=None):
    run["updated_at"]=now()
    run["elapsed_seconds"]=round(time.monotonic()-run["_started"],2)
    item={"seq":len(run["events"])+1,"timestamp":run["updated_at"],"type":event_type,"message":message}
    if role:item["role"]=role
    if data is not None:item["data"]=data
    run["events"].append(item)
    persist(run)


def public(run):
    return {k:v for k,v in run.items() if not k.startswith("_") and k not in {"files","originals"}}


def endpoints():
    return {role:os.getenv("WORKER_"+role.upper()+"_URL","").rstrip("/") for role in ROLES}


async def readiness():
    urls=endpoints()
    validator=os.getenv("VALIDATOR_URL","").rstrip("/")
    missing=[role for role,url in urls.items() if not url]
    if missing or not validator or not GeminiPlanner().configured:
        return False,"Configure hosted Gemini, all three local-model workers and the isolated validator.",{}
    async def one(name,url):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r=await client.get(url+"/health");r.raise_for_status();d=r.json()
            if d.get("status") not in {"ready","ok","healthy"}:
                raise ValueError("service not ready")
            if name=="validator" and not d.get("isolated_execution"):
                raise ValueError("validator isolation unavailable")
            return name,d
        except Exception:
            return name,None
    checks=dict(await asyncio.gather(*(one(k,v) for k,v in {**urls,"validator":validator}.items())))
    failed=[k for k,v in checks.items() if v is None]
    return not failed,"Ready for live execution" if not failed else "Not ready: "+", ".join(failed),checks


@asynccontextmanager
async def lifespan(app):
    app.state.current_metrics=CurrentMetricsCache(lambda:collect_metrics())
    DATA.mkdir(parents=True,exist_ok=True)
    for file in sorted(DATA.glob("*.json"))[-50:]:
        try:
            run=json.loads(file.read_text())
            if run["status"] not in {"passed","failed"}:
                run["status"]="failed";run["error"]="Manager restarted before this run completed. Start a new run."
            RUNS[run["id"]]=run
        except (ValueError,KeyError):pass
    try:
        yield
    finally:
        await app.state.current_metrics.close()
        for task in BACKGROUND:task.cancel()
        if BACKGROUND:await asyncio.gather(*BACKGROUND,return_exceptions=True)


app=FastAPI(title="Axion Agent Studio",lifespan=lifespan)


@app.middleware("http")
async def secure_response(request: Request,call_next):
    # Loopback/port-forward dashboard, no cross-origin run triggering.
    origin=request.headers.get("origin")
    if request.method in {"POST","PUT","DELETE","PATCH"} and origin and origin!=str(request.base_url).rstrip("/"):
        return HTMLResponse("Cross-origin mutation blocked",status_code=403)
    response=await call_next(request)
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["Referrer-Policy"]="no-referrer"
    return response


@app.get("/health")
def health():return {"status":"ready","service":"manager"}


def booth_config():
    """Explicit display configuration; never infer capacity from observed usage."""
    def positive_integer(name):
        value=os.getenv(name,"")
        return int(value) if value.isascii() and value.isdecimal() and 0<int(value)<=4096 else None
    return {
        "preferred_run_id":os.getenv("STUDIO_FEATURED_RUN_ID") or None,
        "worker_node_vcpus":positive_integer("STUDIO_WORKER_NODE_VCPUS"),
        "manager_node_vcpus":positive_integer("STUDIO_MANAGER_NODE_VCPUS"),
        "inference_threads_per_worker":positive_integer("STUDIO_INFERENCE_THREADS"),
        "source":"Deployment configuration; CPU usage is measured separately for each run.",
    }


@app.get("/api/config")
async def config():
    (ready,reason,checks),placement=await asyncio.gather(readiness(),collect_placement())
    return {"name":"Axion Agent Studio","live_available":ready,"live_reason":reason,"default_mode":"live","models":{"planner":GeminiPlanner().model,"worker":os.getenv("GEMMA_MODEL","Not verified")},"deployment":{"project":os.getenv("GOOGLE_CLOUD_PROJECT","Not connected"),"region":os.getenv("GOOGLE_CLOUD_LOCATION","Not connected"),"execution_environment":os.getenv("STUDIO_ENVIRONMENT","Local development")},"supported_examples":EXAMPLES,"services":checks,"booth":booth_config(),"placement":placement}


@app.get("/api/catalog")
def get_catalog():return catalog()


@app.get("/api/metrics/current")
async def current_metrics(request:Request):
    # Independent of selected runs: opening the booth always reads current
    # cluster inventory/measurements, including when no workflow is running.
    result=await request.app.state.current_metrics.get()
    return JSONResponse(result,headers={"Cache-Control":"no-store"})


@app.get("/api/runs")
def list_runs():return [public(r) for r in sorted(RUNS.values(),key=lambda r:r["created_at"],reverse=True)[:50]]


def get_run(run_id):
    if run_id not in RUNS:raise HTTPException(404,"Run not found")
    return RUNS[run_id]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):return public(get_run(run_id))


@app.post("/api/runs",status_code=202)
async def start_run(request:RunRequest):
    if ACTIVE.locked():raise HTTPException(409,"One run is already active; wait for it to finish")
    if request.mode=="live":
        ready,reason,_=await readiness()
        if not ready:raise HTTPException(503,reason)
    else:
        # Fixtures remain an explicit developer-test facility, disabled in the
        # deployed visitor service. Never substitute them for unavailable AI.
        if os.getenv("STUDIO_ENABLE_TEST_FIXTURES")!="1":
            raise HTTPException(403,"This studio accepts live agent workflows only.")
        try:rehearsal_policy(request.prompt)
        except ValueError as exc:raise HTTPException(422,str(exc)) from exc
    # No awaits between checking the lock and reserving it.
    if ACTIVE.locked():raise HTTPException(409,"One run is already active")
    await ACTIVE.acquire()
    run_id=uuid.uuid4().hex[:12]
    run={"id":run_id,"prompt":request.prompt,"mode":request.mode,"status":"queued","created_at":now(),"updated_at":now(),"policy":None,"plan":[],"agents":[{"role":role,"status":"pending","model":None,"machine_family":None,"sandbox":None,"elapsed_seconds":None,"summary":None} for role in ROLES],"tests":{"passed":0,"failed":0,"total":0,"cases":[]},"events":[],"error":None,"preview_url":None,"elapsed_seconds":0,"_started":time.monotonic(),"files":{},"originals":originals(),"review":None}
    RUNS[run_id]=run
    task=asyncio.create_task(execute_run(run));BACKGROUND.add(task);task.add_done_callback(BACKGROUND.discard)
    return public(run)


async def call_worker(run,task,files,feedback=""):
    role=task["role"];agent=next(a for a in run["agents"] if a["role"]==role)
    agent["status"]="running"
    event(run,"worker_started",f"{role.title()} Engineer is generating code with local Gemma",role)
    started=time.monotonic()
    async with httpx.AsyncClient(timeout=600) as client:
        health=await client.get(endpoints()[role]+"/health");health.raise_for_status();info=health.json()
        agent.update(model=info.get("model"),machine_family=info.get("machine_family"),sandbox=info.get("sandbox",os.getenv("STUDIO_ENVIRONMENT","unknown")))
        response=await client.post(endpoints()[role]+"/execute",json={"run_id":run["id"],"role":role,"instructions":task["instructions"]+("\nFix these independently observed failures: "+feedback if feedback else ""),"policy":run["policy"],"files":files})
        response.raise_for_status();result=response.json()
    agent["elapsed_seconds"]=round(time.monotonic()-started,2)
    if result.get("status")!="completed":
        agent["status"]="failed"
        raise RuntimeError(f"{role} worker failed: {result.get('error','unknown error')}")
    allowed={"backend":{"pricing.py"},"frontend":{"checkout.html"},"qa":{"qa_tests.py"}}[role]
    outputs=result.get("files",{})
    if set(outputs)!=allowed or any(not isinstance(v,str) or len(v)>64000 for v in outputs.values()):
        raise ValueError(f"{role} worker returned unexpected files")
    agent.update(status="completed",summary=result.get("summary"),model=result.get("model",agent["model"]))
    event(run,"worker_completed",result.get("summary") or f"{role} changes collected",role,{"elapsed_seconds":agent["elapsed_seconds"],"metrics":result.get("metrics")})
    return outputs


async def validate_remote(run):
    async with httpx.AsyncClient(timeout=45) as client:
        r=await client.post(os.environ["VALIDATOR_URL"].rstrip("/")+"/validate",json={"run_id":run["id"],"policy":run["policy"],"files":run["files"],"catalog":catalog()})
        r.raise_for_status();return r.json()


async def execute_run(run):
    run["_metrics"]={"source":"unavailable","samples":[],"note":"No measurements collected for this run."}
    async def sample():
        while True:
            data=await collect_metrics()
            if data["samples"]:
                run["_metrics"]["source"]=data["source"]
                run["_metrics"]["samples"]=(run["_metrics"]["samples"]+data["samples"])[-900:]
                run["_metrics"]["note"]="Actual manager and worker Pod samples captured during this run, including idle usage. "+data["note"]
            elif not run["_metrics"]["samples"]:
                run["_metrics"]["note"]=data["note"]
            persist(run)
            await asyncio.sleep(5)
    sampler=asyncio.create_task(sample()) if run["mode"]=="live" else None
    try:
        run["status"]="planning"
        if run["mode"]=="rehearsal":
            event(run,"rehearsal","Offline rehearsal: trusted fixtures only; no Gemini or Gemma inference, no cloud performance evidence.")
            policy=rehearsal_policy(run["prompt"]);run["policy"]=policy.model_dump()
            run["plan"]=[{"role":role,"title":title,"instructions":instructions,"files":[file]} for role,title,instructions,file in [("backend","Implement checkout policy","Apply the discount and delivery thresholds to original subtotal.","pricing.py"),("frontend","Display checkout changes","Show savings, shipping and final total.","checkout.html"),("qa","Review policy edge cases","Check threshold boundaries, empty cart and rounding.","qa_tests.py")]]
            event(run,"plan_created","Offline fixture plan loaded",data={"policy":run["policy"]})
            run["status"]="running"
            for agent in run["agents"]:
                agent.update(status="running",model="Offline fixture — no inference",machine_family="Not measured",sandbox="Local rehearsal")
                event(run,"worker_started","Loading trusted rehearsal artifact",agent["role"])
                await asyncio.sleep(0.35)
                agent.update(status="completed",summary="Trusted fixture loaded; no AI work performed",elapsed_seconds=None)
                event(run,"worker_completed",agent["summary"],agent["role"])
            run["files"]=fixture_files(policy,run["originals"])
            run["status"]="validating"
            cases=acceptance_cases(policy)
            outputs=[quote_fixture(c["items"],c["catalog"],policy) for c in cases]
            run["tests"]=check_results(cases,outputs,policy)
            run["review"]="Offline rehearsal verified the trusted fixture against boundary cases. This is not evidence of model quality, GKE execution or Axion performance."
        else:
            planner=GeminiPlanner()
            event(run,"planning","Hosted Gemini is inspecting the request and prepared codebase")
            plan=await planner.plan(run["prompt"],run["originals"])
            run["policy"]=plan.policy.model_dump();run["plan"]=[task.model_dump() for task in plan.tasks]
            event(run,"plan_created","Gemini plan ready; shared interfaces and policy defined",data={"policy":run["policy"]})
            run["status"]="running"
            tasks={t["role"]:t for t in run["plan"]}
            patches=await asyncio.gather(*(call_worker(run,tasks[role],run["originals"]) for role in ("backend","frontend")),return_exceptions=True)
            for patch in patches:
                if isinstance(patch,Exception):raise patch
                run["files"].update(patch)
            run["files"].update(await call_worker(run,tasks["qa"],run["files"]))
            run["status"]="validating";event(run,"validation_started","Running protected acceptance tests in the isolated validator")
            validation=await validate_remote(run);run["tests"]=validation["tests"]
            if validation["status"]!="passed":
                event(run,"repair","Checks failed; allowing one bounded repair round for the responsible specialists")
                feedback=json.dumps([c for c in run["tests"]["cases"] if not c["passed"]])[:3000]
                failures={c["name"] for c in run["tests"]["cases"] if not c["passed"]}
                repairs=[]
                if failures-{"checkout preview contract","supplemental QA tests"}:repairs.append("backend")
                if "checkout preview contract" in failures:repairs.append("frontend")
                for role in repairs:
                    run["files"].update(await call_worker(run,tasks[role],run["files"],feedback))
                # QA must review the actual repaired candidate, not stale code.
                run["files"].update(await call_worker(run,tasks["qa"],run["files"],feedback if "supplemental QA tests" in failures else ""))
                validation=await validate_remote(run);run["tests"]=validation["tests"]
            if validation["status"]!="passed":raise RuntimeError("Independent acceptance checks failed after the repair attempt")
            try:
                run["review"]=await planner.review(run["prompt"],run["policy"],run["tests"],{a["role"]:a["summary"] for a in run["agents"]})
                event(run,"review_completed",run["review"])
            except Exception:
                run["review"]="Protected tests passed. Hosted Gemini summary was unavailable."
                event(run,"review_unavailable",run["review"])
        if run["tests"]["failed"]:raise RuntimeError("Acceptance tests failed")
        run["status"]="passed";run["preview_url"]=f"/preview/{run['id']}/"
        event(run,"completed","Protected checks passed; updated shop preview is available" if run["mode"]=="live" else "Offline fixture checks passed; rehearsal preview is available")
    except asyncio.CancelledError:
        run["status"]="failed";run["error"]="Run interrupted";persist(run);raise
    except Exception as exc:
        run["status"]="failed";run["error"]=str(exc)[:1000]
        for a in run["agents"]:
            if a["status"]=="running":a["status"]="failed"
        event(run,"failed",run["error"])
    finally:
        if sampler:
            sampler.cancel()
            await asyncio.gather(sampler,return_exceptions=True)
        persist(run)
        if ACTIVE.locked():ACTIVE.release()


@app.get("/api/runs/{run_id}/events")
async def events(run_id:str,request:Request):
    get_run(run_id)
    async def stream():
        cursor=0
        while not await request.is_disconnected():
            run=get_run(run_id)
            for entry in run["events"][cursor:]:
                yield "data: "+json.dumps(entry)+"\n\n"
            cursor=len(run["events"])
            if run["status"] in {"passed","failed"}:break
            await asyncio.sleep(0.5)
    return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-cache"})


@app.get("/api/runs/{run_id}/metrics")
async def metrics(run_id:str):
    run=get_run(run_id)
    if run["mode"]!="live":return {"source":"unavailable","samples":[],"note":"Offline rehearsal: no model inference or cloud usage is measured."}
    return run.get("_metrics",{"source":"unavailable","samples":[],"note":"No measurements were captured for this run."})


@app.get("/api/runs/{run_id}/artifacts")
def artifacts(run_id:str):
    run=get_run(run_id)
    return {"files":[{"path":name,"before":run["originals"].get(name,""),"after":content,"diff":"".join(difflib.unified_diff(run["originals"].get(name,"").splitlines(True),content.splitlines(True),fromfile="before/"+name,tofile="after/"+name))} for name,content in run["files"].items()],"review":run["review"],"provenance":"local Gemma output" if run["mode"]=="live" else "trusted offline fixtures; not model-generated"}


@app.post("/api/baseline/quote")
def baseline_quote(request:QuoteRequest):
    try:return baseline([i.model_dump() for i in request.items])
    except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from exc


@app.post("/api/runs/{run_id}/quote")
async def candidate_quote(run_id:str,request:QuoteRequest):
    run=get_run(run_id)
    if run["status"]!="passed":raise HTTPException(409,"Candidate has not passed acceptance checks")
    items=[i.model_dump() for i in request.items]
    if any(item["sku"] not in {p["sku"] for p in catalog()} for item in items):raise HTTPException(422,"Unknown product")
    if run["mode"]=="rehearsal":return quote_fixture(items,catalog(),Policy.model_validate(run["policy"]))
    async with httpx.AsyncClient(timeout=15) as client:
        response=await client.post(os.environ["VALIDATOR_URL"].rstrip("/")+f"/quote/{run_id}",json=request.model_dump())
        if response.status_code>=400:raise HTTPException(502,"Validated candidate is unavailable; rerun the demo")
        result=response.json()
        if "error" in result:raise HTTPException(422,result["error"])
        return result


@app.get("/preview/{run_id}/",response_class=HTMLResponse)
def preview(run_id:str):
    if run_id=="baseline":html=originals()["checkout.html"]
    else:
        run=get_run(run_id)
        if run["status"]!="passed":raise HTTPException(409,"Preview is available only after checks pass")
        html=run["files"]["checkout.html"]
    return HTMLResponse(html,headers={"Content-Security-Policy":"sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'","Cache-Control":"no-store"})


@app.api_route("/",methods=["GET","HEAD"],include_in_schema=False)
@app.api_route("/index.html",methods=["GET","HEAD"],include_in_schema=False)
def web_index(request:Request):
    # New HTML must select the matching CSS/JS bundle, even when a browser has
    # cached an earlier deployment under these same base filenames.
    html=(ROOT/"web/index.html").read_text()
    for name in WEB_ASSETS:
        digest=hashlib.sha256((ROOT/"web"/name).read_bytes()).hexdigest()
        html=html.replace(f'"/{name}"',f'"/{name}?v={digest}"')
    content=html.encode("utf-8")
    return HTMLResponse(content if request.method!="HEAD" else b"",headers={
        "Cache-Control":"no-store","Content-Length":str(len(content))})


@app.api_route("/styles.css",methods=["GET","HEAD"],include_in_schema=False)
@app.api_route("/booth.css",methods=["GET","HEAD"],include_in_schema=False)
@app.api_route("/app.js",methods=["GET","HEAD"],include_in_schema=False)
def web_asset(request:Request):
    name=request.url.path.rsplit("/",1)[-1]
    content=(ROOT/"web"/name).read_bytes()
    digest=hashlib.sha256(content).hexdigest()
    version=request.query_params.get("v")
    if version is not None and version!=digest:
        # Never place new bytes in the cache entry for an old content version.
        return JSONResponse({"detail":"This UI asset version is unavailable. Refresh the page."},status_code=404,headers={"Cache-Control":"no-store"})
    etag=f'"{digest}"'
    headers={"ETag":etag,"Cache-Control":"public, max-age=31536000, immutable" if version else "no-cache"}
    matches=[tag.strip().removeprefix("W/") for tag in request.headers.get("if-none-match","").split(",")]
    if etag in matches or "*" in matches:
        return Response(status_code=304,headers=headers)
    # Intentionally no Last-Modified validator: same-sized edits within one
    # filesystem timestamp must still fetch changed bytes, not return 304.
    headers["Content-Length"]=str(len(content))
    return Response(content if request.method!="HEAD" else b"",media_type=WEB_ASSETS[name],headers=headers)


app.mount("/",StaticFiles(directory=ROOT/"web",html=True),name="web")
