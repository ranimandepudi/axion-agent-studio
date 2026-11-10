"""Protected harness for supplemental worker-written unittest cases."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
import resource

resource.setrlimit(resource.RLIMIT_CPU,(3,3))
resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module

load("pricing",Path(sys.argv[1]))
qa=load("qa_tests",Path(sys.argv[2]))
output=io.StringIO()
result=unittest.TextTestRunner(stream=output,verbosity=1).run(unittest.defaultTestLoader.loadTestsFromModule(qa))
print(json.dumps({"tests_run":result.testsRun,"failed":len(result.failures)+len(result.errors),"detail":output.getvalue()[-2500:]}))
