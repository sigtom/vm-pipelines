"""Native per-network catalog processing and profile isolation; no live APIs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
OC = shutil.which('oc')
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea'
FIELDS = ('NETWORK_ATTACHMENT INFOBLOX_NETWORK INFOBLOX_NETWORK_VIEW INFOBLOX_DNS_VIEW '
          'STATIC_START STATIC_END GATEWAY DNS_SERVERS DOMAIN DHCP_START DHCP_END DHCP_DISABLED').split()
PROFILE = dict(PROFILE_ENABLED='true', NETWORK_ATTACHMENT='vlan-101',
               INFOBLOX_NETWORK='192.0.2.0/24', INFOBLOX_NETWORK_VIEW='network-view',
               INFOBLOX_DNS_VIEW='dns-view', STATIC_START='192.0.2.32', STATIC_END='192.0.2.47',
               GATEWAY='192.0.2.1', DNS_SERVERS='192.0.2.2,192.0.2.3', DOMAIN='example.test',
               DHCP_START='', DHCP_END='', DHCP_DISABLED='true')
SITE = dict(VM_NAMESPACE='vm-provisioning', NETWORK_ATTACHMENT='legacy-nad',
            DATASOURCE_NAME='rhel-golden', DATASOURCE_NAMESPACE='image-sources',
            STORAGE_CLASS='approved-storage', ACCESS_MODE='ReadWriteMany', VOLUME_MODE='Block',
            MACHINE_TYPE='q35', GUEST_USER='guestops', IPAM_URL='https://ipam.example.test',
            INFOBLOX_NETWORK='198.51.100.0/24', GATEWAY='198.51.100.1', DOMAIN='legacy.test')


class NetworkCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.created = self.root / 'created'
        self.created.mkdir()
        self.calls = self.root / 'calls'
        self.task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())['spec']
        self.env = dict(SITE, PATH=str(self.bin) + ':' + os.environ['PATH'], HOME=str(self.root),
                        KUBECONFIG=str(self.root / 'no-kubeconfig'), SCRATCH=str(self.scratch),
                        NETWORK_PROFILE_BOUND='true', NETWORK_PROFILE_PATH=str(self.profile),
                        REQ_NAME='web-01', REQ_SIZE='small', REQ_COUNT='1', REQ_LABELS='foo=bar;boo=far',
                        REQ_SSH_KEY=KEY, CALLS=str(self.calls), REAL_OC=OC or '',
                        VM_TEMPLATE=str(ROOT / 'templates/rhel-vm.yaml'), CREATED=str(self.created),
                        VM_NAMES_FILE=str(self.scratch / 'names'), ALLOCATIONS_FILE=str(self.scratch / 'allocations'),
                        PROFILE_FIELDS=' '.join(FIELDS))
        # The CLI is mocked except real oc process/label --local; python3 is a
        # fake helper that records its environment without making any HTTP call.
        fake = '''import json, os, shutil, sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['CALLS'],'a') as f: f.write(name+' '+repr(args)+'\\n')
if name=='python3':
    assert args==['/opt/vm/ipam.py']
    data={key:os.environ.get(key) for key in os.environ['PROFILE_FIELDS'].split()}
    Path(os.environ['SCRATCH'],'helper-env.json').write_text(json.dumps(data))
    vm=Path(os.environ['VM_NAMES_FILE']).read_text().splitlines()[0]
    Path(os.environ['ALLOCATIONS_FILE']).write_text('|'.join([vm,'192.0.2.40/24','02:11:22:33:44:55',data['GATEWAY'],data['DNS_SERVERS'],data['DOMAIN']])+'\\n')
elif args[0] in ('process','label'):
    assert '--local' in args
    os.execv(os.environ['REAL_OC'],[os.environ['REAL_OC'],*args])
elif 'auth' in args: print('yes')
elif 'get' in args:
    i=args.index('get'); kind=args[i+1]; obj=args[i+2]
    if kind.startswith('templates.'):
        if 'yaml' in args: sys.stdout.write(Path(os.environ['VM_TEMPLATE']).read_text())
        elif any(x.startswith('go-template=') for x in args): print('vm-pipelines/size')
        else: print('present')
    elif kind.startswith(('virtualmachineinstancetypes.','network-attachment-definitions.')): print('present')
    elif kind.startswith('datasources.'): print('True|golden-root|image-sources')
    elif kind=='persistentvolumeclaims' and obj=='golden-root': print('Bound|approved-storage|Block|104Gi')
    elif kind.startswith(('virtualmachines.','datavolumes.')) or kind=='persistentvolumeclaims': pass
    else: sys.exit(97)
elif 'create' in args:
    src=Path(args[args.index('-f')+1]); shutil.copyfile(src,Path(os.environ['CREATED'])/src.name)
else: sys.exit(97)
'''
        for name in ('oc', 'python3'):
            executable = self.bin / name
            executable.write_text('#!' + sys.executable + ' -S\n' + fake)
            executable.chmod(0o755)
        self.write_profile(PROFILE)

    def write_profile(self, data):
        for file in self.profile.iterdir():
            file.unlink()
        for key, value in data.items():
            (self.profile / key).write_text(value)
        for file in self.scratch.glob('network.env*'):
            file.unlink()
        if self.calls.exists():
            self.calls.unlink()

    def run_step(self, name):
        script = next(step['script'] for step in self.task['steps'] if step['name'] == name)
        shell = 'bash' if 'bash' in script.splitlines()[0] else 'sh'
        return subprocess.run([shell, '-c', script], env=self.env, cwd=self.root,
                              text=True, capture_output=True)

    def require_oc(self):
        if not OC:
            if os.environ.get('REQUIRE_OC') == '1':
                self.fail('oc required in CI')
            self.skipTest('oc not installed')

    def test_optional_workspace_keeps_five_inputs_and_existing_identity(self):
        pipeline = yaml.safe_load((ROOT / 'tekton/pipeline.yaml').read_text())['spec']
        self.assertEqual(len(pipeline['params']), 5)
        workspace = next(w for w in self.task['workspaces'] if w['name'] == 'network')
        self.assertTrue(workspace['optional'])
        self.assertTrue(workspace['readOnly'])
        self.assertTrue(next(w for w in pipeline['workspaces'] if w['name'] == 'network')['optional'])
        self.assertIn({'name': 'network', 'workspace': 'network'}, pipeline['tasks'][0]['workspaces'])
        for step in self.task['steps']:
            if step['name'] != 'validate-key':
                self.assertIn({'name': 'NETWORK_PROFILE_BOUND', 'value': '$(workspaces.network.bound)'}, step['env'])
        legacy = yaml.safe_load((ROOT / 'tekton/request-template.yaml').read_text())
        self.assertEqual(legacy['objects'][0]['spec']['workspaces'], [{'name': 'scratch', 'emptyDir': {}}])

    def test_native_two_stage_processing_keeps_fixed_network_and_five_form_fields(self):
        self.require_oc()
        names = set()
        for number in range(101, 106):
            nad = f'vlan-{number}'
            entry = f'vm-provision-{nad}'
            cmd = [OC, 'process', '--local', '-f', str(ROOT / 'tekton/network-catalog.yaml'), '-o', 'json',
                   '-p', f'NETWORK_ATTACHMENT={nad}', '-p', f'REQUEST_TEMPLATE_NAME={entry}',
                   '-p', f'DISPLAY_NAME=Provision VM on VLAN {number}']
            outer = json.loads(subprocess.check_output(cmd, text=True))
            self.assertEqual([obj['kind'] for obj in outer['items']], ['ConfigMap', 'Template'])
            config, form = outer['items']
            self.assertEqual(config['metadata']['name'], 'vm-network-' + nad)
            self.assertEqual(config['data']['NETWORK_ATTACHMENT'], nad)
            self.assertEqual(config['data']['PROFILE_ENABLED'], 'false')
            self.assertTrue(set(FIELDS) <= set(config['data']))
            self.assertEqual(form['metadata']['name'], entry)
            self.assertEqual(form['metadata']['annotations']['openshift.io/display-name'], f'Provision VM on VLAN {number}')
            self.assertEqual([p['name'] for p in form['parameters']], ['VM_NAME', 'SIZE', 'COUNT', 'SSH_PUBLIC_KEY', 'VM_LABELS'])
            cmd = [OC, 'process', '--local', '-f', '-', '-o', 'json', '-p', 'VM_NAME=web-01',
                   '-p', 'SIZE=small', '-p', 'COUNT=1', '-p', 'SSH_PUBLIC_KEY=' + KEY,
                   '-p', 'VM_LABELS=foo=bar;boo=far']
            inner = json.loads(subprocess.check_output(cmd, input=json.dumps(form), text=True))
            run = inner['items'][0]
            self.assertEqual(run['kind'], 'PipelineRun')
            self.assertEqual(run['metadata']['name'], 'vm-request-web-01')
            self.assertEqual(run['spec']['pipelineRef']['name'], 'vm-provision')
            self.assertEqual(run['spec']['taskRunTemplate']['serviceAccountName'], 'vm-provision')
            self.assertEqual(run['spec']['workspaces'], [{'name': 'scratch', 'emptyDir': {}},
                {'name': 'network', 'configMap': {'name': 'vm-network-' + nad}}])
            self.assertEqual({p['name']: p['value'] for p in run['spec']['params']}['vm-labels'], 'foo=bar;boo=far')
            names.add(config['metadata']['name'])
        self.assertEqual(len(names), 5)

    def test_selected_profile_is_complete_and_snapshot_overrides_site(self):
        run = self.run_step('preflight')
        self.assertEqual(run.returncode, 0, run.stderr)
        snapshot = dict(line.split('=', 1) for line in (self.scratch / 'network.env').read_text().splitlines())
        self.assertEqual(snapshot, {key: PROFILE[key] for key in FIELDS})
        self.assertIn('vlan-101', self.calls.read_text())
        self.assertNotIn('legacy-nad', self.calls.read_text())

    def test_disabled_profile_stops_before_any_cli_or_ipam(self):
        self.write_profile(dict(PROFILE, PROFILE_ENABLED='false'))
        run = self.run_step('preflight')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('disabled', run.stderr)
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.scratch / 'network.env').exists())

    def test_missing_blank_placeholder_and_multiline_profile_do_not_fall_back(self):
        cases = [dict(PROFILE, GATEWAY=''), dict(PROFILE, DOMAIN='REPLACE_ME_DOMAIN'),
                 dict(PROFILE, INFOBLOX_DNS_VIEW='view\nBAD=setting'),
                 dict(PROFILE, NETWORK_ATTACHMENT='other/namespace'),
                 dict(PROFILE, DHCP_START='192.0.2.50'),
                 {key: value for key, value in PROFILE.items() if key != 'INFOBLOX_NETWORK'}]
        for data in cases:
            with self.subTest(data=data):
                self.write_profile(data)
                run = self.run_step('preflight')
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(self.calls.exists())
                self.assertFalse((self.scratch / 'network.env').exists())

    def test_profile_data_is_not_executed_and_unknown_keys_not_exported(self):
        self.write_profile(dict(PROFILE, INFOBLOX_DNS_VIEW='$(touch unexpected)', PATH='/bad/path'))
        run = self.run_step('preflight')
        self.assertEqual(run.returncode, 0, run.stderr)
        run = self.run_step('reserve-ip')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((self.root / 'unexpected').exists())
        self.assertNotIn('PATH=', (self.scratch / 'network.env').read_text())
        env = json.loads((self.scratch / 'helper-env.json').read_text())
        self.assertEqual(env['INFOBLOX_DNS_VIEW'], '$(touch unexpected)')

    def test_bound_profile_cannot_run_later_steps_without_snapshot(self):
        for name in ('reserve-ip', 'create-vms'):
            with self.subTest(name=name):
                run = self.run_step(name)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('Missing validated network snapshot', run.stderr)
                self.assertFalse(self.calls.exists())

    def test_legacy_request_without_profile_preserves_site_network(self):
        self.env['NETWORK_PROFILE_BOUND'] = 'false'
        run = self.run_step('preflight')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((self.scratch / 'network.env').exists())
        self.assertIn('legacy-nad', self.calls.read_text())

    def test_snapshot_used_for_ipam_and_vm_after_profile_changes(self):
        self.require_oc()
        for number in range(101, 106):
            with self.subTest(network=number):
                selected = dict(PROFILE, NETWORK_ATTACHMENT=f'vlan-{number}')
                self.write_profile(selected)
                pre = self.run_step('preflight')
                self.assertEqual(pre.returncode, 0, pre.stderr)
                # Simulate a ConfigMap update between containers; the captured
                # NAD and Infoblox settings must remain paired for this request.
                (self.profile / 'NETWORK_ATTACHMENT').write_text('changed-nad')
                (self.profile / 'GATEWAY').write_text('198.51.100.1')
                reserve = self.run_step('reserve-ip')
                self.assertEqual(reserve.returncode, 0, reserve.stderr)
                env = json.loads((self.scratch / 'helper-env.json').read_text())
                self.assertEqual(env, {key: selected[key] for key in FIELDS})
                create = self.run_step('create-vms')
                self.assertEqual(create.returncode, 0, create.stderr)
                data = yaml.safe_load((self.created / 'web-01.yaml').read_text())
                vm = data['items'][0] if data['kind'] == 'List' else data
                self.assertEqual(vm['spec']['template']['spec']['networks'][0]['multus']['networkName'], f'vlan-{number}')
                self.assertEqual(vm['metadata']['labels']['foo'], 'bar')
                self.assertEqual(vm['metadata']['labels']['boo'], 'far')
                cloud = vm['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']
                net = yaml.safe_load(cloud['networkData'])['ethernets']['lan']
                self.assertEqual(net['addresses'], ['192.0.2.40/24'])
                self.assertEqual(net['routes'][0]['via'], '192.0.2.1')


if __name__ == '__main__':
    unittest.main()
