"""Keep direct IPAM-only execution independent of an orchestration service."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class DirectModeTests(unittest.TestCase):
    def test_direct_credentials_and_optional_tls_bundle(self):
        task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())['spec']
        reserve = next(s for s in task['steps'] if s['name'] == 'reserve-ip')
        self.assertIn({'secretRef': {'name': 'vm-ipam-credentials'}}, reserve['envFrom'])
        self.assertEqual(reserve['volumeMounts'], [
            {'name': 'ipam-ca', 'mountPath': '/etc/ipam-ca', 'readOnly': True}])
        self.assertEqual(task['volumes'], [{'name': 'ipam-ca', 'configMap': {
            'name': 'vm-ipam-ca', 'optional': True}}])
        self.assertIn('SSL_CERT_FILE=/etc/ipam-ca/ca-bundle.crt', reserve['script'])
        self.assertNotIn('request-id', [p['name'] for p in task['params']])
        self.assertIn('vm-ipam 0.4: IPAM-only subnet allocation', task['steps'][0]['script'])
        for path in ('scripts/ipam.py', 'tekton/task.yaml', 'tekton/pipeline.yaml',
                     'config/site.example.yaml', 'tekton/request-template.yaml'):
            text = (ROOT / path).read_text()
            for marker in ('AAP_', 'vm-aap-', '/api/controller/', 'artifacts.vm_network'):
                self.assertNotIn(marker, text, path)

    def test_version_and_site_contract(self):
        build = list(yaml.safe_load_all((ROOT / 'build/buildconfig.yaml').read_text()))
        config = next(obj for obj in build if obj['kind'] == 'BuildConfig')
        self.assertEqual(config['spec']['output']['to']['name'], 'vm-ipam:0.4')
        site = yaml.safe_load((ROOT / 'config/site.example.yaml').read_text())['data']
        for key in ('IPAM_URL', 'INFOBLOX_WAPI_VERSION', 'INFOBLOX_NETWORK',
                    'INFOBLOX_NETWORK_VIEW', 'INFOBLOX_ALLOCATION_MODE',
                    'GATEWAY', 'DNS_SERVERS', 'DOMAIN'):
            self.assertIn(key, site)
        for key in ('INFOBLOX_DNS_VIEW', 'STATIC_START', 'STATIC_END', 'DHCP_START', 'DHCP_END', 'DHCP_DISABLED', 'INFOBLOX_PASSWORD'):
            self.assertNotIn(key, site)
        pipeline = yaml.safe_load((ROOT / 'tekton/pipeline.yaml').read_text())['spec']
        self.assertEqual([p['name'] for p in pipeline['params']],
                         ['vm-name', 'size', 'count', 'ssh-public-key', 'vm-labels', 'target-namespace'])

    def test_readonly_check_uses_new_image_and_no_allocation_command(self):
        template = yaml.safe_load((ROOT / 'tekton/ipam-check.yaml').read_text())
        run = template['objects'][0]
        self.assertEqual(run['kind'], 'TaskRun')
        self.assertEqual(run['spec']['serviceAccountName'], 'vm-provision')
        step = run['spec']['taskSpec']['steps'][0]
        self.assertIn('python3 /opt/vm/ipam.py check-ipam "$CHECK_VM_NAME"', step['script'])
        self.assertIn('vm-ipam 0.4: IPAM-only subnet allocation', step['script'])
        self.assertNotIn('python3 /opt/vm/ipam.py\n', step['script'])
        installation = yaml.safe_load((ROOT / 'tekton/kustomization.yaml').read_text())
        self.assertNotIn('ipam-check.yaml', installation['resources'])


if __name__ == '__main__':
    unittest.main()
