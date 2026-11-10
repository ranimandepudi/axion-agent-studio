"""Run only in an explicitly isolated Linux container, using trusted fixtures."""
import pytest
from fastapi.testclient import TestClient
from validation.app import app,isolated_available,execute
from studio.domain import Policy
from studio.rehearsal import fixture_files

pytestmark=pytest.mark.skipif(not isolated_available(),reason="Requires isolated Linux validator container")


def test_isolated_fixture_and_agent_tests_execute():
    p=Policy(discount_percent=10,discount_threshold_cents=10000,free_shipping_threshold_cents=15000)
    files=fixture_files(p,{"checkout.html":"<div id='savings'></div><script>window.addEventListener('message',function(e){const quote=e.data.quote;});</script>"})
    files["qa_tests.py"]='''import unittest
from pricing import quote
class PricingTests(unittest.TestCase):
    def test_free_shipping(self):
        result=quote([{"sku":"a","quantity":1}],[{"sku":"a","price_cents":15000}])
        self.assertEqual(result["shipping_cents"],0)
        self.assertEqual(result["total_cents"],13500)
'''
    with TestClient(app) as client:
        result=client.post("/validate",json={"run_id":"aabbccddeeff","policy":p.model_dump(),"files":files,"catalog":[{"sku":"a","price_cents":15000}]}).json()
        assert result["status"]=="passed",result
        assert result["qa_result"]["tests_run"]==1
        quote=client.post("/quote/aabbccddeeff",json={"items":[{"sku":"a","quantity":1}]}).json()
        assert quote["total_cents"]==13500


def test_generated_output_cannot_grow_parent_unbounded():
    source='def quote(items,catalog):\n while True: print("x"*10000)'
    with pytest.raises(ValueError,match="execution failed"):
        execute(source,[{"items":[],"catalog":[]}])


def test_nonterminating_generated_code_is_stopped():
    source='def quote(items,catalog):\n while True: pass'
    with pytest.raises(ValueError,match="execution failed"):
        execute(source,[{"items":[],"catalog":[]}])
