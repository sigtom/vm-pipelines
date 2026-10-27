"""Destination RBAC, input wiring and safe upgrades of existing catalog entries."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
OC = shutil.which('oc')
spec = importlib.util.spec_from_file_location('upgrade_forms', ROOT / 'scripts/add-target-namespace.py')
upgrade_forms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade_forms)


class TargetNamespaceTests(unittest.TestCase):
    def legacy_form(self, name='request-one', network=None):
        form = yaml.safe_load((ROOT / 'tekton/request-template.yaml').read_text())
        form['metadata'].update(name=name, resourceVersion='123')
        form['metadata']['annotations']['openshift.io/display-name'] = 'Keep my custom title'
        form['parameters'] = [p for p in form['parameters'] if p['name'] != 'TARGET_NAMESPACE']
        run = form['objects'][0]
        run['spec']['params'] = [p for p in run['spec']['params'] if p['name'] != 'target-namespace']
        if network:
            run['spec']['workspaces'].append({'name': 'network', 'configMap': {'name': network}})
        return form

    def test_sixth_input_flows_without_exposing_execution_namespace(self):
        pipeline = yaml.safe_load((ROOT / 'tekton/pipeline.yaml').read_text())['spec']
        task = yaml.safe_load((ROOT / 'tekton/task.yaml').read_text())['spec']
        for obj in (pipeline, task):
            param = next(p for p in obj['params'] if p['name'] == 'target-namespace')
            self.assertEqual(param['default'], '')
        self.assertIn({'name': 'target-namespace', 'value': '$(params.target-namespace)'}, pipeline['tasks'][0]['params'])
        for name in ('preflight', 'create-vms'):
            env = next(s for s in task['steps'] if s['name'] == name)['env']
            execution = next(e for e in env if e['name'] == 'EXECUTION_NAMESPACE')
            self.assertEqual(execution['valueFrom'], {'fieldRef': {'fieldPath': 'metadata.namespace'}})
        pre = next(s for s in task['steps'] if s['name'] == 'preflight')
        self.assertIn({'name': 'REQ_TARGET_NAMESPACE', 'value': '$(params.target-namespace)'}, pre['env'])
        self.assertNotIn('$(params.target-namespace)', pre['script'])

    def test_upgrade_preserves_five_titles_network_bindings_and_parameters(self):
        forms = [self.legacy_form('request-one')]
        forms += [self.legacy_form(f'request-{i}', f'vm-network-{i}') for i in range(2, 6)]
        image = yaml.safe_load((ROOT / 'templates/rhel-vm.yaml').read_text())
        config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'do-not-touch'}, 'data': {'PROFILE_ENABLED': 'false'}}
        source = {'apiVersion': 'v1', 'kind': 'List', 'items': [image, config, *forms]}
        before = copy.deepcopy(source)
        result = upgrade_forms.upgrade(source)
        self.assertEqual(source, before)
        self.assertEqual(len(result['items']), 5)
        for original, updated in zip(forms, result['items']):
            self.assertEqual(updated['metadata'], original['metadata'])
            self.assertEqual(updated['parameters'][:-1], original['parameters'])
            self.assertEqual(updated['parameters'][-1]['name'], 'TARGET_NAMESPACE')
            self.assertEqual(updated['parameters'][-1]['value'], 'vm-provisioning')
            old, new = original['objects'][0]['spec'], updated['objects'][0]['spec']
            self.assertEqual(old['workspaces'], new['workspaces'])
            self.assertEqual(old['params'], new['params'][:-1])
            self.assertEqual(new['params'][-1], {'name': 'target-namespace', 'value': '${TARGET_NAMESPACE}'})
        self.assertEqual(upgrade_forms.upgrade(result), result)

    def test_upgrade_rejects_conflicting_binding_or_no_matching_form(self):
        form = self.legacy_form()
        form['objects'][0]['spec']['params'].append({'name': 'target-namespace', 'value': 'unexpected'})
        with self.assertRaises(ValueError):
            upgrade_forms.upgrade(form)
        with self.assertRaises(ValueError):
            upgrade_forms.upgrade({'kind': 'List', 'items': []})

    def test_upgrade_retains_resource_version_and_custom_fields(self):
        form = self.legacy_form()
        form['metadata'].update(uid='server-uid', managedFields=[{}])
        form['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration'] = 'old'
        form['parameters'].append({'name': 'CUSTOM', 'value': 'keep'})
        updated = upgrade_forms.upgrade(form)['items'][0]
        self.assertEqual(updated['metadata']['resourceVersion'], '123')
        self.assertNotIn('managedFields', updated['metadata'])
        self.assertNotIn('uid', updated['metadata'])
        self.assertEqual(updated['parameters'][-2], {'name': 'CUSTOM', 'value': 'keep'})

    def test_target_setup_has_only_scoped_roles_and_matching_instance_types(self):
        template = yaml.safe_load((ROOT / 'tekton/target-namespace.yaml').read_text())
        objects = template['objects']
        self.assertEqual([o['kind'] for o in objects], ['Role', 'RoleBinding'] + ['VirtualMachineInstancetype'] * 3)
        for obj in objects:
            self.assertEqual(obj['metadata']['namespace'], '${TARGET_NAMESPACE}')
        role, binding = objects[:2]
        self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': 'vm-provision', 'namespace': '${PROVISIONING_NAMESPACE}'}])
        for rule in role['rules']:
            self.assertFalse(set(rule['verbs']) - {'get', 'create'})
            self.assertFalse(set(rule['resources']) & {'secrets', 'pods', 'roles', 'rolebindings', 'namespaces', '*', 'templates'})
            if 'create' in rule['verbs']:
                self.assertEqual(rule['resources'], ['virtualmachines'])
        for size, obj in zip(('small', 'medium', 'large'), objects[2:]):
            existing = yaml.safe_load((ROOT / f'sizes/{size}.yaml').read_text())
            self.assertEqual(obj['spec'], existing['spec'])
            self.assertEqual(obj['metadata']['name'], existing['metadata']['name'])
        installation = yaml.safe_load((ROOT / 'tekton/kustomization.yaml').read_text())
        self.assertNotIn('target-namespace.yaml', installation['resources'])

    def test_native_target_setup_and_migrated_form_processing(self):
        if not OC:
            if os.environ.get('REQUIRE_OC') == '1':
                self.fail('oc required in CI')
            self.skipTest('oc not installed')
        cmd = [OC, 'process', '--local', '-f', str(ROOT / 'tekton/target-namespace.yaml'),
               '-p', 'TARGET_NAMESPACE=team-apps', '-p', 'PROVISIONING_NAMESPACE=vm-provisioning', '-o', 'json']
        result = json.loads(subprocess.check_output(cmd, text=True))
        self.assertEqual({o['metadata']['namespace'] for o in result['items']}, {'team-apps'})
        self.assertEqual(result['items'][1]['subjects'][0]['namespace'], 'vm-provisioning')
        form = upgrade_forms.upgrade(self.legacy_form())['items'][0]
        cmd = [OC, 'process', '--local', '-f', '-', '-o', 'json', '-p', 'VM_NAME=web-01',
               '-p', 'SSH_PUBLIC_KEY=public-test-input', '-p', 'TARGET_NAMESPACE=team-apps']
        processed = json.loads(subprocess.check_output(cmd, input=json.dumps(form), text=True))
        run = processed['items'][0]
        self.assertNotEqual(run['metadata'].get('namespace'), 'team-apps')
        self.assertEqual({p['name']: p['value'] for p in run['spec']['params']}['target-namespace'], 'team-apps')


if __name__ == '__main__':
    unittest.main()
