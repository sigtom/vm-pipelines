"""Offline WAPI contract and failure tests. No real IPAM endpoint is contacted."""
import io
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
ENV = dict(IPAM_URL='https://ipam.example.test', INFOBLOX_WAPI_VERSION='v2.12',
           INFOBLOX_USERNAME='test-user', INFOBLOX_PASSWORD='TEST_ONLY',
           INFOBLOX_NETWORK='192.0.2.0/24', INFOBLOX_NETWORK_VIEW='default',
           INFOBLOX_DNS_VIEW='default', GATEWAY='192.0.2.1',
           DNS_SERVERS='192.0.2.2,192.0.2.3', DOMAIN='example.test',
           STATIC_START='192.0.2.32', STATIC_END='192.0.2.47',
           DHCP_START='192.0.2.50', DHCP_END='192.0.2.200')


class IpamTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.facts = ipam.network_settings('192.0.2.0/24','192.0.2.1',
            '192.0.2.2,192.0.2.3','example.test','192.0.2.32','192.0.2.47',
            '192.0.2.50','192.0.2.200')
        self.facts['network_view'] = 'default'
        # Individual API tests are mocked; printed receipts are harmless test fixtures.

    def test_ssh_wire_format_and_quoted_comment(self):
        self.assertEqual(ipam.validate_ssh_key(KEY + ' say "hello": # test'), KEY + ' say "hello": # test')
        for key in ('ssh-ed25519 AAAA', KEY+'\n', KEY+'\nother', 'ssh-rsa AAAA', ''):
            with self.subTest(key=key), self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key)

    def test_unknown_command_cannot_allocate(self):
        with patch.object(ipam, 'request') as call, self.assertRaises(ipam.AllocationError):
            ipam.main(['unknown'])
        call.assert_not_called()

    def test_valid_network_and_multiple_dns(self):
        self.assertEqual(self.facts['dns'], '192.0.2.2,192.0.2.3')
        self.assertEqual(str(self.facts['net']), '192.0.2.0/24')

    def test_network_rejects_conflicts(self):
        cases = [('192.0.2.32','192.0.2.210','192.0.2.1'),
                 ('192.0.2.0','192.0.2.47','192.0.2.1'),
                 ('192.0.2.32','192.0.2.255','192.0.2.1'),
                 ('192.0.2.40','192.0.2.32','192.0.2.1'),
                 ('192.0.2.32','192.0.2.47','192.0.2.34')]
        for start,end,gateway in cases:
            with self.subTest(start=start,end=end), self.assertRaises(ipam.AllocationError):
                ipam.network_settings('192.0.2.0/24', gateway, '192.0.2.2',
                    'example.test',start,end,'192.0.2.50','192.0.2.200')

    def test_dhcp_declaration_required(self):
        for value in ('', 'false','FALSE', '1'):
            env = dict(ENV, DHCP_DISABLED=value)
            env.pop('DHCP_START'); env.pop('DHCP_END')
            with patch.dict(os.environ, env, clear=True), patch.object(ipam,'request') as call:
                with self.assertRaises(ipam.AllocationError):
                    ipam.infoblox_preflight(['vm-01'])
                call.assert_not_called()

    def test_dhcp_disabled_cannot_conflict_with_range(self):
        with patch.dict(os.environ, {'DHCP_DISABLED':'true'}), self.assertRaises(ipam.AllocationError):
            ipam.infoblox_preflight(['vm-01'])

    def test_url_and_missing_fields_fail_before_api(self):
        for key,value in [('IPAM_URL','http://ipam.example.test'),
                          ('IPAM_URL','https://user:password@ipam.example.test'),
                          ('IPAM_URL','https://ipam.example.test/wapi'),
                          ('INFOBLOX_NETWORK_VIEW','REPLACE_ME'),
                          ('INFOBLOX_WAPI_VERSION','bad')]:
            with self.subTest(key=key,value=value), patch.dict(os.environ,{key:value}), patch.object(ipam,'request') as call:
                with self.assertRaises(ipam.AllocationError):
                    ipam.infoblox_preflight(['vm-01'])
                call.assert_not_called()

    def test_version_normalized_and_search_uses_get(self):
        for ver in ('2.12','v2.12'):
            with patch.dict(os.environ,{'INFOBLOX_WAPI_VERSION':ver}), patch.object(ipam,'request',return_value=[]) as call:
                ipam.infoblox_preflight(['vm-01','vm-02'])
                self.assertEqual(call.call_count,2)
                for args in call.call_args_list:
                    self.assertEqual(args.args[0],'https://ipam.example.test/wapi/v2.12')
                    self.assertEqual(args.args[1],'GET')

    def test_duplicate_or_malformed_search_stops(self):
        for result in ([{'name':'vm-01.example.test'}], {}, None):
            with patch.object(ipam,'request',return_value=result), self.assertRaises(ipam.AllocationError):
                ipam.infoblox_preflight(['vm-01'])

    def test_bounded_host_allocation_and_readback(self):
        call = Mock(side_effect=['record:host/test:vm-01.example.test/default',
                                 {'ipv4addrs':[{'ipv4addr':'192.0.2.34'}]}])
        result = ipam.infoblox_allocate(call,self.facts,'default','vm-01','02:11:22:33:44:55')
        self.assertEqual(result,'192.0.2.34/24')
        body=call.call_args_list[0].args[2]
        self.assertEqual(body['network_view'],'default')
        self.assertEqual(body['view'],'default')
        self.assertEqual(body['ipv4addrs'][0]['ipv4addr'], 'func:nextavailableip:192.0.2.32-192.0.2.47,default')
        self.assertEqual([c.args[0] for c in call.call_args_list],['POST','GET'])

    def test_malformed_or_out_of_pool_answer_never_accepted(self):
        for response in ({}, {'ipv4addrs':[]}, {'ipv4addrs':[{},{}]},
                         {'ipv4addrs':[{'ipv4addr':'192.0.2.201'}]},
                         {'ipv4addrs':[{'ipv4addr':3232235777}]}):
            call=Mock(side_effect=['record:host/test',response])
            with self.subTest(response=response), self.assertRaises(ipam.AllocationError):
                ipam.infoblox_allocate(call,self.facts,'default','vm-01','02:11:22:33:44:55')
            self.assertEqual(call.call_count,2)  # No retries or deletes.

    def test_failed_insert_never_retried(self):
        call=Mock(side_effect=ipam.AllocationError('HTTP 403'))
        with self.assertRaises(ipam.AllocationError):
            ipam.infoblox_allocate(call,self.facts,'default','vm-01','02:11:22:33:44:55')
        self.assertEqual(call.call_count,1)

    def test_http_body_is_not_logged(self):
        error=urllib.error.HTTPError('https://ipam.example.test',400,'Bad',{},io.BytesIO(b'SECRET_BODY'))
        with patch.object(ipam.OPENER,'open',side_effect=error), self.assertRaises(ipam.AllocationError) as raised:
            ipam.request('https://ipam.example.test','POST','record:host',{},user='user',password='SECRET_BODY')
        self.assertNotIn('SECRET_BODY',str(raised.exception))
        self.assertIn('400',str(raised.exception))

    def test_redirects_not_followed(self):
        self.assertIsNone(ipam._NoRedirect().redirect_request(None,None,302,'',{},'https://other.example.test'))

    def test_bad_names_do_not_reach_ipam(self):
        for data in ('vm-01\nvm-01\n','BAD_NAME\n','', '\n'.join(f'vm-0{i}' for i in range(6))):
            with tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp); (p/'names').write_text(data)
                with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'receipt')}), patch.object(ipam,'infoblox_preflight') as pre:
                    with self.assertRaises(ipam.AllocationError): ipam.main()
                    pre.assert_not_called()

    def test_partial_batch_retains_receipt_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'names').write_text('vm-01\nvm-02\n')
            with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'receipt')}), patch.object(ipam,'infoblox_preflight',return_value=(Mock(),self.facts,'default')), patch.object(ipam,'infoblox_allocate',side_effect=['192.0.2.33/24',ipam.AllocationError('failed')]):
                with self.assertRaises(ipam.AllocationError): ipam.main()
            self.assertIn('vm-01|192.0.2.33/24|',(p/'receipt').read_text())
            self.assertNotIn('vm-02|',(p/'receipt').read_text())

    def test_duplicate_address_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'names').write_text('vm-01\nvm-02\n')
            with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'receipt')}), patch.object(ipam,'infoblox_preflight',return_value=(Mock(),self.facts,'default')), patch.object(ipam,'infoblox_allocate',return_value='192.0.2.33/24'):
                with self.assertRaises(ipam.AllocationError): ipam.main()
            self.assertEqual(len((p/'receipt').read_text().splitlines()),1)

    def test_existing_receipt_prevents_any_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/'names').write_text('vm-01\n'); (p/'receipt').write_text('keep')
            with patch.dict(os.environ,{'VM_NAMES_FILE':str(p/'names'),'ALLOCATIONS_FILE':str(p/'receipt')}), patch.object(ipam,'infoblox_preflight') as call:
                with self.assertRaises(FileExistsError): ipam.main()
                call.assert_not_called()
            self.assertEqual((p/'receipt').read_text(),'keep')


if __name__=='__main__': unittest.main()
