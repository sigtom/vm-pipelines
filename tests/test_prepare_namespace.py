"""Namespace preparation safety and idempotence; no live cluster or IPAM writes."""
import ast
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml  # Tests only; the admin script uses oc process, not PyYAML.

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/prepare-namespace.py'
spec = importlib.util.spec_from_file_location('prepare_namespace', SCRIPT)
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)
OC = shutil.which('oc')


def source_nad(name='vlan-101'):
    return {
        'apiVersion': 'k8s.cni.cncf.io/v1', 'kind': 'NetworkAttachmentDefinition',
        'metadata': {'name': name, 'namespace': 'vm-provisioning', 'uid': 'source-uid',
                     'resourceVersion': '123', 'creationTimestamp': 'old', 'managedFields': [{}],
                     'ownerReferences': [{'uid': 'source-owner'}], 'finalizers': ['keep-source'],
                     'labels': {'argocd.argoproj.io/instance': 'source-app', 'team': 'source'},
                     'annotations': {prep.RESOURCE_ANNOTATION: 'bridge.network.kubevirt.io/br-test',
                         'argocd.argoproj.io/tracking-id': 'source-app:some-id',
                         'kubectl.kubernetes.io/last-applied-configuration': 'stale',
                         'meta.helm.sh/release-name': 'source-release'}},
        'spec': {'config': json.dumps({'cniVersion': '0.3.1', 'name': name, 'type': 'bridge',
                                     'bridge': 'br-test', 'disableContainerInterface': True,
                                     'macspoofchk': False})},
        'status': {'do-not-copy': True},
    }


class FakeOc:
    """In-memory cluster; all API writes are recorded and create-only."""
    def __init__(self, target='team-apps', source='vm-provisioning'):
        self.context = 'test-context'
        self.target, self.source = target, source
        self.objects, self.calls, self.writes = {}, [], []
        self.dry_error = self.deny_access = self.fail_create_at = None
        self.missing_after_create = False
        for ns in (source, target):
            self.objects[('', 'namespaces', ns)] = {
                'kind': 'Namespace', 'metadata': {'name': ns}, 'status': {'phase': 'Active'}}
        self.objects[(source, 'serviceaccounts', 'vm-provision')] = {
            'kind': 'ServiceAccount', 'metadata': {'name': 'vm-provision', 'namespace': source}}
        for name in ('vlan-101', 'vlan-102', 'vlan-103'):
            nad = source_nad(name)
            nad['metadata']['namespace'] = source
            self.objects[(source, prep.NAD, name)] = nad

    def get(self, resource, name, namespace=''):
        self.calls.append(('get', namespace, resource, name))
        if self.missing_after_create and self.writes and namespace == self.target:
            return None
        return copy.deepcopy(self.objects.get((namespace, resource, name)))

    def run(self, *args, document=None):
        self.calls.append(('run', args, copy.deepcopy(document)))
        if args[0] == 'process':
            assert '--local' in args
            text = Path(args[args.index('-f') + 1]).read_text()
            for i, arg in enumerate(args):
                if arg == '-p':
                    key, value = args[i + 1].split('=', 1)
                    text = text.replace('${' + key + '}', value)
            self.rendered = yaml.safe_load(text)['objects']
            return json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': self.rendered})
        if args[0] == 'create':
            if '--dry-run=server' in args:
                if self.dry_error:
                    raise prep.PrepError(self.dry_error)
                return json.dumps(document)
            if self.fail_create_at == len(self.writes):
                raise prep.PrepError('simulated concurrent create / timeout')
            assert document['kind'] in prep.RESOURCES
            assert document['metadata']['namespace'] == self.target
            key = (self.target, prep.RESOURCES[document['kind']], document['metadata']['name'])
            if key in self.objects:
                raise prep.PrepError('AlreadyExists')
            self.objects[key] = copy.deepcopy(document)
            self.writes.append(copy.deepcopy(document))
            return prep.identity(document)
        if args[:2] == ('auth', 'can-i'):
            assert args[args.index('--as') + 1] == f'system:serviceaccount:{self.source}:vm-provision'
            assert args[args.index('-n') + 1] == self.target
            if self.deny_access:
                raise prep.PrepError('Forbidden: impersonation or runtime permission denied')
            return 'yes\n'
        raise AssertionError(f'Unexpected command: {args}')

    def seed_target(self, networks=('vlan-101',)):
        for obj in prep.desired_objects(self, self.target, self.source, list(networks)):
            self.objects[(self.target, prep.RESOURCES[obj['kind']], obj['metadata']['name'])] = copy.deepcopy(obj)
        self.calls.clear()


class PrepareNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.oc = FakeOc()
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def prepare(self, apply=False, networks=('vlan-101',)):
        prep.prepare(self.oc, self.oc.target, self.oc.source, list(networks), apply)

    def test_dry_run_checks_server_without_writes(self):
        before = copy.deepcopy(self.oc.objects)
        self.prepare(networks=('vlan-101', 'vlan-102', 'vlan-103'))
        self.assertEqual(before, self.oc.objects)
        self.assertEqual(self.oc.writes, [])
        creates = [c for c in self.oc.calls if c[0] == 'run' and c[1][0] == 'create']
        self.assertEqual(len(creates), 1)
        self.assertIn('--dry-run=server', creates[0][1])
        self.assertEqual(len(creates[0][2]['items']), 8)
        self.assertIn('DRY RUN passed', self.output.getvalue())
        self.assertNotIn('PREPARED:', self.output.getvalue())

    def test_apply_copies_only_selected_nads_and_grants_access_last(self):
        before = copy.deepcopy({k: v for k, v in self.oc.objects.items() if k[0] != self.oc.target})
        self.prepare(True, ('vlan-101', 'vlan-103'))
        self.assertEqual(len(self.oc.writes), 7)
        self.assertEqual(self.oc.writes[-1]['kind'], 'RoleBinding')
        self.assertIsNone(self.oc.get(prep.NAD, 'vlan-102', self.oc.target))
        self.assertEqual(before, {k: v for k, v in self.oc.objects.items() if k[0] != self.oc.target})
        self.assertIn('PREPARED: team-apps', self.output.getvalue())
        for obj in self.oc.writes:
            self.assertIn(obj['kind'], prep.RESOURCES)
            self.assertEqual(obj['metadata']['namespace'], self.oc.target)

    def test_rerun_leaves_matching_objects_untouched(self):
        self.prepare(True)
        before = copy.deepcopy(self.oc.objects)
        writes = len(self.oc.writes)
        self.prepare(True)
        self.assertEqual(before, self.oc.objects)
        self.assertEqual(len(self.oc.writes), writes)
        self.assertIn('MATCH', self.output.getvalue())

    def test_existing_managed_matching_nad_is_not_adopted(self):
        nad = source_nad()
        nad['metadata']['namespace'] = self.oc.target
        self.oc.objects[(self.oc.target, prep.NAD, 'vlan-101')] = copy.deepcopy(nad)
        self.prepare(True)
        self.assertEqual(nad, self.oc.get(prep.NAD, 'vlan-101', self.oc.target))
        self.assertFalse(any(o['kind'] == 'NetworkAttachmentDefinition' for o in self.oc.writes))

    def test_source_metadata_is_stripped_and_resource_annotation_preserved(self):
        source = source_nad()
        before = copy.deepcopy(source)
        obj = prep.copy_nad(source, 'team-apps')
        self.assertEqual(source, before)
        self.assertEqual(obj['metadata'], {'name': 'vlan-101', 'namespace': 'team-apps',
            'annotations': {prep.RESOURCE_ANNOTATION: 'bridge.network.kubevirt.io/br-test'}})
        self.assertEqual(obj['spec'], source['spec'])
        self.assertNotIn('status', obj)
        obj['spec']['config'] = '{}'
        self.assertEqual(source, before)

    def test_no_resource_annotation_does_not_invent_one(self):
        nad = source_nad()
        del nad['metadata']['annotations'][prep.RESOURCE_ANNOTATION]
        self.assertNotIn('annotations', prep.copy_nad(nad, 'team-apps')['metadata'])

    def test_json_spacing_and_key_order_do_not_cause_a_conflict(self):
        self.oc.seed_target()
        nad = self.oc.objects[(self.oc.target, prep.NAD, 'vlan-101')]
        nad['spec']['config'] = json.dumps(json.loads(nad['spec']['config']), sort_keys=True, indent=4)
        self.prepare(True)
        self.assertEqual(self.oc.writes, [])

    def test_rbac_order_and_serviceaccount_api_group_defaults_match(self):
        self.oc.seed_target()
        role = self.oc.objects[(self.oc.target, prep.RESOURCES['Role'], 'vm-provision-target')]
        role['rules'].reverse()
        for rule in role['rules']:
            rule['verbs'].reverse()
            rule['nonResourceURLs'] = []
        binding = self.oc.objects[(self.oc.target, prep.RESOURCES['RoleBinding'], 'vm-provision-target')]
        binding['subjects'][0]['apiGroup'] = ''
        self.prepare(True)
        self.assertEqual(self.oc.writes, [])

    def test_each_functional_conflict_stops_before_all_creates(self):
        for kind in prep.RESOURCES:
            with self.subTest(kind=kind):
                self.oc = FakeOc()
                self.oc.seed_target()
                key = next(k for k, v in self.oc.objects.items()
                           if k[0] == self.oc.target and v['kind'] == kind)
                obj = self.oc.objects[key]
                if kind == 'NetworkAttachmentDefinition':
                    value = json.loads(obj['spec']['config']); value['bridge'] = 'different'
                    obj['spec']['config'] = json.dumps(value)
                elif kind == 'Role':
                    obj['rules'][0]['verbs'].append('delete')
                elif kind == 'RoleBinding':
                    obj['subjects'][0]['namespace'] = 'another-central-namespace'
                else:
                    obj['spec']['cpu']['guest'] = 64
                with self.assertRaisesRegex(prep.PrepError, 'Conflict:'):
                    self.prepare(True, ('vlan-101', 'vlan-102'))
                self.assertEqual(self.oc.writes, [])
                self.assertFalse(any(c[0] == 'run' and c[1][0] == 'create' for c in self.oc.calls))

    def test_added_or_removed_scheduling_annotation_is_a_conflict(self):
        for annotation in (None, 'different-resource'):
            with self.subTest(annotation=annotation):
                self.oc = FakeOc(); self.oc.seed_target()
                nad = self.oc.objects[(self.oc.target, prep.NAD, 'vlan-101')]
                nad['metadata']['annotations'] = {} if annotation is None else {prep.RESOURCE_ANNOTATION: annotation}
                with self.assertRaisesRegex(prep.PrepError, 'Conflict:'):
                    self.prepare(True)
                self.assertEqual(self.oc.writes, [])

    def test_missing_source_nad_stops_before_any_create(self):
        with self.assertRaisesRegex(prep.PrepError, 'Missing active source NAD'):
            self.prepare(True, ('vlan-101', 'vlan-404'))
        self.assertEqual(self.oc.writes, [])

    def test_invalid_json_or_namespace_bound_nad_stops(self):
        for value in ('invalid', '[]', '{}', json.dumps({'type': 'ovn-k8s-cni-overlay', 'netAttachDefName': 'source/network'})):
            with self.subTest(value=value):
                self.oc.objects[(self.oc.source, prep.NAD, 'vlan-101')]['spec']['config'] = value
                with self.assertRaises(prep.PrepError):
                    self.prepare(True)
                self.assertEqual(self.oc.writes, [])

    def test_missing_or_inactive_namespace_stops(self):
        for which in ('source', 'target'):
            for state in ('missing', 'terminating', 'deleting'):
                with self.subTest(which=which, state=state):
                    self.oc = FakeOc()
                    key = ('', 'namespaces', getattr(self.oc, which))
                    if state == 'missing':
                        del self.oc.objects[key]
                    elif state == 'terminating':
                        self.oc.objects[key]['status']['phase'] = 'Terminating'
                    else:
                        self.oc.objects[key]['metadata']['deletionTimestamp'] = 'now'
                    with self.assertRaisesRegex(prep.PrepError, 'must already exist and be Active'):
                        self.prepare(True)
                    self.assertEqual(self.oc.writes, [])

    def test_missing_central_serviceaccount_stops(self):
        del self.oc.objects[(self.oc.source, 'serviceaccounts', 'vm-provision')]
        with self.assertRaisesRegex(prep.PrepError, 'central ServiceAccount'):
            self.prepare(True)
        self.assertEqual(self.oc.writes, [])

    def test_source_and_destination_deletion_are_rejected(self):
        self.oc.objects[(self.oc.source, prep.NAD, 'vlan-101')]['metadata']['deletionTimestamp'] = 'now'
        with self.assertRaises(prep.PrepError): self.prepare(True)
        self.oc = FakeOc(); self.oc.seed_target()
        self.oc.objects[(self.oc.target, prep.NAD, 'vlan-101')]['metadata']['deletionTimestamp'] = 'now'
        with self.assertRaisesRegex(prep.PrepError, 'being deleted'): self.prepare(True)
        self.assertEqual(self.oc.writes, [])

    def test_bad_names_system_targets_and_duplicates_fail_before_cli(self):
        for target in ('default', 'openshift', 'openshift-monitoring', 'kube-system',
                       'vm-provisioning', 'UPPER', '-bad', 'x' * 64, 'a/b', 'one two', '$(cmd)'):
            with self.subTest(target=target), self.assertRaises(prep.PrepError):
                prep.prepare(self.oc, target, self.oc.source, ['vlan-101'], True)
        for networks in ([], ['vlan-101', 'vlan-101'], ['bad/name']):
            with self.assertRaises(prep.PrepError):
                prep.prepare(self.oc, self.oc.target, self.oc.source, networks, True)
        self.assertEqual(self.oc.calls, [])

    def test_source_namespace_override_changes_rbac_subject_and_reads(self):
        self.oc = FakeOc(source='central-vms')
        self.prepare(True)
        binding = self.oc.writes[-1]
        self.assertEqual(binding['subjects'][0]['namespace'], 'central-vms')
        self.assertEqual(binding['subjects'][0]['name'], 'vm-provision')

    def test_server_dry_run_failure_prevents_all_writes(self):
        self.oc.dry_error = 'admission denied'
        with self.assertRaisesRegex(prep.PrepError, 'admission denied'):
            self.prepare(True)
        self.assertEqual(self.oc.writes, [])

    def test_partial_failure_stops_without_rollback_retry_or_rolebinding(self):
        self.oc.fail_create_at = 2
        with self.assertRaisesRegex(prep.PrepError, 'simulated concurrent create'):
            self.prepare(True, ('vlan-101', 'vlan-102'))
        self.assertEqual(len(self.oc.writes), 2)
        self.assertNotIn('RoleBinding', [obj['kind'] for obj in self.oc.writes])
        self.oc.fail_create_at = None
        self.prepare(True, ('vlan-101', 'vlan-102'))
        self.assertEqual(len(self.oc.writes), 7)

    def test_readback_and_effective_access_are_required_for_success(self):
        for failure in ('missing_after_create', 'deny_access'):
            with self.subTest(failure=failure):
                self.oc = FakeOc(); setattr(self.oc, failure, True)
                with self.assertRaises(prep.PrepError): self.prepare(True)
                self.assertNotIn('PREPARED:', self.output.getvalue())

    def test_effective_checks_include_named_instance_types(self):
        self.prepare(True)
        checks = [c[1] for c in self.oc.calls if c[0] == 'run' and c[1][:2] == ('auth', 'can-i')]
        self.assertEqual(len(checks), 8)
        for size in ('small', 'medium', 'large'):
            self.assertTrue(any(f'virtualmachineinstancetypes.instancetype.kubevirt.io/poc-{size}' in c for c in checks))
        self.assertTrue(any(c[2:4] == ('create', 'virtualmachines.kubevirt.io') for c in checks))

    def test_oc_errors_are_never_treated_as_missing(self):
        client = prep.Oc.__new__(prep.Oc); client.context = 'pinned-context'
        with patch.object(prep.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Forbidden')) as run:
            with self.assertRaisesRegex(prep.PrepError, 'Forbidden'):
                client.get(prep.NAD, 'vlan-101', 'team-apps')
            self.assertIn('pinned-context', run.call_args.args[0])
            self.assertFalse(run.call_args.kwargs.get('shell', False))
        with patch.object(prep.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertIsNone(client.get(prep.NAD, 'vlan-101', 'team-apps'))

    def test_oc_missing_binary_and_timeout_are_clear_errors(self):
        client = prep.Oc.__new__(prep.Oc); client.context = 'test'
        for error in (FileNotFoundError('oc missing'), subprocess.TimeoutExpired('oc', 60)):
            with patch.object(prep.subprocess, 'run', side_effect=error), self.assertRaises(prep.PrepError):
                client.get('namespaces', 'team-apps')

    def test_cli_defaults_to_preview_and_uses_explicit_apply(self):
        for flags, expected in (([], False), (['--dry-run'], False), (['--apply'], True)):
            with patch.object(prep, 'Oc', return_value=self.oc), patch.object(prep, 'prepare') as call:
                self.assertEqual(prep.main(['team-apps', '--networks', 'vlan-101', *flags]), 0)
                self.assertEqual(call.call_args.args[-1], expected)
        with patch.object(prep, 'Oc') as client, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(prep.main(['default', '--networks', 'vlan-101', '--apply']), 1)
            client.assert_not_called()

    def test_python39_syntax_and_no_site_packages_for_help(self):
        ast.parse(SCRIPT.read_text(), feature_version=(3, 9))
        result = subprocess.run([sys.executable, '-S', str(SCRIPT), '--help'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--source-namespace', result.stdout)

    def test_actual_oc_local_target_template_rendering(self):
        if not OC:
            if os.environ.get('REQUIRE_OC') == '1':
                self.fail('oc required in CI')
            self.skipTest('oc not installed')
        result = subprocess.check_output([
            OC, 'process', '--local', '-f', str(ROOT / 'tekton/target-namespace.yaml'),
            '-p', 'TARGET_NAMESPACE=team-apps', '-p', 'PROVISIONING_NAMESPACE=vm-provisioning', '-o', 'json'
        ], text=True)
        actual = json.loads(result)['items']
        desired = prep.desired_objects(self.oc, 'team-apps', 'vm-provisioning', ['vlan-101'])
        desired = [obj for obj in desired if obj['kind'] != 'NetworkAttachmentDefinition']
        self.assertEqual({prep.identity(obj): prep.payload(obj) for obj in actual},
                         {prep.identity(obj): prep.payload(obj) for obj in desired})


if __name__ == '__main__':
    unittest.main()
