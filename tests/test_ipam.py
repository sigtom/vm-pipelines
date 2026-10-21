"""Offline AAP API/contract tests. No job or address is created on a real system."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ipam

KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea'
ENV = dict(AAP_API_BASE_URL='https://aap.example.test/api/controller/v2',
           AAP_JOB_TEMPLATE_ID='42', AAP_TOKEN='TEST_ONLY', NETWORK_PROFILE='app-network',
           AAP_JOB_TIMEOUT_SECONDS='600', AAP_POLL_INTERVAL_SECONDS='5')
SENT = dict(request_id='run-uid/web-01/lan', vm_name='web-01',
            mac_address='02:11:22:33:44:55', network_profile='app-network')
TEMPLATE = dict(job_type='run', job_slice_count=1, ask_variables_on_launch=True)


def result(sent=None, ip='192.0.2.40/24'):
    sent = SENT if sent is None else sent
    return dict(sent, schema_version=1, fqdn=sent['vm_name']+'.example.test',
                ip_cidr=ip, gateway='192.0.2.1', dns_servers=['192.0.2.2','192.0.2.3'],
                domain='example.test')


def job(status='successful', artifacts=None):
    return dict(id=71, job_template=42, status=status,
                artifacts={'vm_network':result()} if artifacts is None else artifacts)


class IpamTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = ipam.settings()
        self.stdout = io.StringIO()
        quiet = contextlib.redirect_stdout(self.stdout)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_ssh_wire_format_and_comments(self):
        self.assertEqual(ipam.validate_ssh_key(KEY+' say "hi": #test'),KEY+' say "hi": #test')
        for key in ('ssh-ed25519 AAAA', KEY+'\n', 'ssh-rsa AAAA', ''):
            with self.subTest(key=key), self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key)

    def test_offline_key_validation_needs_no_aap(self):
        with patch.dict(os.environ, {'REQ_SSH_KEY':KEY},clear=True), patch.object(ipam,'request') as remote:
            ipam.main(['check-ssh-key'])
            remote.assert_not_called()

    def test_bad_config_is_rejected(self):
        for key,value in [('AAP_API_BASE_URL','http://aap.example.test/api/v2'),
                          ('AAP_API_BASE_URL','https://user:pass@aap.example.test/api/v2'),
                          ('AAP_API_BASE_URL','https://aap.example.test/not-an-api'),
                          ('AAP_API_BASE_URL','https://aap.example.test/api/v2?token=test'),
                          ('AAP_JOB_TEMPLATE_ID','0'),('AAP_JOB_TEMPLATE_ID','REPLACE_ME'),
                          ('AAP_TOKEN','bad\nvalue'),('NETWORK_PROFILE','app/../../network'),
                          ('AAP_JOB_TIMEOUT_SECONDS','0'),('AAP_POLL_INTERVAL_SECONDS','999')]:
            with self.subTest(key=key),patch.dict(os.environ,{key:value}),self.assertRaises(ipam.AllocationError):
                ipam.settings()

    def test_gateway_and_direct_controller_bases(self):
        for suffix in ('/api/v2/','/api/controller/v2/'):
            with patch.dict(os.environ,{'AAP_API_BASE_URL':'https://aap.example.test'+suffix}):
                self.assertEqual(ipam.settings()['base'],'https://aap.example.test'+suffix.rstrip('/'))

    def test_bearer_request_uses_exact_url_and_body(self):
        response=Mock(); response.read.return_value=b'{"job":71}'
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
        with patch.object(ipam.OPENER,'open',return_value=response) as opened:
            value=ipam.request(self.config,'POST','job_templates/42/launch/',{'extra_vars':SENT})
        req=opened.call_args.args[0]
        self.assertEqual(req.full_url,'https://aap.example.test/api/controller/v2/job_templates/42/launch/')
        self.assertEqual(req.get_header('Authorization'),'Bearer TEST_ONLY')
        self.assertEqual(json.loads(req.data),{'extra_vars':SENT})
        self.assertEqual(value,{'job':71})
        opened.assert_called_once()

    def test_http_errors_and_timeouts_are_not_retried_or_leaked(self):
        for error in [urllib.error.HTTPError('https://aap.example.test',403,'secret',{},io.BytesIO(b'TEST_ONLY')),
                      TimeoutError('TEST_ONLY'),urllib.error.URLError('TEST_ONLY')]:
            with patch.object(ipam.OPENER,'open',side_effect=error) as opened,self.assertRaises(ipam.AllocationError) as raised:
                ipam.request(self.config,'POST','job_templates/42/launch/',{'extra_vars':SENT})
            self.assertNotIn('TEST_ONLY',str(raised.exception)); opened.assert_called_once()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(ipam._NoRedirect().redirect_request(None,None,302,'',{},'https://other.example.test'))

    def test_non_json_or_wrong_response_shape_is_rejected(self):
        for raw in (b'<html>login</html>',b'[]',b'null',b'x'*(2*1024*1024+1)):
            response=Mock(); response.read.return_value=raw
            response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
            with patch.object(ipam.OPENER,'open',return_value=response),self.assertRaises(ipam.AllocationError):
                ipam.request(self.config,'GET','jobs/71/')

    def test_preflight_is_get_only(self):
        with patch.object(ipam,'request',side_effect=[TEMPLATE,{}]) as remote:
            ipam.preflight(self.config)
        self.assertEqual([c.args[1] for c in remote.call_args_list],['GET','GET'])

    def test_survey_must_accept_all_inputs(self):
        template=dict(TEMPLATE,ask_variables_on_launch=False,survey_enabled=True)
        spec={'spec':[{'variable':key} for key in ipam.INPUTS]}
        with patch.object(ipam,'request',side_effect=[template,spec,{}]):
            ipam.preflight(self.config)
        with patch.object(ipam,'request',side_effect=[template,{'spec':[]}]),self.assertRaises(ipam.AllocationError):
            ipam.preflight(self.config)

    def test_prompts_slicing_and_check_jobs_rejected(self):
        for changes in ({'job_type':'check'},{'job_slice_count':2},{'ask_variables_on_launch':False}):
            with patch.object(ipam,'request',return_value=dict(TEMPLATE,**changes)),self.assertRaises(ipam.AllocationError):
                ipam.preflight(self.config)
        for launch in ({'passwords_needed_to_start':['ssh_password']},{'inventory_needed_to_start':True},
                       {'variables_needed_to_start':['unrelated']},{'credential_needed_to_start':True}):
            with patch.object(ipam,'request',side_effect=[TEMPLATE,launch]),self.assertRaises(ipam.AllocationError):
                ipam.preflight(self.config)

    def test_check_aap_launches_no_job(self):
        with patch.object(ipam,'request',side_effect=[TEMPLATE,{}]) as remote:
            ipam.main(['check-aap'])
        self.assertTrue(all(c.args[1]=='GET' for c in remote.call_args_list))

    def test_valid_result_preserves_the_allocation_contract(self):
        self.assertEqual(ipam.validate_result(result(),SENT),
            ['web-01','192.0.2.40/24','02:11:22:33:44:55','192.0.2.1','192.0.2.2,192.0.2.3','example.test'])

    def test_result_correlation_is_required(self):
        for field in ipam.INPUTS:
            with self.subTest(field=field),self.assertRaises(ipam.AllocationError):
                ipam.validate_result(dict(result(),**{field:'wrong'}),SENT)

    def test_bad_result_network_and_schema_rejected(self):
        bad=[{'schema_version':True},{'schema_version':2},{'ip_cidr':'192.0.2.40'},
             {'ip_cidr':'192.0.2.0/24'},{'ip_cidr':'192.0.2.255/24'},{'ip_cidr':'::1/64'},
             {'ip_cidr':'192.0.2.40/32'},{'gateway':'198.51.100.1'},{'gateway':'192.0.2.40'},
             {'gateway':'192.0.2.255'},{'dns_servers':[]},{'dns_servers':'192.0.2.2'},
             {'dns_servers':['224.0.0.1']},{'domain':'bad|domain'},{'fqdn':'other.example.test'}]
        for changes in bad:
            with self.subTest(changes=changes),self.assertRaises(ipam.AllocationError):
                ipam.validate_result(dict(result(),**changes),SENT)
        with self.assertRaises(ipam.AllocationError): ipam.validate_result(None,SENT)

    def test_launch_wait_then_read_artifact(self):
        journal=io.StringIO()
        with patch.object(ipam,'request',side_effect=[{'job':71,'ignored_fields':{}},job('pending'),job('running'),job()]) as remote,patch.object(ipam.time,'sleep'):
            record=ipam.allocate(self.config,SENT,journal)
        self.assertEqual(record[1],'192.0.2.40/24')
        self.assertEqual(remote.call_args_list[0].args[3],{'extra_vars':SENT})
        self.assertEqual([c.args[1] for c in remote.call_args_list],['POST','GET','GET','GET'])
        self.assertEqual([json.loads(x)['event'] for x in journal.getvalue().splitlines()],['prepared','launched','validated'])

    def test_identity_is_recorded_before_launch(self):
        journal=io.StringIO()
        def lost(*args,**kwargs):
            self.assertIn(SENT['mac_address'],journal.getvalue())
            self.assertIn(SENT['request_id'],journal.getvalue())
            raise ipam.AllocationError('network error')
        with patch.object(ipam,'request',side_effect=lost) as remote,self.assertRaises(ipam.AllocationError):
            ipam.allocate(self.config,SENT,journal)
        remote.assert_called_once()

    def test_ignored_inputs_or_missing_job_id_stops(self):
        for launch in ({'job':71,'ignored_fields':{'extra_vars':SENT}}, {}, {'job':True}):
            journal=io.StringIO()
            with patch.object(ipam,'request',return_value=launch) as remote,self.assertRaises(ipam.AllocationError):
                ipam.allocate(self.config,SENT,journal)
            remote.assert_called_once()
            if launch.get('job')==71: self.assertIn('"job_id": 71',journal.getvalue())

    def test_failed_canceled_unknown_and_wrong_job_identity_stops(self):
        for value in [job(s) for s in ('failed','error','canceled','unknown')]+[dict(job(),id=72),dict(job(),job_template=43),job(artifacts={})]:
            with patch.object(ipam,'request',side_effect=[{'job':71},value]) as remote,self.assertRaises(ipam.AllocationError):
                ipam.allocate(self.config,SENT,io.StringIO())
            self.assertEqual(remote.call_count,2)

    def test_local_timeout_never_cancels_or_relaunches(self):
        with patch.object(ipam,'request',side_effect=[{'job':71}]) as remote,patch.object(ipam.time,'monotonic',side_effect=[0,601]),self.assertRaisesRegex(ipam.AllocationError,'NOT canceled'):
            ipam.allocate(self.config,SENT,io.StringIO())
        remote.assert_called_once()

    def test_poll_failure_keeps_job_receipt(self):
        journal=io.StringIO()
        with patch.object(ipam,'request',side_effect=[{'job':71},ipam.AllocationError('HTTP 503')]),self.assertRaises(ipam.AllocationError):
            ipam.allocate(self.config,SENT,journal)
        self.assertIn('"job_id": 71',journal.getvalue())

    def test_unknown_command_and_bad_names_never_call_aap(self):
        with patch.object(ipam,'request') as remote,self.assertRaises(ipam.AllocationError): ipam.main(['unknown'])
        remote.assert_not_called()
        for text in ('vm-01\nvm-01\n','BAD_NAME\n','','\n'.join(f'vm-0{i}' for i in range(6))):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp); (p/'names').write_text(text)
                with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'allocations'),'REQUEST_ID':'run-uid'}),patch.object(ipam,'request') as remote,self.assertRaises(ipam.AllocationError):
                    ipam.main()
                remote.assert_not_called()

    def run_batch(self,tmp,second='ok'):
        p=Path(tmp); (p/'names').write_text('web-01\nweb-02\n')
        sent={}; launched=[]
        def api(config,method,path,data=None,**kwargs):
            if path=='job_templates/42/': return TEMPLATE
            if method=='GET' and path.endswith('launch/'): return {}
            if method=='POST':
                number=71+len(launched); sent[number]=data['extra_vars']; launched.append(number)
                return {'job':number}
            number=int(path.split('/')[1]); status='failed' if number==72 and second=='fail' else 'successful'
            ip='192.0.2.40/24' if number==71 or second=='duplicate' else '192.0.2.41/24'
            return dict(id=number,job_template=42,status=status,artifacts={'vm_network':result(sent[number],ip)})
        with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'allocations'),'REQUEST_ID':'run-uid'}),patch.object(ipam,'request',side_effect=api):
            if second=='ok': ipam.main()
            else:
                with self.assertRaises(ipam.AllocationError): ipam.main()
        return p,launched

    def test_batch_result_files_and_per_vm_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,launched=self.run_batch(tmp)
            lines=(p/'allocations').read_text().splitlines()
            self.assertEqual(len(lines),2); self.assertEqual(launched,[71,72])
            entries=[json.loads(x) for x in (p/'allocations.requests.jsonl').read_text().splitlines()]
            prepared=[x for x in entries if x['event']=='prepared']
            self.assertEqual([x['request_id'] for x in prepared],['run-uid/web-01/lan','run-uid/web-02/lan'])
            for line,entry in zip(lines,prepared): self.assertEqual(line.split('|')[2],entry['mac_address'])
            self.assertNotIn('TEST_ONLY',(p/'allocations.requests.jsonl').read_text())

    def test_partial_failure_or_duplicate_keeps_only_first_allocation(self):
        for failure in ('fail','duplicate'):
            with tempfile.TemporaryDirectory() as tmp:
                p,_=self.run_batch(tmp,failure)
                self.assertEqual(len((p/'allocations').read_text().splitlines()),1)
                self.assertIn('"job_id": 72',(p/'allocations.requests.jsonl').read_text())

    def test_existing_receipt_prevents_relaunch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'names').write_text('web-01\n'); (p/'allocations').write_text('keep')
            with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'allocations'),'REQUEST_ID':'run-uid'}),patch.object(ipam,'request') as remote,self.assertRaises(FileExistsError):
                ipam.main()
            remote.assert_not_called(); self.assertEqual((p/'allocations').read_text(),'keep')


if __name__=='__main__': unittest.main()
