"""Native rendering and real Task scripts with a fake API CLI; no cluster writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml  # Test dependency only.

ROOT = Path(__file__).resolve().parents[1]
OC = shutil.which('oc')
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea'
SIZES = [('small', '110Gi'), ('medium', '500Gi'), ('large', '1Ti')]
SITE = dict(VM_NAMESPACE='vm-provisioning', NETWORK_ATTACHMENT='approved-network',
            DATASOURCE_NAME='rhel-golden', DATASOURCE_NAMESPACE='image-sources',
            STORAGE_CLASS='approved-storage', ACCESS_MODE='ReadWriteMany',
            VOLUME_MODE='Block', GUEST_USER='guestops', MACHINE_TYPE='q35',
            IPAM_URL='https://ipam.example.test')


def process(file, params):
    command = [OC, 'process', '--local', '-f', str(ROOT / file), '-o', 'json']
    for key, value in params.items():
        command += ['-p', f'{key}={value}']
    return json.loads(subprocess.check_output(command, text=True))


class NativeTests(unittest.TestCase):
    def require_oc(self):
        if not OC:
            if os.environ.get('REQUIRE_OC') == '1':
                self.fail('oc required in CI')
            self.skipTest('oc not installed')

    def test_all_yaml_and_shell_parse(self):
        for path in ROOT.rglob('*.yaml'):
            list(yaml.safe_load_all(path.read_text()))
        task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())
        for step in task['spec']['steps']:
            result = subprocess.run(['bash', '-n'], input=step['script'], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        template = yaml.safe_load((ROOT / 'templates/rhel-vm.yaml').read_text())
        cloud = yaml.safe_load(template['objects'][0]['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']['userData'])
        for script in cloud['runcmd']:
            result = subprocess.run(['sh', '-n'], input=script, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_credentials_and_identity(self):
        task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())
        for step in task['spec']['steps']:
            secrets = [x for x in step.get('envFrom', []) if 'secretRef' in x]
            self.assertEqual(bool(secrets), step['name'] == 'reserve-ip')
            self.assertNotIn('$(params.vm-labels)', step['script'])
        self.assertEqual({x['name']: x['value'] for x in task['spec']['stepTemplate']['env']},
                         {'HOME': '/tmp', 'DOCKER_CONFIG': '/tmp/.docker'})
        docs = list(yaml.safe_load_all((ROOT / 'tekton/serviceaccount.yaml').read_text()))
        self.assertFalse(any(x['kind'].startswith('Cluster') for x in docs))
        pipeline = yaml.safe_load((ROOT / 'tekton/pipeline.yaml').read_text())
        self.assertEqual(len(pipeline['spec']['params']), 5)
        self.assertEqual(next(p for p in pipeline['spec']['params'] if p['name'] == 'vm-labels')['default'], '')
        self.assertEqual(next(p for p in task['spec']['params'] if p['name'] == 'vm-labels')['default'], '')
        self.assertIn({'name': 'vm-labels', 'value': '$(params.vm-labels)'}, pipeline['spec']['tasks'][0]['params'])

    def test_kustomize(self):
        self.require_oc()
        for directory in ('catalog', 'tekton', 'build'):
            data = subprocess.check_output([OC, 'kustomize', directory], cwd=ROOT, text=True)
            self.assertTrue(list(yaml.safe_load_all(data)))

    def test_request_form_native_processing(self):
        self.require_oc()
        key = KEY + ' ops "comment": #test'
        for labels in (None, '', 'foo=bar;boo=far', ' foo = bar ; example.com/team = ops '):
            params = dict(VM_NAME='web-01', SIZE='small', COUNT='2', SSH_PUBLIC_KEY=key)
            if labels is not None:
                params['VM_LABELS'] = labels
            run = process('tekton/request-template.yaml', params)['items'][0]
            self.assertEqual(run['spec']['pipelineRef']['name'], 'vm-provision')
            self.assertEqual(run['spec']['taskRunTemplate']['serviceAccountName'], 'vm-provision')
            self.assertEqual(run['spec']['workspaces'], [{'name': 'scratch', 'emptyDir': {}}])
            values = {p['name']: p['value'] for p in run['spec']['params']}
            self.assertEqual(values['ssh-public-key'], key)
            self.assertEqual(values['vm-labels'], labels or '')
            self.assertFalse(run['metadata'].get('labels'))

    def test_three_vm_sizes_and_embedded_cloud_init(self):
        self.require_oc()
        for size, disk in SIZES:
            for machine in ('q35', 'pc-q35-rhel9.6.0'):
                key = KEY + ' ops "quoted": #comment ${NOT_A_PARAM}'
                params = dict(SITE, VM_NAME='web-01', VM_SIZE=size, INSTANCE_TYPE='poc-' + size,
                              DISK_SIZE=disk, MACHINE_TYPE=machine,
                              MAC_ADDRESS='02:11:22:33:44:55', IP_CIDR='192.0.2.33/24',
                              GATEWAY='192.0.2.1', DNS_SERVERS='192.0.2.2,192.0.2.3', DOMAIN='example.test',
                              SSH_PUBLIC_KEY=key)
                params.pop('IPAM_URL')
                vm = process('templates/rhel-vm.yaml', params)['items'][0]
                self.assertEqual(vm['spec']['instancetype']['name'], 'poc-' + size)
                storage = vm['spec']['dataVolumeTemplates'][0]['spec']['storage']
                self.assertEqual(storage['resources']['requests']['storage'], disk)
                self.assertEqual(storage['volumeMode'], 'Block')
                domain = vm['spec']['template']['spec']['domain']
                self.assertEqual(domain['machine']['type'], machine)
                self.assertEqual(domain['firmware']['bootloader'], {'bios': {}})
                self.assertEqual(domain['devices']['disks'][0]['bootOrder'], 1)
                self.assertEqual(domain['devices']['disks'][0]['disk']['bus'], 'virtio')
                cloud = vm['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']
                user, network = yaml.safe_load(cloud['userData']), yaml.safe_load(cloud['networkData'])
                self.assertEqual(user['users'], [])
                self.assertEqual(user['write_files'][0]['content'], key)
                self.assertIn("user='guestops'", user['runcmd'][0])
                self.assertFalse(user['preserve_hostname'])
                for field in ('ssh_pwauth', 'chpasswd', 'password', 'sudo', 'groups'):
                    self.assertNotIn(field, user)
                self.assertEqual(network['ethernets']['lan']['nameservers']['addresses'], ['192.0.2.2', '192.0.2.3'])

    def run_preflight(self, request=None, failure='', present='', source='Bound|approved-storage|Block|104Gi', extra_managed=''):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'bin').mkdir()
            log = p / 'calls'
            managed = yaml.safe_load((ROOT / 'templates/rhel-vm.yaml').read_text())['objects'][0]['metadata']['labels']
            fake = '''import os, sys
args=' '.join(sys.argv[1:])
with open(os.environ['CALLS'],'a') as f: f.write(args+'\\n')
if os.environ.get('FAIL') and os.environ['FAIL'] in args: sys.exit(1)
if 'auth can-i' in args: print('yes')
elif 'get templates.' in args and 'go-template=' in args: print(os.environ['MANAGED'])
elif 'get templates.' in args or 'get virtualmachineinstancetypes.' in args or 'get network-attachment-definitions.' in args: print('present')
elif 'get datasources.' in args: print('True|golden-root|image-sources')
elif 'get persistentvolumeclaims golden-root ' in args: print(os.environ['SOURCE'])
elif os.environ.get('PRESENT') and os.environ['PRESENT'] in args: print('present')
elif 'get virtualmachines.' in args or '-root --ignore-not-found' in args: pass
else: sys.exit(97)
'''
            (p / 'bin/oc').write_text('#!' + sys.executable + ' -S\n' + fake)
            (p / 'bin/oc').chmod(0o755)
            task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())
            script = next(x['script'] for x in task['spec']['steps'] if x['name'] == 'preflight')
            env = dict(SITE, PATH=str(p / 'bin') + ':' + os.environ['PATH'], SCRATCH=tmp, CALLS=str(log),
                       FAIL=failure, PRESENT=present, SOURCE=source, REQ_NAME='web', REQ_SIZE='small', REQ_COUNT='1',
                       MANAGED='\n'.join(managed) + '\n' + extra_managed)
            env.update(request or {})
            run = subprocess.run(['bash', '-c', script], env=env, cwd=tmp, text=True, capture_output=True)
            names = (p / 'names').read_text().splitlines() if (p / 'names').exists() else []
            calls = log.read_text() if log.exists() else ''
            labels = (p / 'labels').read_text().splitlines() if (p / 'labels').exists() else []
            self.preflight_disk = (p / 'disk-size').read_text().strip() if (p / 'disk-size').exists() else None
            self.assertFalse((p / 'unexpected').exists(), 'label input must not execute commands')
            return run, names, calls, labels

    def test_golden_source_fits_each_size_and_exact_limits_are_checked(self):
        for size, disk in SIZES:
            for source_size in ('104Gi', disk):
                run, names, _, _ = self.run_preflight({'REQ_SIZE': size}, source=f'Bound|approved-storage|Block|{source_size}')
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(self.preflight_disk, disk)
                self.assertEqual(names, ['web'])
        for size, too_big in [('small', '111Gi'), ('medium', '501Gi'), ('large', '1025Gi')]:
            run, names, _, _ = self.run_preflight({'REQ_SIZE': size}, source=f'Bound|approved-storage|Block|{too_big}')
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('larger than target', run.stderr)
            self.assertFalse(names)

    def test_batch_naming(self):
        for count in range(1, 6):
            run, names, calls, _ = self.run_preflight({'REQ_COUNT': str(count)})
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(names, ['web'] if count == 1 else [f'web-{i:03d}' for i in range(1, count + 1)])
            self.assertNotIn(' get namespaces', calls)

    def test_bad_requests_fail_before_api(self):
        for request in ({'REQ_NAME': 'BAD'}, {'REQ_COUNT': '0'}, {'REQ_COUNT': '6'}, {'REQ_SIZE': 'huge'},
                        {'GUEST_USER': 'unsafe\nname'}, {'GUEST_USER': 'root'},
                        {'MACHINE_TYPE': ''}, {'MACHINE_TYPE': 'bad;name'},
                        {'CLOUD_USER_SUDO': 'ALL=(ALL) NOPASSWD:ALL'}):
            run, names, calls, _ = self.run_preflight(request)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(names)
            self.assertFalse(calls)

    def test_duplicates_or_orphan_disks_stop(self):
        for present in ('get virtualmachines.kubevirt.io web ', 'get datavolumes.cdi.kubevirt.io web-root ',
                        'get persistentvolumeclaims web-root '):
            run, names, _, _ = self.run_preflight(present=present)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(names)

    def test_api_failure_never_means_free(self):
        run, names, _, _ = self.run_preflight(failure='get virtualmachines.')
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(names)

    def test_source_mismatch_stops(self):
        for source in ('Pending|approved-storage|Block|104Gi', 'Bound|other-storage|Block|104Gi',
                       'Bound|approved-storage|Filesystem|104Gi', 'Bound|approved-storage|Block|111Gi'):
            run, names, _, _ = self.run_preflight(source=source)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse(names)

    def test_optional_labels_and_whitespace_normalization(self):
        for raw, expected in [('', []), ('   ', []), ('foo=bar', ['foo=bar']),
                              ('foo=bar;boo=far', ['foo=bar', 'boo=far']),
                              (' foo = bar ; example.com/team = ops ', ['foo=bar', 'example.com/team=ops']),
                              ('empty=;flag=false;number=123', ['empty=', 'flag=false', 'number=123']),
                              ('a' * 63 + '=' + 'v' * 63, ['a' * 63 + '=' + 'v' * 63])]:
            with self.subTest(raw=raw):
                run, names, _, labels = self.run_preflight({'REQ_LABELS': raw, 'REQ_COUNT': '2'})
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(names, ['web-001', 'web-002'])
                self.assertEqual(labels, expected)

    def test_invalid_labels_stop_before_any_api(self):
        bad = ['foo', '=bar', 'foo=bar=far', ';foo=bar', 'foo=bar;', 'foo=bar;;boo=far',
               'foo=one;foo=two', 'foo=one; foo = one', '/key=val', 'a/b/c=val',
               'Upper.example/key=val', 'a..example/key=val', '_key=val', 'key=-val',
               'a' * 64 + '=val', 'key=' + 'v' * 64, 'a' * 254 + '/key=val', 'key=hello world',
               'foo=bar\nboo=far', 'foo=bar\rboo=far', 'foo=$(touch unexpected)',
               'foo=`touch unexpected`', 'foo=bar;touch unexpected', 'foo=bar,boo=far']
        for raw in bad:
            with self.subTest(raw=raw):
                run, names, calls, labels = self.run_preflight({'REQ_LABELS': raw})
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(names)
                self.assertFalse(labels)
                self.assertFalse(calls)

    def test_template_managed_labels_are_protected_before_ipam(self):
        managed = yaml.safe_load((ROOT / 'templates/rhel-vm.yaml').read_text())['objects'][0]['metadata']['labels']
        for key in [*managed, 'platform.example.com/owner']:
            with self.subTest(key=key):
                run, names, calls, labels = self.run_preflight({'REQ_LABELS': f'foo=bar;{key}=override'},
                                                             extra_managed='platform.example.com/owner')
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('managed by the VM Template', run.stderr)
                self.assertFalse(names)
                self.assertFalse(labels)
                self.assertTrue(all(' get ' in c for c in calls.splitlines()))

    def test_failed_managed_label_read_stops_before_ipam(self):
        run, names, _, labels = self.run_preflight({'REQ_LABELS': 'foo=bar'}, failure='go-template=')
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(names)
        self.assertFalse(labels)

    def run_create(self, label_args, size='small', disk='110Gi'):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'bin').mkdir()
            (p / 'created').mkdir()
            (p / 'disk-size').write_text(disk + '\n')
            (p / 'labels').write_text(''.join(x + '\n' for x in label_args))
            (p / 'allocations').write_text(
                'web-001|192.0.2.33/24|02:11:22:33:44:55|192.0.2.1|192.0.2.2|example.test\n'
                'web-002|192.0.2.34/24|02:11:22:33:44:56|192.0.2.1|192.0.2.2|example.test\n')
            fake = '''import os, sys, shutil
from pathlib import Path
args=sys.argv[1:]
if args[0] in ('process','label'):
    assert '--local' in args
    os.execv(os.environ['REAL_OC'],[os.environ['REAL_OC'],*args])
elif 'get' in args and 'templates.template.openshift.io' in args:
    sys.stdout.write(Path(os.environ['VM_TEMPLATE']).read_text())
elif 'create' in args:
    src=Path(args[args.index('-f')+1])
    shutil.copyfile(src,Path(os.environ['CREATED'])/src.name)
else:
    sys.exit(97)
'''
            (p / 'bin/oc').write_text('#!' + sys.executable + ' -S\n' + fake)
            (p / 'bin/oc').chmod(0o755)
            task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())
            script = next(x['script'] for x in task['spec']['steps'] if x['name'] == 'create-vms')
            env = dict(SITE, PATH=str(p / 'bin') + ':' + os.environ['PATH'], HOME=tmp, KUBECONFIG=str(p / 'no-config'),
                       SCRATCH=tmp, REAL_OC=OC, VM_TEMPLATE=str(ROOT / 'templates/rhel-vm.yaml'),
                       CREATED=str(p / 'created'), REQ_SIZE=size, REQ_SSH_KEY=KEY)
            run = subprocess.run(['bash', '-c', script], env=env, cwd=tmp, text=True, capture_output=True)
            vms = []
            for path in sorted((p / 'created').iterdir()):
                doc = yaml.safe_load(path.read_text())
                vms.extend(doc['items'] if doc['kind'] == 'List' else [doc])
            return run, vms

    def test_create_step_labels_each_vm_before_creation(self):
        self.require_oc()
        for size, disk in SIZES:
            for labels in ([], ['foo=bar', 'boo=far', 'empty=', 'enabled=false', 'number=123']):
                with self.subTest(size=size, labels=labels):
                    run, vms = self.run_create(labels, size, disk)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(len(vms), 2)
                    for i, vm in enumerate(vms, 1):
                        expected = {'app.kubernetes.io/managed-by': 'tekton-vm-provision',
                                    'app.kubernetes.io/part-of': 'vm-pipelines',
                                    'vm-pipelines/size': size, 'vm-pipelines/os': 'rhel'}
                        expected.update(dict(x.split('=', 1) for x in labels))
                        self.assertEqual(vm['metadata']['name'], f'web-00{i}')
                        self.assertEqual(vm['metadata']['labels'], expected)
                        self.assertEqual(vm['spec']['template']['metadata']['labels'], {'kubevirt.io/domain': f'web-00{i}'})
                        self.assertNotIn('labels', vm['spec']['dataVolumeTemplates'][0]['metadata'])
                        storage = vm['spec']['dataVolumeTemplates'][0]['spec']['storage']
                        self.assertEqual(storage['resources']['requests']['storage'], disk)
                        self.assertEqual(storage['accessModes'], ['ReadWriteMany'])
                        cloud = vm['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']
                        user = yaml.safe_load(cloud['userData'])
                        self.assertEqual(user['users'], [])
                        self.assertEqual(user['write_files'][0]['content'], KEY)
                        network = yaml.safe_load(cloud['networkData'])['ethernets']['lan']
                        self.assertEqual(network['addresses'], [f'192.0.2.{32 + i}/24'])
                        self.assertEqual(network['match']['macaddress'], f'02:11:22:33:44:{54 + i}')

    def test_create_step_refuses_to_overwrite_template_labels(self):
        self.require_oc()
        run, vms = self.run_create(['vm-pipelines/size=override'])
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(vms)
        self.assertIn('reservations remain', run.stderr)


if __name__ == '__main__':
    unittest.main()
