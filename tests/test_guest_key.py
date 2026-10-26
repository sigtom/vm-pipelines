"""Run the key-only guest script in a temporary home; OS identity calls are mocked."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea'


class GuestKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        (self.home / '.ssh').mkdir()
        self.keys = self.home / '.ssh/authorized_keys'
        self.keys.write_text('existing-key-without-newline')
        self.keyfile = self.root / 'new-key'
        self.public_key = KEY + ' quote\' " $() # comment'
        self.keyfile.write_text(self.public_key)
        self.log = self.root / 'calls'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        TEST_HOME=str(self.home), TEST_CALLS=str(self.log), TEST_MODE='existing',
                        REAL_INSTALL=shutil.which('install'))
        fake = '''import os, sys
from pathlib import Path
cmd=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['TEST_CALLS'],'a') as f: f.write(cmd+' '+repr(args)+'\\n')
if cmd=='id':
    if os.environ['TEST_MODE']=='missing': sys.exit(1)
    print('0' if os.environ['TEST_MODE']=='root' else '1001' if args[0]=='-u' else 'guestops')
elif cmd=='getent':
    print('guestops:x:1001:1001:Guest:'+os.environ['TEST_HOME']+':/bin/bash')
elif cmd=='install':
    # Preserve real mode/directory creation; do not change runner ownership.
    cleaned=[]
    while args:
        arg=args.pop(0)
        if arg in ('-o','-g'): args.pop(0)
        else: cleaned.append(arg)
    os.execv(os.environ['REAL_INSTALL'],[os.environ['REAL_INSTALL'],*cleaned])
elif cmd in ('chown','restorecon'): pass
else: sys.exit(99)
'''
        for command in ('id', 'getent', 'install', 'chown', 'restorecon', 'passwd', 'usermod', 'useradd', 'groupadd'):
            path = self.bin / command
            path.write_text('#!' + sys.executable + ' -S\n' + fake)
            path.chmod(0o755)
        template = yaml.safe_load((ROOT / 'templates/rhel-vm.yaml').read_text())
        cloud = yaml.safe_load(template['objects'][0]['spec']['template']['spec']['volumes'][1]['cloudInitNoCloud']['userData'])
        self.cloud = cloud
        self.script = cloud['runcmd'][0].replace('${GUEST_USER}', 'guestops').replace('/run/vm-provision-public-key', str(self.keyfile))

    def run_script(self):
        return subprocess.run(['sh', '-c', self.script], env=self.env, cwd=self.root, text=True, capture_output=True)

    def test_add_key_preserves_existing_contents_and_has_no_account_commands(self):
        run = self.run_script()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.keys.read_text().splitlines(), ['existing-key-without-newline', self.public_key])
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.keyfile.exists())
        log = self.log.read_text()
        self.assertIn('restorecon ', log)
        for command in ('passwd ', 'usermod ', 'useradd ', 'groupadd '):
            self.assertNotIn(command, log)
        self.assertEqual(self.cloud['users'], [])
        self.assertNotIn('ssh_pwauth', self.cloud)
        self.assertNotIn('chpasswd', self.cloud)

    def test_repeated_key_is_not_duplicated(self):
        self.assertEqual(self.run_script().returncode, 0)
        before = self.keys.read_bytes()
        self.keyfile.write_text(self.public_key)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.keys.read_bytes(), before)

    def test_missing_or_root_account_stops_before_key_changes(self):
        for mode in ('missing', 'root'):
            with self.subTest(mode=mode):
                self.env['TEST_MODE'] = mode
                run = self.run_script()
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(self.keys.read_text(), 'existing-key-without-newline')

    def test_symlink_key_target_is_rejected(self):
        target = self.root / 'protected'
        target.write_text('unchanged')
        self.keys.unlink()
        self.keys.symlink_to(target)
        run = self.run_script()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(target.read_text(), 'unchanged')


if __name__ == '__main__':
    unittest.main()
