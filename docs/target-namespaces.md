# Central provisioning into approved VM namespaces

The PipelineRun, Task, ServiceAccount, helper image, configuration, network
profiles, credentials and rhel-standard Template stay in **vm-provisioning**.
Only VMs and their generated DataVolumes/PVCs go to the chosen destination.
No second pipeline, controller or Infoblox credential copy is needed.

**Current helper: 0.4.** Complete [the IPAM-only upgrade](ipam-only-upgrade.md)
when moving from 0.3: that helper-code change requires one rebuild. Adding a
target namespace after the 0.4 upgrade does not require another image build.

## Operator form

Keep the native form's top **Namespace** selector on **vm-provisioning**.
Use **Target VM namespace** for the VM destination. All network catalog entries
retain their name, size, count, SSH key, labels and fixed NAD/profile binding.
There are six text inputs. CLI requests omitting target-namespace preserve the
site VM_NAMESPACE default. Do not edit that shared default for each request.

The selected NAD must exist in the target and describe the same VLAN as the
central profile. A matching name alone does not prove network equivalence.
No namespace prefix is added to the IPAM host's short name: names must remain
unique within the selected Infoblox network view, even across namespaces.
This workflow creates no DNS records.

## Upgrade an older five-field installation

Pause submissions and investigate active requests before changing definitions.
This upgrade leaves source disks, VMs, network ConfigMaps and credentials alone.
From the existing main checkout, preserve local image/display-name changes:

```sh
cd ~/vm-pipelines-rhel9
git pull --ff-only --autostash origin main
git status --short
git diff --name-only --diff-filter=U
grep -A2 -E 'name: (cli-image|ipam-image)$' tekton/task.yaml
```

Stop for conflicts/placeholders. The Task must reference the built 0.4 digest,
not the old DNS-enabled 0.3 image. Complete its build/config upgrade first.

```sh
oc apply --dry-run=server -f tekton/task.yaml -f tekton/pipeline.yaml &&
oc apply -f tekton/task.yaml -f tekton/pipeline.yaml
```

The authoring Template does not update already-generated forms. This offline
script adds only the missing target field/binding while preserving names,
display names, existing inputs, ServiceAccount and network workspaces. It skips
underlying VM/golden-image Templates and emits no ConfigMaps.

```sh
umask 077
backup=$(mktemp -d "$HOME/vm-request-upgrade.XXXXXX")
oc -n vm-provisioning get templates -o json > "$backup/templates.before.json" &&
python3 scripts/add-target-namespace.py < "$backup/templates.before.json" \
  > "$backup/templates.updated.json" &&
oc apply --dry-run=server -f "$backup/templates.updated.json" &&
oc apply -f "$backup/templates.updated.json"
printf 'Request Template backup: %s\n' "$backup"
```

Python 3 is needed only on the admin jump host for this JSON edit. There are no
API calls in the script. Retained resource versions make concurrent changes
fail; refresh and review rather than bypass a conflict. Do not blindly reapply
older site-networks files that lack the new field or contain obsolete profiles.

```sh
oc -n vm-provisioning get template vm-provision-request \
  -o jsonpath='{range .parameters[*]}{.name}{"\t"}{.displayName}{"\n"}{end}'
```

Expect TARGET_NAMESPACE / Target VM namespace plus the previous five inputs.
This inspection does not submit a request.

## Enroll each approved target once

The platform team creates the target namespace and its approved NADs through
its normal process. This repository does not automatically create/copy them.
Inspect owners of existing poc-small/medium/large and RBAC before applying.

tekton/target-namespace.yaml produces a namespace-scoped Role, RoleBinding and
three namespaced instance types. The RoleBinding names the central
vm-provisioning/vm-provision ServiceAccount, not a target ServiceAccount.
No target Secret, Pipeline/Task, source disk or new source-image grant is created.

```sh
read -r -p 'Approved target namespace: ' TARGET_NS
oc get namespace "$TARGET_NS"
oc -n "$TARGET_NS" get network-attachment-definitions.k8s.cni.cncf.io

oc process --local -f tekton/target-namespace.yaml \
  -p "TARGET_NAMESPACE=$TARGET_NS" \
  -p PROVISIONING_NAMESPACE=vm-provisioning -o yaml > /tmp/vm-target-setup.yaml
# Review the rendered resources and existing ownership first.
oc apply --dry-run=server -f /tmp/vm-target-setup.yaml &&
oc apply -f /tmp/vm-target-setup.yaml
```

Do not apply to unapproved/system projects. The Task rejects default, openshift,
openshift-* and kube-* targets. The setup Template is an administrator tool,
not an admission policy or user-facing entry point.

```sh
SA=system:serviceaccount:vm-provisioning:vm-provision
oc auth can-i create virtualmachines.kubevirt.io -n "$TARGET_NS" --as="$SA"
oc auth can-i get network-attachment-definitions.k8s.cni.cncf.io \
  -n "$TARGET_NS" --as="$SA"
oc -n "$TARGET_NS" get virtualmachineinstancetypes.instancetype.kubevirt.io \
  poc-small poc-medium poc-large --as="$SA"
```

Source DataSource/PVC checks and clone authorization still use the central
identity in the real source namespace. Do not grant the target's default SA
broad access. Quota/admission, storage and guest boot still need live tests.

## Boundaries and recovery

The central SA gets only VM get/create plus dependency reads in enrolled targets.
It cannot create namespaces, roles, NADs or Secrets. Preflight checks permissions,
NAD/instance types and duplicate VM/root-disk names before IPAM. The target and
selected network settings are frozen for the request, not changed mid-run.

This is for trusted platform operators, not multi-tenant authorization. Anyone
with request access can choose targets permitted to the central SA; human
identity is not impersonated. Arbitrary central Pod/TaskRun rights expose
credentials. Never grant cluster-admin to the provisioning SA.
No automatic retries, rollback, records/disks deletion or namespace creation.
Preserve logs and reconcile uncertain IPAM writes before another request.
Keep unapproved profiles disabled.

References:
- https://kubernetes.io/docs/reference/access-authn-authz/rbac/
- https://kubernetes.io/docs/concepts/workloads/pods/downward-api/
- https://tekton.dev/docs/pipelines/workspaces/
