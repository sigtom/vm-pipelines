"""Native template rendering plus real Task scripts with a fake API CLI.

No cluster login or provisioning. REQUIRE_OC=1 makes missing oc a test error in CI.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml  # Test dependency only.

ROOT=Path(__file__).resolve().parents[1]
OC=shutil.which('oc')
KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea'
SITE=dict(VM_NAMESPACE='vm-provisioning', NETWORK_ATTACHMENT='approved-network',
          DATASOURCE_NAME='rhel-golden', DATASOURCE_NAMESPACE='image-sources',
          STORAGE_CLASS='approved-storage', ACCESS_MODE='ReadWriteMany',
          VOLUME_MODE='Filesystem', GUEST_USER='cloud-user', CLOUD_USER_SUDO='false',
          IPAM_URL='https://ipam.example.test')


def process(file, params):
    command=[OC,'process','--local','-f',str(ROOT/file),'-o','json']
    for k,v in params.items(): command += ['-p',f'{k}={v}']
    return json.loads(subprocess.check_output(command,text=True))


class NativeTests(unittest.TestCase):
    def test_all_yaml_and_shell_parse(self):
        paths=list(ROOT.rglob('*.yaml'))
        for path in paths: list(yaml.safe_load_all(path.read_text()))
        task=yaml.safe_load((ROOT/'tekton/task.yaml').read_text())
        for step in task['spec']['steps']:
            result=subprocess.run(['bash','-n'],input=step['script'],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_runtime_credentials_and_identity(self):
        task=yaml.safe_load((ROOT/'tekton/task.yaml').read_text())
        for step in task['spec']['steps']:
            secrets=[x for x in step.get('envFrom',[]) if 'secretRef' in x]
            self.assertEqual(bool(secrets),step['name']=='reserve-ip')
        self.assertEqual({x['name']:x['value'] for x in task['spec']['stepTemplate']['env']},
                         {'HOME':'/tmp','DOCKER_CONFIG':'/tmp/.docker'})
        docs=list(yaml.safe_load_all((ROOT/'tekton/serviceaccount.yaml').read_text()))
        self.assertFalse(any(x['kind'].startswith('Cluster') for x in docs))
        pipeline=yaml.safe_load((ROOT/'tekton/pipeline.yaml').read_text())
        self.assertEqual(len(pipeline['spec']['params']),4)

    def test_kustomize(self):
        if not OC:
            if os.environ.get('REQUIRE_OC')=='1': self.fail('oc required in CI')
            self.skipTest('oc not installed')
        for directory in ('catalog','tekton','build'):
            data=subprocess.check_output([OC,'kustomize',directory],cwd=ROOT,text=True)
            self.assertTrue(list(yaml.safe_load_all(data)))

    def test_request_form_native_processing(self):
        if not OC:
            if os.environ.get('REQUIRE_OC')=='1': self.fail('oc required in CI')
            self.skipTest('oc not installed')
        key=KEY+' ops "comment": #test'
        run=process('tekton/request-template.yaml',dict(VM_NAME='web-01',SIZE='small',COUNT='2',SSH_PUBLIC_KEY=key))['items'][0]
        self.assertEqual(run['spec']['pipelineRef']['name'],'vm-provision')
        self.assertEqual(run['spec']['taskRunTemplate']['serviceAccountName'],'vm-provision')
        self.assertEqual(run['spec']['workspaces'],[{'name':'scratch','emptyDir':{}}])
        self.assertEqual({p['name']:p['value'] for p in run['spec']['params']}['ssh-public-key'],key)

    def test_three_vm_sizes_and_embedded_cloud_init(self):
        if not OC:
            if os.environ.get('REQUIRE_OC')=='1': self.fail('oc required in CI')
            self.skipTest('oc not installed')
        for size,disk in [('small','40Gi'),('medium','80Gi'),('large','160Gi')]:
            for sudo in ('false','ALL=(ALL) NOPASSWD:ALL'):
                key=KEY+' ops "quoted": #comment ${NOT_A_PARAM}'
                params=dict(SITE,VM_NAME='web-01',VM_SIZE=size,INSTANCE_TYPE='poc-'+size,
                            DISK_SIZE=disk,MAC_ADDRESS='02:11:22:33:44:55',IP_CIDR='192.0.2.33/24',
                            GATEWAY='192.0.2.1',DNS_SERVERS='192.0.2.2,192.0.2.3',DOMAIN='example.test',
                            SUDO_POLICY=sudo,SSH_PUBLIC_KEY=key)
                params.pop('IPAM_URL'); params.pop('CLOUD_USER_SUDO')
                vm=process('templates/rhel-vm.yaml',params)['items'][0]
                self.assertEqual(vm['spec']['instancetype']['name'],'poc-'+size)
                self.assertEqual(vm['spec']['dataVolumeTemplates'][0]['spec']['storage']['resources']['requests']['storage'],disk)
                cloud=vm['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']
                user=yaml.safe_load(cloud['userData']); network=yaml.safe_load(cloud['networkData'])
                self.assertEqual(user['users'][0]['ssh_authorized_keys'],[key])
                self.assertEqual(user['users'][0]['sudo'],False if sudo=='false' else sudo)
                self.assertEqual(network['ethernets']['lan']['nameservers']['addresses'],['192.0.2.2','192.0.2.3'])

    def run_preflight(self, request=None, failure='', present='', source='Bound|approved-storage|Filesystem|30Gi'):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'bin').mkdir(); log=p/'calls'
            fake='''import os, sys
from pathlib import Path
args=' '.join(sys.argv[1:])
with open(os.environ['CALLS'],'a') as f: f.write(args+'\\n')
if os.environ.get('FAIL') and os.environ['FAIL'] in args: sys.exit(1)
if 'auth can-i' in args: print('yes')
elif 'get templates.' in args or 'get virtualmachineinstancetypes.' in args or 'get network-attachment-definitions.' in args: print('present')
elif 'get datasources.' in args: print('True|golden-root|image-sources')
elif 'get persistentvolumeclaims golden-root ' in args: print(os.environ['SOURCE'])
elif os.environ.get('PRESENT') and os.environ['PRESENT'] in args: print('present')
elif 'get virtualmachines.' in args or '-root --ignore-not-found' in args: pass
else: sys.exit(97)
'''
            (p/'bin/oc').write_text('#!'+sys.executable+' -S\n'+fake); (p/'bin/oc').chmod(0o755)
            task=yaml.safe_load((ROOT/'tekton/task.yaml').read_text())
            script=next(x['script'] for x in task['spec']['steps'] if x['name']=='preflight')
            env=dict(SITE, PATH=str(p/'bin')+':'+os.environ['PATH'],SCRATCH=tmp,CALLS=str(log),
                     FAIL=failure,PRESENT=present,SOURCE=source,REQ_NAME='web',REQ_SIZE='small',REQ_COUNT='1')
            env.update(request or {})
            run=subprocess.run(['bash','-c',script],env=env,cwd=tmp,text=True,capture_output=True)
            names=(p/'names').read_text().splitlines() if (p/'names').exists() else []
            calls=log.read_text() if log.exists() else ''
            return run,names,calls

    def test_batch_naming(self):
        for count in range(1,6):
            run,names,calls=self.run_preflight({'REQ_COUNT':str(count)})
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(names,['web'] if count==1 else [f'web-{i:03d}' for i in range(1,count+1)])
            self.assertNotIn(' get namespaces',calls)

    def test_bad_requests_fail_before_api(self):
        for request in ({'REQ_NAME':'BAD'},{'REQ_COUNT':'0'},{'REQ_COUNT':'6'},{'REQ_SIZE':'huge'},
                        {'GUEST_USER':'unsafe\nname'},{'CLOUD_USER_SUDO':'ALL=(ALL) ALL'}):
            run,names,calls=self.run_preflight(request)
            self.assertNotEqual(run.returncode,0); self.assertFalse(names); self.assertFalse(calls)

    def test_duplicates_or_orphan_disks_stop(self):
        for present in ('get virtualmachines.kubevirt.io web ','get datavolumes.cdi.kubevirt.io web-root ',
                        'get persistentvolumeclaims web-root '):
            run,names,_=self.run_preflight(present=present)
            self.assertNotEqual(run.returncode,0); self.assertFalse(names)

    def test_api_failure_never_means_free(self):
        run,names,_=self.run_preflight(failure='get virtualmachines.')
        self.assertNotEqual(run.returncode,0); self.assertFalse(names)

    def test_source_mismatch_stops(self):
        for source in ('Pending|approved-storage|Filesystem|30Gi','Bound|other-storage|Filesystem|30Gi',
                       'Bound|approved-storage|Block|30Gi','Bound|approved-storage|Filesystem|50Gi'):
            run,names,_=self.run_preflight(source=source)
            self.assertNotEqual(run.returncode,0); self.assertFalse(names)


if __name__=='__main__': unittest.main()
