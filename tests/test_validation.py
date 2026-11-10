import pytest
from validation.app import inspect_source, execute


@pytest.mark.parametrize("source",[
    "import os\ndef quote(items,catalog):return os.environ",
    "def quote(items,catalog):return __import__('subprocess')",
    "def quote(items,catalog):return open('/etc/passwd').read()",
    "def quote(items,catalog):return ().__class__.__mro__",
    "def quote(items,catalog):return eval('1')",
])
def test_restrict_unneeded_system_access(source):
    with pytest.raises(ValueError):inspect_source(source)


def test_plain_checkout_code_allowed():
    inspect_source('def quote(items,catalog):\n return {"total_cents":sum(x["quantity"] for x in items)}')


def test_never_execute_generated_code_on_host(monkeypatch):
    monkeypatch.delenv("VALIDATOR_ISOLATED",raising=False)
    with pytest.raises(RuntimeError,match="isolated Linux"):
        execute('def quote(items,catalog):return {}',[])
