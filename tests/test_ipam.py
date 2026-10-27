"""Offline tests of the tested IPAM-only WAPI contract. No live requests."""
import contextlib
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
REF = 'record:host/test:vm-01/%20'
ENV = dict(IPAM_URL='https://ipam.example.test', INFOBLOX_WAPI_VERSION='v2.13.6',
           INFOBLOX_USERNAME='test-user', INFOBLOX_PASSWORD='TEST_ONLY',
           INFOBLOX_ALLOCATION_MODE='subnet', INFOBLOX_NETWORK='192.0.2.0/25',
           INFOBLOX_NETWORK_VIEW='default', GATEWAY='192.0.2.1',
           DNS_SERVERS='192.0.2.2,192.0.2.3', DOMAIN='example.test')


def host(ip='192.0.2.9', **changes):
    data = dict(name='vm-01', network_view='default', configure_for_dns=False,
                ipv4addrs=[dict(ipv4addr=ip, configure_for_dhcp=False, host='vm-01')])
    data.update(changes)
    return data


class IpamTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.facts = ipam.network_settings('192.0.2.0/25', '192.0.2.1',
                                           '192.0.2.2,192.0.2.3', 'example.test')
        self.output = io.StringIO()
        quiet = contextlib.redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_ssh_wire_format_and_quoted_comment(self):
        self.assertEqual(ipam.validate_ssh_key(KEY + ' say "hello": # test'), KEY + ' say "hello": # test')
        for key in ('ssh-ed25519 AAAA', KEY + '\n', KEY + '\nother', 'ssh-rsa AAAA', ''):
            with self.subTest(key=key), self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key)

    def test_offline_commands_need_no_settings_or_http(self):
        with patch.dict(os.environ, {'REQ_SSH_KEY': KEY}, clear=True), patch.object(ipam, 'request') as call:
            ipam.main(['check-ssh-key'])
            ipam.main(['--version'])
            call.assert_not_called()
        self.assertIn('vm-ipam 0.4', self.output.getvalue())

    def test_unknown_command_cannot_allocate(self):
        for args in (['unknown'], ['check-ipam'], ['check-ipam', 'one', 'two']):
            with patch.object(ipam, 'request') as call, self.assertRaises(ipam.AllocationError):
                ipam.main(args)
            call.assert_not_called()

    def test_no_dns_view_range_or_dhcp_declaration_needed(self):
        with patch.object(ipam, 'request', return_value=[]) as call:
            _, facts, view = ipam.infoblox_preflight(['vm-01'])
        self.assertEqual(view, 'default')
        self.assertEqual(facts['dns'], '192.0.2.2,192.0.2.3')
        self.assertEqual(str(facts['net']), '192.0.2.0/25')
        call.assert_called_once()

    def test_explicit_subnet_mode_is_required_before_any_request(self):
        for mode in ('', 'range', 'SUBNET', 'REPLACE_ME', 'false'):
            with patch.dict(os.environ, {'INFOBLOX_ALLOCATION_MODE': mode}), patch.object(ipam, 'request') as call:
                with self.assertRaises(ipam.AllocationError):
                    ipam.infoblox_preflight(['vm-01'])
                call.assert_not_called()

    def test_old_fields_cannot_change_explicit_subnet_mode(self):
        old = dict(INFOBLOX_DNS_VIEW='REPLACE_ME', STATIC_START='REPLACE_ME', STATIC_END='REPLACE_ME',
                   DHCP_START='REPLACE_ME', DHCP_END='REPLACE_ME', DHCP_DISABLED='false')
        with patch.dict(os.environ, old), patch.object(ipam, 'request', return_value=[]) as call:
            ipam.infoblox_preflight(['vm-01'])
        self.assertNotIn('view=', call.call_args.args[2].replace('network_view=', ''))

    def test_invalid_network_settings(self):
        cases = [('192.0.2.1/25', '192.0.2.1', '192.0.2.2', 'example.test'),
                 ('192.0.2.0/31', '192.0.2.1', '192.0.2.2', 'example.test'),
                 ('192.0.2.0/25', '192.0.3.1', '192.0.2.2', 'example.test'),
                 ('192.0.2.0/25', '192.0.2.127', '192.0.2.2', 'example.test'),
                 ('192.0.2.0/25', '192.0.2.1', '', 'example.test'),
                 ('192.0.2.0/25', '192.0.2.1', '224.0.0.1', 'example.test'),
                 ('192.0.2.0/25', '192.0.2.1', '192.0.2.2', 'bad|domain')]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ipam.AllocationError):
                ipam.network_settings(*values)

    def test_url_and_missing_fields_fail_before_api(self):
        for key, value in [('IPAM_URL', 'http://ipam.example.test'),
                           ('IPAM_URL', 'https://user:password@ipam.example.test'),
                           ('IPAM_URL', 'https://ipam.example.test/wapi'),
                           ('INFOBLOX_NETWORK_VIEW', 'REPLACE_ME'),
                           ('INFOBLOX_NETWORK_VIEW', 'view,other'),
                           ('INFOBLOX_WAPI_VERSION', 'bad')]:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), patch.object(ipam, 'request') as call:
                with self.assertRaises(ipam.AllocationError):
                    ipam.infoblox_preflight(['vm-01'])
                call.assert_not_called()

    def test_version_normalized_and_short_name_network_view_search(self):
        for ver in ('2.13.6', 'v2.13.6'):
            with patch.dict(os.environ, {'INFOBLOX_WAPI_VERSION': ver}), patch.object(ipam, 'request', return_value=[]) as call:
                ipam.infoblox_preflight(['vm-01', 'vm-02'])
            self.assertEqual(call.call_count, 2)
            for i, entry in enumerate(call.call_args_list, 1):
                self.assertEqual(entry.args[:3], ('https://ipam.example.test/wapi/v2.13.6', 'GET',
                                                  f'record:host?name=vm-0{i}&network_view=default'))

    def test_duplicate_or_malformed_search_stops(self):
        for result in ([{'name': 'vm-01'}], {}, None, '[]'):
            with patch.object(ipam, 'request', return_value=result), self.assertRaises(ipam.AllocationError):
                ipam.infoblox_preflight(['vm-01'])

    def test_check_ipam_is_get_only_and_writes_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'receipt'
            with patch.dict(os.environ, {'ALLOCATIONS_FILE': str(out)}), patch.object(ipam, 'request', return_value=[]) as call:
                ipam.main(['check-ipam', 'vm-01'])
            self.assertFalse(out.exists())
            self.assertEqual([c.args[1] for c in call.call_args_list], ['GET'])

    def test_exact_ipam_only_payload_and_encoded_readback(self):
        call = Mock(side_effect=[REF, host()])
        result = ipam.infoblox_allocate(call, self.facts, 'default', 'vm-01', '02:11:22:33:44:55')
        self.assertEqual(result, '192.0.2.9/25')
        self.assertEqual(call.call_args_list[0].args, ('POST', 'record:host', {
            'name': 'vm-01', 'network_view': 'default', 'configure_for_dns': False,
            'ipv4addrs': [{'ipv4addr': 'func:nextavailableip:192.0.2.0/25,default',
                           'configure_for_dhcp': False}]}))
        self.assertEqual(call.call_args_list[1].args, ('GET', REF + '?_return_fields=' + ipam.READ_FIELDS))
        body = json.dumps(call.call_args_list[0].args[2])
        self.assertNotIn('mac', body)
        self.assertNotIn('example.test', body)
        self.assertIn('ref=' + REF, self.output.getvalue())

    def test_host_reference_validation_does_not_follow_untrusted_urls(self):
        for ref in (REF, 'record:host/test:vm-01/default', 'record:host/test/%2f'):
            self.assertTrue(ipam.valid_host_ref(ref))
        for ref in (None, {}, 'https://evil.test', 'record:host/a?bad=1', 'record:host/a#fragment',
                    'record:host/a/%', 'record:host/a/%ZZ', 'record:host/a/%0a',
                    'record:host/../x', 'record:host/%2e%2e/x', 'record:host/a\\b'):
            with self.subTest(ref=ref):
                self.assertFalse(ipam.valid_host_ref(ref))
                call = Mock(return_value=ref)
                with self.assertRaises(ipam.AllocationError):
                    ipam.infoblox_allocate(call, self.facts, 'default', 'vm-01', '02:11:22:33:44:55')
                call.assert_called_once()

    def test_readback_identity_and_dns_dhcp_false_are_required(self):
        bad = [None, [], {}, host(name='other'), host(network_view='other'), host(configure_for_dns=True),
               host(configure_for_dns='false'), host(configure_for_dns=0), host(ipv4addrs=[]),
               host(ipv4addrs=[{}, {}]), host(ipv4addrs=[{}]),
               host(ipv4addrs=[{'ipv4addr': '192.0.2.9', 'configure_for_dhcp': True}]),
               host(ipv4addrs=[{'ipv4addr': '192.0.2.9', 'configure_for_dhcp': 'false'}])]
        for record in bad:
            with self.subTest(record=record):
                call = Mock(side_effect=[REF, record])
                with self.assertRaises(ipam.AllocationError):
                    ipam.infoblox_allocate(call, self.facts, 'default', 'vm-01', '02:11:22:33:44:55')
                self.assertEqual(call.call_count, 2)

    def test_unusable_or_infrastructure_address_rejected(self):
        for ip in ('192.0.3.9', '192.0.2.0', '192.0.2.127', '192.0.2.1', '192.0.2.2',
                   '::1', '127.0.0.1', '224.0.0.1', '0.0.0.0', 1234):
            call = Mock(side_effect=[REF, host(ip)])
            with self.subTest(ip=ip), self.assertRaises(ipam.AllocationError):
                ipam.infoblox_allocate(call, self.facts, 'default', 'vm-01', '02:11:22:33:44:55')
            self.assertEqual(call.call_count, 2)

    def test_failed_or_uncertain_insert_never_retried(self):
        for exc in (ipam.AllocationError('HTTP 403'), TimeoutError('test timeout')):
            call = Mock(side_effect=exc)
            with self.assertRaises((ipam.AllocationError, TimeoutError)):
                ipam.infoblox_allocate(call, self.facts, 'default', 'vm-01', '02:11:22:33:44:55')
            call.assert_called_once()
        self.assertIn('ALLOCATING host=vm-01', self.output.getvalue())

    def test_http_body_is_not_logged(self):
        error = urllib.error.HTTPError('https://ipam.example.test', 400, 'Bad', {}, io.BytesIO(b'SECRET_BODY'))
        with patch.object(ipam.OPENER, 'open', side_effect=error), self.assertRaises(ipam.AllocationError) as raised:
            ipam.request('https://ipam.example.test', 'POST', 'record:host', {}, user='user', password='SECRET_BODY')
        self.assertNotIn('SECRET_BODY', str(raised.exception))
        self.assertIn('400', str(raised.exception))

    def test_network_error_is_credential_safe(self):
        with patch.object(ipam.OPENER, 'open', side_effect=urllib.error.URLError('TEST_ONLY')), self.assertRaises(ipam.AllocationError) as raised:
            ipam.request('https://ipam.example.test', 'GET', 'record:host', user='user', password='TEST_ONLY')
        self.assertNotIn('TEST_ONLY', str(raised.exception))

    def test_request_keeps_percent_encoding_and_basic_auth(self):
        response = Mock()
        response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(ipam.OPENER, 'open', return_value=response) as opened:
            ipam.request('https://ipam.example.test/wapi/v2.13.6', 'GET', REF + '?_return_fields=ipv4addrs', user='test', password='pass')
        req = opened.call_args.args[0]
        self.assertIn('/%20?', req.full_url)
        self.assertNotIn('%2520', req.full_url)
        self.assertEqual(req.get_header('Authorization'), 'Basic dGVzdDpwYXNz')

    def test_redirects_not_followed(self):
        self.assertIsNone(ipam._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example.test'))

    def test_bad_names_do_not_reach_ipam(self):
        for data in ('vm-01\nvm-01\n', 'BAD_NAME\n', '', '\n'.join(f'vm-0{i}' for i in range(6))):
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp); (p / 'names').write_text(data)
                with patch.dict(os.environ, {'VM_NAMES_FILE': str(p / 'names'), 'ALLOCATIONS_FILE': str(p / 'receipt')}), patch.object(ipam, 'infoblox_preflight') as pre:
                    with self.assertRaises(ipam.AllocationError): ipam.main()
                    pre.assert_not_called()

    def test_partial_batch_retains_receipt_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p / 'names').write_text('vm-01\nvm-02\n')
            with patch.dict(os.environ, {'VM_NAMES_FILE': str(p / 'names'), 'ALLOCATIONS_FILE': str(p / 'receipt')}), patch.object(ipam, 'infoblox_preflight', return_value=(Mock(), self.facts, 'default')), patch.object(ipam, 'infoblox_allocate', side_effect=['192.0.2.9/25', ipam.AllocationError('failed')]):
                with self.assertRaises(ipam.AllocationError): ipam.main()
            fields = (p / 'receipt').read_text().strip().split('|')
            self.assertEqual(fields[:2], ['vm-01', '192.0.2.9/25'])
            self.assertRegex(fields[2], r'^02(:[0-9a-f]{2}){5}$')
            self.assertEqual(fields[3:], ['192.0.2.1', '192.0.2.2,192.0.2.3', 'example.test'])

    def test_duplicate_address_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p / 'names').write_text('vm-01\nvm-02\n')
            with patch.dict(os.environ, {'VM_NAMES_FILE': str(p / 'names'), 'ALLOCATIONS_FILE': str(p / 'receipt')}), patch.object(ipam, 'infoblox_preflight', return_value=(Mock(), self.facts, 'default')), patch.object(ipam, 'infoblox_allocate', return_value='192.0.2.9/25'):
                with self.assertRaises(ipam.AllocationError): ipam.main()
            self.assertEqual(len((p / 'receipt').read_text().splitlines()), 1)

    def test_existing_receipt_prevents_any_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p / 'names').write_text('vm-01\n'); (p / 'receipt').write_text('keep')
            with patch.dict(os.environ, {'VM_NAMES_FILE': str(p / 'names'), 'ALLOCATIONS_FILE': str(p / 'receipt')}), patch.object(ipam, 'infoblox_preflight') as call:
                with self.assertRaises(FileExistsError): ipam.main()
                call.assert_not_called()
            self.assertEqual((p / 'receipt').read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
