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

Use **scripts/prepare-namespace.py** on the admin jump host. It copies selected
NADs from the central namespace and processes the existing
**tekton/target-namespace.yaml** for the Role, RoleBinding and three sizes.
It does not install another pipeline or change the IPAM helper image.

### Prerequisites

- The platform team creates the target project/namespace through its normal
  process, including team access, quotas and policies. It must be Active.
- Python **3.9+**, `oc` on PATH, and an admin login to the intended workload
  cluster. No Python packages, SDK, jq or new container are required.
- The admin must be able to read the source NADs/central ServiceAccount, read
  and create the target resources, grant the scoped Role, and impersonate the
  central ServiceAccount for the final permission checks. The script does not
  grant the admin or the provisioning ServiceAccount any cluster-wide rights.
- Source NADs and the `vm-provision` ServiceAccount already exist in
  `vm-provisioning`. Select only networks approved for this target.

### Preview, then create missing resources

Use real NAD names in place of the documentation examples below. The source
namespace defaults to **vm-provisioning**. `--source-namespace` overrides both
where NADs are read and the namespace of the central provisioning ServiceAccount.

```sh
# Read/compare resources and run server-side create validation; no writes.
python3 scripts/prepare-namespace.py team-apps \
  --networks vlan-101 vlan-102 vlan-103 --dry-run

# Run only after reviewing the displayed context and CREATE/MATCH plan.
python3 scripts/prepare-namespace.py team-apps \
  --networks vlan-101 vlan-102 vlan-103 --apply
```

Omitting both mode flags defaults to dry-run. The target must already exist;
source-equals-target, default, openshift, openshift-* and kube-* targets are
rejected. There is deliberately no option to copy every NAD automatically.
The script pins the current oc context for its calls and checks all selected
sources and existing destination resources before the first create.

For three selected networks, a new target receives **eight resources**:
three NADs, `poc-small`/`poc-medium`/`poc-large`, Role `vm-provision-target`, and
RoleBinding `vm-provision-target`. The binding grants access to the existing
**vm-provisioning/vm-provision** identity. A new RoleBinding is created last.
No target ServiceAccount, ConfigMap, Secret, VM, disk, PipelineRun or IPAM record
is created. The source namespace/resources and existing VMs remain untouched.

### Repeat runs and conflicts

- Missing objects are server-dry-run validated, then created only with `--apply`.
- Matching objects are left untouched, including their existing owners and labels.
- Different NAD configs/resource annotations, size specs, Role rules or binding
  subjects cause a nonzero exit with a diff. Nothing is overwritten or adopted.
- A read/API/authorization failure is never treated as a missing resource.
  Creates use `oc create`, not apply/replace, so a concurrent create cannot be
  overwritten. No force, automatic deletion, rollback or automatic retry exists.

The NAD copy preserves **spec verbatim** and the operational
**k8s.v1.cni.cncf.io/resourceName** annotation when present. It creates fresh
name/namespace metadata; no source labels, other annotations, UIDs, resource
versions, owner references, finalizers or status are copied. This avoids
transferring GitOps/Helm ownership. Review NADs needing other annotations before
using this utility. Configurations containing **netAttachDefName** are rejected
because they require a namespace-specific design, not a blind copy. The current
bridge NAD workflow does not need that field.

Comparison ignores JSON whitespace/key order in NAD config, Role rule ordering,
ServiceAccount-subject apiGroup defaults and API bookkeeping. Other spec changes
are conflicts, even if the difference might be an equivalent API default; review
rather than force an overwrite. Existing tracking labels alone are not conflicts
because matching resources are never updated.

On successful apply, the script reads back the resources and verifies VM
get/create plus the required dependency reads as the central ServiceAccount.
Expect **PREPARED: <namespace>**. This does not prove VM admission, quota, node
bridge/uplink configuration, network connectivity, cloning or guest boot. Run one
approved VM request separately. A missing impersonation permission or failed
verification returns an error, not a readiness claim.

Copies are **not automatically synchronized** after source NAD changes. A later
run detects differences and stops. The operation is not transactional: an API
failure, timeout or interrupted session can leave some creates completed. Inspect
that target before rerunning; a rerun keeps matching completed objects and can
create the remaining ones. No existing object is removed as recovery.

When NADs are managed separately, the existing manual setup remains available:
process `tekton/target-namespace.yaml` with TARGET_NAMESPACE and
PROVISIONING_NAMESPACE, then review/dry-run/apply its output. Do not overwrite
resources owned by another management process. Source DataSource/PVC checks and
clone authorization still use the central identity in the source-image namespace;
no new source-image grant or destination default-ServiceAccount grant is needed.

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
- https://kubernetes.io/docs/reference/kubectl/generated/kubectl_create/
- https://docs.redhat.com/en/documentation/openshift_container_platform/4.21/html/virtualization/networking
- https://kubernetes.io/docs/concepts/workloads/pods/downward-api/
- https://tekton.dev/docs/pipelines/workspaces/
