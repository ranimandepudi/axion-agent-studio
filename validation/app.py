"""Independent acceptance service. Generated code never executes in the manager."""
from __future__ import annotations
import ast
import asyncio
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import hashlib
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from studio.domain import Policy, QuoteRequest, acceptance_cases, check_results

app=FastAPI(title="Axion isolated validation")
ROOT=Path(__file__).parent
RUNS: dict[str,dict]={}
LOCK=threading.Lock()


class ValidationRequest(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    policy: Policy
    files: dict[str,str]
    catalog: list[dict]


def inspect_source(source: str):
    if len(source)>64000:
        raise ValueError("Candidate file exceeds size limit")
    tree=ast.parse(source)
    denied={"eval","exec","compile","open","input","__import__","getattr","setattr","delattr","globals","locals","vars","breakpoint","help","exit","quit","type"}
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            modules=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ""]
            if any(m not in {"math","decimal","typing"} for m in modules):
                raise ValueError("Checkout module may import only math, decimal or typing")
        if isinstance(node,ast.Name) and (node.id in denied or node.id.startswith("__")):
            raise ValueError("Unsupported dynamic or system access in checkout module")
        if isinstance(node,ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Private attribute access is unsupported")
    if not any(isinstance(n,ast.FunctionDef) and n.name=="quote" for n in tree.body):
        raise ValueError("Candidate must define quote(items, catalog)")


def isolated_available():
    return platform.system()=="Linux" and os.getenv("VALIDATOR_ISOLATED")=="1"


def bounded_process(command,directory,payload=None):
    # Child harness sets RLIMIT_FSIZE before loading generated code. File-backed
    # output avoids unbounded capture_output buffers in the parent process.
    stdout_path=Path(directory)/"stdout.log";stderr_path=Path(directory)/"stderr.log"
    with stdout_path.open("w") as out,stderr_path.open("w") as err:
        result=subprocess.run(command,input=payload,text=True,stdout=out,stderr=err,timeout=8,env={"PATH":"/usr/local/bin:/usr/bin:/bin","PYTHONHASHSEED":"0"},cwd=directory)
    if result.returncode:
        with stderr_path.open() as stream:
            detail=stream.read(3000)
        raise ValueError("Candidate execution failed: "+detail[-600:])
    if stdout_path.stat().st_size>128000:raise ValueError("Candidate output exceeds limit")
    return json.loads(stdout_path.read_text())


def execute(source: str, cases: list[dict]) -> list[dict]:
    if not isolated_available():
        raise RuntimeError("Generated-code execution requires the isolated Linux validator container")
    inspect_source(source)
    with tempfile.TemporaryDirectory(prefix="axion-validation-") as directory:
        candidate=Path(directory)/"pricing.py"
        candidate.write_text(source)
        result=bounded_process([sys.executable,"-I","-S",str(ROOT/"runner.py"),str(candidate)],directory,json.dumps({"cases":cases}))
        if not isinstance(result,list) or len(result)!=len(cases):
            raise ValueError("Invalid candidate result shape")
        return result


def execute_qa(source: str, qa_source: str):
    if not isolated_available():
        raise RuntimeError("QA execution requires an isolated Linux validator")
    if len(qa_source)>64000:raise ValueError("QA source exceeds size limit")
    tree=ast.parse(qa_source)
    forbidden={"eval","exec","compile","open","input","__import__","getattr","setattr","delattr","globals","locals","vars","breakpoint"}
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            modules=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ""]
            if any(m not in {"unittest","pricing","math","decimal","typing"} for m in modules):raise ValueError("QA may import only unittest, pricing, math, decimal or typing")
        if isinstance(node,ast.Name) and (node.id in forbidden or node.id.startswith("__") and node.id!="__name__"):
            raise ValueError("Unsupported system access in QA module")
        if isinstance(node,ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Private attribute access is unsupported in QA")
    with tempfile.TemporaryDirectory(prefix="axion-qa-") as directory:
        candidate=Path(directory)/"pricing.py";candidate.write_text(source)
        qa=Path(directory)/"qa_tests.py";qa.write_text(qa_source)
        return bounded_process([sys.executable,"-I","-S",str(ROOT/"qa_runner.py"),str(candidate),str(qa)],directory)


@app.get("/health")
def health():
    return {"status":"ready" if isolated_available() else "unavailable","isolated_execution":isolated_available(),"architecture":platform.machine(),"runtime":os.getenv("SANDBOX_RUNTIME","unknown")}


@app.post("/validate")
async def validate(request: ValidationRequest):
    if not isolated_available():
        raise HTTPException(503,"Isolated execution not configured")
    if set(request.files)-{"pricing.py","checkout.html","qa_tests.py"}:
        raise HTTPException(400,"Unexpected candidate files")
    try:
        source=request.files["pricing.py"]
        cases=acceptance_cases(request.policy)
        outputs=await asyncio.to_thread(execute,source,cases)
        tests=check_results(cases,outputs,request.policy)
        html=request.files.get("checkout.html","")
        html_ok="quote" in html and "<" in html and len(html)<=64000
        tests["cases"].append({"name":"checkout preview contract","passed":html_ok,"detail":"Quote receiver present; visual behavior is checked separately" if html_ok else "Missing checkout HTML or quote receiver"})
        tests["total"]+=1
        tests["passed"]+=int(html_ok)
        tests["failed"]+=int(not html_ok)
        qa_result=None
        if request.files.get("qa_tests.py"):
            try:
                qa_result=await asyncio.to_thread(execute_qa,source,request.files["qa_tests.py"])
                qa_ok=qa_result["tests_run"]>0 and qa_result["failed"]==0
                detail=f"{qa_result['tests_run']} agent-written tests; {qa_result['failed']} failures. "+qa_result.get("detail","")[-600:]
            except Exception as exc:
                qa_ok=False;detail=str(exc)[:800]
            tests["cases"].append({"name":"supplemental QA tests","passed":qa_ok,"detail":detail})
            tests["total"]+=1;tests["passed"]+=int(qa_ok);tests["failed"]+=int(not qa_ok)
        passed=tests["failed"]==0
        if passed:
            with LOCK:
                RUNS[request.run_id]={"source":source,"catalog":request.catalog,"sha256":hashlib.sha256(source.encode()).hexdigest()}
                while len(RUNS)>50:
                    RUNS.pop(next(iter(RUNS)))
        return {"status":"passed" if passed else "failed","tests":tests,"error":None,"qa_result":qa_result,"qa_note":"Agent-written unittest cases run separately from the protected acceptance suite."}
    except Exception as exc:
        return {"status":"failed","tests":{"passed":0,"failed":1,"total":1,"cases":[{"name":"candidate execution","passed":False,"detail":str(exc)[:800]}]},"error":str(exc)[:800]}


@app.post("/quote/{run_id}")
async def quote(run_id: str, request: QuoteRequest):
    with LOCK:
        candidate=RUNS.get(run_id)
    if not candidate:
        raise HTTPException(404,"Validated candidate is unavailable; rerun validation")
    try:
        return (await asyncio.to_thread(execute,candidate["source"],[{"items":[i.model_dump() for i in request.items],"catalog":candidate["catalog"]}]))[0]
    except Exception as exc:
        raise HTTPException(422,str(exc)[:500]) from exc
