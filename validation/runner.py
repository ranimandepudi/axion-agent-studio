"""Protected subprocess entry point, executed only inside an isolated validator."""
import importlib.util
import json
from pathlib import Path
import sys
import resource

resource.setrlimit(resource.RLIMIT_CPU,(3,3))
resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))

request=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location("candidate",Path(sys.argv[1]))
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
results=[]
for case in request["cases"]:
    try:
        result=module.quote(case["items"],case["catalog"])
        results.append(result)
    except Exception as exc:
        results.append({"error":type(exc).__name__+": "+str(exc)[:200]})
print(json.dumps(results))
