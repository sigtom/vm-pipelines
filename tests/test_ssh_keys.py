"""SSH key validation and native RSA batch rendering; no live API calls.

RSA fixtures use synthetic integers for encoding tests, never customer keys.
"""
import ast
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ipam

ED25519 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea'


def field(raw):
    return struct.pack('>I', len(raw)) + raw


def mpint(number):
    raw = number.to_bytes(max(1, (number.bit_length() + 7) // 8), 'big')
    return field((b'\0' if raw[0] & 0x80 else b'') + raw)


def rsa_blob(bits=4096, exponent=65537, modulus=None):
    # These are parser fixtures, not deployable keypairs.
    modulus = modulus if modulus is not None else (1 << (bits - 1)) | 0xA5
    return field(b'ssh-rsa') + mpint(exponent) + mpint(modulus)


def key_line(blob, kind='ssh-rsa', comment=''):
    return kind + ' ' + base64.b64encode(blob).decode() + (' ' + comment if comment else '')


class SshKeyTests(unittest.TestCase):
    def test_ed25519_preserved(self):
        for comment in ('', 'operator@example.test', 'say "hello": # $HOME'):
            key = ED25519 + (' ' + comment if comment else '')
            self.assertEqual(ipam.validate_ssh_key(key), key)

    def test_rsa_sizes_and_comments_preserved(self):
        for bits in (2048, 3072, 4096, 8192):
            for comment in ('', 'operator@example.test', "quoted 'comment': # test"):
                key = key_line(rsa_blob(bits), comment=comment)
                with self.subTest(bits=bits, comment=comment):
                    self.assertEqual(ipam.validate_ssh_key(key), key)

    def test_weak_rsa_rejected(self):
        for bits in (512, 1024, 2047):
            with self.subTest(bits=bits), self.assertRaisesRegex(ipam.AllocationError, '2048'):
                ipam.validate_ssh_key(key_line(rsa_blob(bits)))

    def test_bad_rsa_exponents_and_even_modulus_rejected(self):
        modulus = (1 << 2047) | 0xA5
        for exponent in (1, 2, 4, 65536, modulus, modulus + 2):
            with self.subTest(exponent=exponent), self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key_line(rsa_blob(modulus=modulus, exponent=exponent)))
        with self.assertRaises(ipam.AllocationError):
            ipam.validate_ssh_key(key_line(rsa_blob(modulus=modulus + 1)))

    def test_negative_zero_and_noncanonical_mpints_rejected(self):
        for bad in (b'', b'\0', b'\x80', b'\xff', b'\0\0\x80', b'\0\x01\0\x01'):
            for blob in (field(b'ssh-rsa') + field(bad) + mpint((1 << 2047) | 1),
                         field(b'ssh-rsa') + mpint(65537) + field(bad)):
                with self.subTest(raw=bad), self.assertRaises(ipam.AllocationError):
                    ipam.validate_ssh_key(key_line(blob))

    def test_all_rsa_truncations_rejected(self):
        blob = rsa_blob()
        for end in range(len(blob)):
            with self.subTest(end=end), self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key_line(blob[:end]))

    def test_rsa_trailing_data_rejected(self):
        for tail in (b'\0', b'extra', mpint(65537)):
            with self.assertRaisesRegex(ipam.AllocationError, 'trailing'):
                ipam.validate_ssh_key(key_line(rsa_blob() + tail))

    def test_declared_lengths_cannot_run_past_buffer(self):
        for blob in (b'\xff' * 4, field(b'ssh-rsa') + b'\xff' * 4):
            with self.assertRaisesRegex(ipam.AllocationError, 'Truncated'):
                ipam.validate_ssh_key(key_line(blob))

    def test_text_type_and_wire_type_must_match(self):
        for key in (key_line(rsa_blob(), 'ssh-ed25519'), ED25519.replace('ssh-ed25519 ', 'ssh-rsa ', 1)):
            with self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key)

    def test_base64_is_strict(self):
        valid = key_line(rsa_blob())
        for key in ('ssh-rsa !!!!', valid + '=', 'ssh-rsa AAAA', 'ssh-rsa '):
            with self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(key)
        # Nonzero base64 pad bits decode to the same bytes but are not canonical.
        with self.assertRaisesRegex(ipam.AllocationError, 'canonical'):
            ipam.validate_ssh_key('ssh-rsa AB==')

    def test_private_options_certificates_and_other_types_rejected(self):
        for value in ('-----BEGIN OPENSSH PRIVATE KEY-----',
                      'from="192.0.2.1" ' + ED25519,
                      key_line(rsa_blob(), 'ssh-dss'),
                      key_line(rsa_blob(), 'ecdsa-sha2-nistp256'),
                      key_line(rsa_blob(), 'ssh-rsa-cert-v01@openssh.com')):
            with self.subTest(value=value[:40]), self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(value)

    def test_line_limits_and_control_characters(self):
        for value in (None, 1, '', ' ' + ED25519, ED25519 + ' ', ED25519 + '\n',
                      ED25519 + '\r', ED25519 + '\n' + ED25519, ED25519 + '\tcomment',
                      ED25519 + '\0', ED25519 + ' ' + 'x' * 8192):
            with self.assertRaises(ipam.AllocationError):
                ipam.validate_ssh_key(value)

    def test_check_command_needs_no_credentials_and_makes_no_api_call(self):
        for count in ('1', '2', '5'):
            for key in (ED25519, key_line(rsa_blob())):
                env = {'REQ_SSH_KEY': key, 'REQ_COUNT': count}
                with patch.dict(os.environ, env, clear=True), patch.object(ipam, 'request') as request:
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        ipam.main(['check-ssh-key'])
                    self.assertEqual(output.getvalue(), 'SSH public key format OK\n')
                    request.assert_not_called()

    def test_invalid_key_fails_without_calling_ipam_or_printing_key(self):
        key = key_line(rsa_blob(1024), comment='DO_NOT_ECHO_COMMENT')
        env = dict(os.environ, REQ_SSH_KEY=key)
        run = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/ipam.py'), 'check-ssh-key'],
                             env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('2048', run.stderr)
        self.assertNotIn('DO_NOT_ECHO_COMMENT', run.stderr + run.stdout)
        self.assertNotIn(key.split()[1], run.stderr + run.stdout)

    def test_python39_syntax_and_stdlib_only_validation(self):
        ast.parse((ROOT / 'scripts/ipam.py').read_text(), feature_version=(3, 9))
        run = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/ipam.py'), 'check-ssh-key'],
                             env=dict(os.environ, REQ_SSH_KEY=key_line(rsa_blob())),
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_native_two_vm_batch_preserves_rsa_key(self):
        oc = shutil.which('oc')
        if not oc:
            if os.environ.get('REQUIRE_OC') == '1':
                self.fail('oc required in CI')
            self.skipTest('oc not installed')
        key = key_line(rsa_blob(), comment='say "hello": # test')
        forms = [json.loads(subprocess.check_output(
            [oc, 'process', '--local', '-f', str(ROOT / 'tekton/network-catalog.yaml'),
             '-p', 'NETWORK_ATTACHMENT=vlan-101', '-p', 'REQUEST_TEMPLATE_NAME=request-test',
             '-p', 'DISPLAY_NAME=Provision VM test', '-o', 'json'], text=True))['items'][1]]
        # Native request forms must retain the one key for count=2.
        with tempfile.TemporaryDirectory() as tmp:
            form = Path(tmp) / 'form.json'
            form.write_text(json.dumps(forms[0]))
            for path in (ROOT / 'tekton/request-template.yaml', form):
                result = json.loads(subprocess.check_output(
                    [oc, 'process', '--local', '-f', str(path), '-p', 'VM_NAME=batch-test',
                     '-p', 'COUNT=2', '-p', 'SSH_PUBLIC_KEY=' + key,
                     '-p', 'TARGET_NAMESPACE=team-apps', '-o', 'json'], text=True))
                values = {p['name']: p['value'] for p in result['items'][0]['spec']['params']}
                self.assertEqual(values['count'], '2')
                self.assertEqual(values['ssh-public-key'], key)
        params = dict(VM_NAMESPACE='team-apps', VM_SIZE='small', INSTANCE_TYPE='poc-small',
                      DISK_SIZE='110Gi', STORAGE_CLASS='approved-storage', ACCESS_MODE='ReadWriteMany',
                      VOLUME_MODE='Block', DATASOURCE_NAME='rhel-golden', DATASOURCE_NAMESPACE='image-sources',
                      MACHINE_TYPE='q35', NETWORK_ATTACHMENT='vlan-101', MAC_ADDRESS='02:11:22:33:44:55',
                      IP_CIDR='192.0.2.9/25', GATEWAY='192.0.2.1', DNS_SERVERS='192.0.2.2',
                      DOMAIN='example.test', GUEST_USER='guestops', SSH_PUBLIC_KEY=key)
        for name in ('batch-test-001', 'batch-test-002'):
            cmd = [oc, 'process', '--local', '-f', str(ROOT / 'templates/rhel-vm.yaml'), '-o', 'json']
            for param, value in dict(params, VM_NAME=name).items():
                cmd += ['-p', f'{param}={value}']
            vm = json.loads(subprocess.check_output(cmd, text=True))['items'][0]
            self.assertEqual(vm['metadata']['name'], name)
            cloud = vm['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']['userData']
            self.assertIn('\n      ' + key + '\n', cloud)
            self.assertNotIn('PubkeyAcceptedAlgorithms', cloud)
            self.assertNotIn('update-crypto-policies', cloud)


if __name__ == '__main__':
    unittest.main()
