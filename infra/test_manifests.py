"""Portable configuration/isolation tests. No cluster, model or cloud calls."""
import copy
import json
from pathlib import Path
import unittest

from infra.render import render


def fixture_config():
    config = json.loads((Path(__file__).parent / 'config.example.json').read_text())
    config.update(kube_api_cidr='10.42.0.2/32', node_cidrs=['10.42.0.0/20'],
                  pod_cidrs=['10.44.0.0/16'], service_cidrs=['10.45.0.0/20'])
    for key in ('worker_image', 'studio_image', 'model_image'):
        config[key] = 'registry.invalid/review@sha256:' + 'a' * 64
    return config


class PortableManifestTests(unittest.TestCase):
    def test_both_phases_have_real_isolation_and_scoped_services(self):
        for phase, count in [('checkpoint', 2), ('full', 4)]:
            with self.subTest(phase=phase):
                items = render(fixture_config(), phase)['items']
                sandboxes = [x for x in items if x['kind'] == 'Sandbox']
                self.assertEqual(len(sandboxes), count)
                for obj in sandboxes:
                    self.assertEqual(obj['apiVersion'], 'agents.x-k8s.io/v1beta1')
                    pod = obj['spec']['podTemplate']['spec']
                    self.assertEqual(pod['runtimeClassName'], 'gvisor')
                    self.assertEqual(pod['nodeSelector']['kubernetes.io/arch'], 'arm64')
                    self.assertFalse(pod['automountServiceAccountToken'])
                    self.assertTrue(all(c['securityContext']['readOnlyRootFilesystem'] for c in pod['containers']))
                self.assertTrue(all(x['spec']['type'] == 'ClusterIP' for x in items if x['kind'] == 'Service'))
                deny = next(x for x in items if x['kind'] == 'NetworkPolicy' and x['metadata']['name'] == 'default-deny')
                self.assertEqual(deny['spec']['policyTypes'], ['Ingress', 'Egress'])

    def test_full_placement_matches_shared_c4a_and_separate_n4a_roles(self):
        items = render(fixture_config(), 'full')['items']
        manager = next(x for x in items if x['kind'] == 'Deployment')['spec']['template']['spec']
        self.assertEqual(manager['nodeSelector']['studio.axion.dev/pool'], 'n4a-regular')
        self.assertNotIn('runtimeClassName', manager)
        for obj in [x for x in items if x['kind'] == 'Sandbox']:
            expected = 'n4a-sandbox' if obj['metadata']['name'] == 'validator' else 'c4a-sandbox'
            self.assertEqual(obj['spec']['podTemplate']['spec']['nodeSelector']['studio.axion.dev/pool'], expected)

    def test_endpoint_cannot_target_workload_or_special_ranges(self):
        for endpoint in ('10.44.0.2/32', '10.45.0.2/32', '127.0.0.1/32', '169.254.169.254/32', '10.42.0.0/20'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                render(dict(fixture_config(), kube_api_cidr=endpoint))

    def test_network_ranges_are_required_private_and_nonoverlapping(self):
        for changes in ({'node_cidrs': []}, {'pod_cidrs': ['10.42.0.0/16']},
                        {'service_cidrs': ['8.8.0.0/16']}, {'node_cidrs': ['::/64']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                render(dict(fixture_config(), **changes))

    def test_node_access_accepts_new_names_and_pins_exact_observed_addresses(self):
        config = fixture_config()
        config['node_metrics_addresses'] = {'gke-review-manager-a1': '10.42.0.3', 'gke-review-workers-b2': '10.42.0.4'}
        items = render(config)['items']
        role = next(x for x in items if x['kind'] == 'ClusterRole')
        self.assertEqual(role['rules'][0]['resourceNames'], sorted(config['node_metrics_addresses']))
        policy = next(x for x in items if x['kind'] == 'NetworkPolicy' and x['metadata']['name'] == 'manager-egress')
        rules = [r for r in policy['spec']['egress'] if {'protocol': 'TCP', 'port': 10250} in r['ports']]
        self.assertEqual(rules[0]['to'], [{'ipBlock': {'cidr': '10.42.0.3/32'}}, {'ipBlock': {'cidr': '10.42.0.4/32'}}])
        for bad in ('10.44.0.3', '10.42.0.0', '10.42.15.255', '8.8.8.8'):
            with self.subTest(address=bad), self.assertRaises(ValueError):
                render(dict(config, node_metrics_addresses={'gke-review-a': bad}))

    def test_no_node_allowlist_means_no_node_privileges_or_kubelet_egress(self):
        items = render(fixture_config())['items']
        self.assertFalse(any(x['kind'].startswith('ClusterRole') for x in items))
        self.assertFalse(any(p['port'] == 10250 for x in items if x['kind'] == 'NetworkPolicy'
                             for r in x['spec'].get('egress', []) for p in r.get('ports', [])))

    def test_namespace_is_consistent_with_checkpoint_guard(self):
        with self.assertRaises(ValueError):
            render(dict(fixture_config(), namespace='other'))
        items = render(fixture_config())['items']
        self.assertTrue(all(x['metadata'].get('namespace', 'axion-agent-studio') == 'axion-agent-studio' for x in items))


if __name__ == '__main__':
    unittest.main()
