# Central provisioning into approved VM namespaces

The PipelineRun, Task, ServiceAccount, helper image, configuration, network
profiles, credentials and rhel-standard Template stay in **vm-provisioning**.
Only VMs and their generated DataVolumes/PVCs go to the chosen destination.
No second pipeline, controller, helper build or Infoblox credential copy is needed.

## Operator form

Keep the native form's top **Namespace** selector on **vm-provisioning**.
Use the new **Target VM namespace** text field for the VM destination.
All five network catalog entries retain their fixed NAD/IPAM profile and their
existing name, size, count, SSH key and labels inputs. There are now six inputs.
The default target is the provisioning namespace. CLI requests that omit the
new `target-namespace` parameter preserve the site's `VM_NAMESPACE` default.
Do not edit that shared ConfigMap for each request.

The chosen NAD name must exist in the target namespace and refer to the same
VLAN as the approved central network profile. A matching name alone does not
prove equivalent NAD/node-bridge/uplink configuration. Review network setup in
each target. No namespace prefix is added to the Infoblox hostname: DNS names
must remain unique in the selected DNS view/domain, even across namespaces.

## Upgrade the existing installation

Do not submit requests during the update. Let existing runs finish or investigate
them before proceeding. This upgrade does not change source disks, existing VMs,
network ConfigMaps, credentials or the built helper. It is not a live VM migration.

From the existing main checkout, preserve local image/display-name edits:

```sh
cd ~/vm-pipelines-rhel9
git pull --ff-only --autostash origin main
git status --short
git diff --name-only --diff-filter=U
grep -A2 -E 'name: (cli-image|ipam-image)$' tekton/task.yaml
```

Stop on merge conflicts or image placeholders. Do not reset/clean the checkout.
Keep the locally selected CLI and direct vm-ipam:0.3 digest references. Do not
apply upstream files with placeholder defaults over the running Task.

Update the shared execution definitions only:

```sh
oc apply --dry-run=server -f tekton/task.yaml -f tekton/pipeline.yaml &&
oc apply -f tekton/task.yaml -f tekton/pipeline.yaml
```

Existing request Templates were generated earlier, so updating the authoring
file alone cannot change the five installed forms. Use the small **offline**
upgrade script below. It reads JSON and adds just the new parameter/binding to
Templates that create vm-provision PipelineRuns. It preserves their names,
custom display names, old fields, ServiceAccount and fixed network workspaces.
It emits no ConfigMaps and skips the underlying VM/golden-image Templates.

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

Python 3 is only required on the admin jump host for this one-time JSON edit;
there is no new runtime image dependency. No API calls occur inside the script.
Resource versions are retained so concurrent updates fail rather than silently
being overwritten. On a conflict, fetch a fresh snapshot and inspect the change.
Do not rerun stale apply output to bypass a conflict.

The shared rhel-standard VM Template and source-image RBAC need no changes.
The old ignored `site-networks/` YAML files still represent their earlier form
version. Do not blindly reapply them: either update their Template objects with
the new field/binding, or repeat this additive form-upgrade after reapplying them.
Do not regenerate/overwrite their ConfigMaps just to update request forms.

Inspect an installed form without starting a request:

```sh
oc -n vm-provisioning get template vm-provision-request \
  -o jsonpath='{range .parameters[*]}{.name}{"\t"}{.displayName}{"\n"}{end}'
```

Expect TARGET_NAMESPACE / Target VM namespace in addition to the original five
fields. Reload the browser form. Do not click Create before Infoblox is ready.

## Enroll each approved target once

The platform team creates the destination namespace and its NADs through its
normal process. This repository does not create or copy either automatically.
Review existing resources/owners before applying the target setup, especially
poc-small, poc-medium and poc-large if they are already managed by another app.

The admin authoring Template `tekton/target-namespace.yaml` produces only a
namespace-scoped Role, RoleBinding and the three namespaced instance types. The
RoleBinding names **vm-provisioning/vm-provision**, not a ServiceAccount in the
target. No target Secret, Pipeline/Task, source disk, or source clone grant is
created. The existing central SA's source-image RoleBinding remains in place.

```sh
# Use a real, approved existing namespace; do not enter a system namespace.
read -r -p 'Approved target namespace: ' TARGET_NS
oc get namespace "$TARGET_NS"
oc -n "$TARGET_NS" get network-attachment-definitions.k8s.cni.cncf.io

oc process --local -f tekton/target-namespace.yaml \
  -p "TARGET_NAMESPACE=$TARGET_NS" \
  -p PROVISIONING_NAMESPACE=vm-provisioning -o yaml > /tmp/vm-target-setup.yaml
# Review this file and existing ownership before continuing.
oc apply --dry-run=server -f /tmp/vm-target-setup.yaml &&
oc apply -f /tmp/vm-target-setup.yaml
```

Do not apply this setup to kube-*, openshift*, default, or unapproved projects.
The provisioning Task rejects default, openshift, openshift-* and kube-* targets.
The template itself is an administrator tool, not a policy or user entry point.

Check the effective runtime permissions and actual local resources:

```sh
SA=system:serviceaccount:vm-provisioning:vm-provision
oc auth can-i create virtualmachines.kubevirt.io -n "$TARGET_NS" --as="$SA"
oc auth can-i get network-attachment-definitions.k8s.cni.cncf.io \
  -n "$TARGET_NS" --as="$SA"
oc -n "$TARGET_NS" get virtualmachineinstancetypes.instancetype.kubevirt.io \
  poc-small poc-medium poc-large --as="$SA"
```

The source DataSource/PVC checks still happen in their real source namespace;
preflight checks source clone authorization with the same central identity.
Do not grant the destination's default ServiceAccount broad access as a shortcut.
Quota/admission, storage cloning and guest boot still require a controlled live
acceptance test; RBAC checks alone do not prove provisioning will complete.

## Boundaries and failure behavior

The central SA gets only VM get/create plus dependency reads in each enrolled
target. It cannot create namespaces, grant roles, install NADs or copy Secrets.
Preflight validates the namespace, checks target permissions, local NAD and
instance types, and duplicate VM/root-disk names before calling Infoblox.
It freezes the validated destination in the workspace; creation uses that
snapshot, not a later site ConfigMap value. Network snapshots remain unchanged.

This is for **trusted platform operators**, not a multi-tenant authorization
portal. A requester able to use the central pipeline can select any target the
central SA is allowed to manage. There is no impersonation of the human caller.
RBAC grants to the central SA determine allowed targets; review its existing
permissions and do not add cluster-admin. Arbitrary TaskRun/Pod creation in the
execution namespace can expose credentials, as before.

Failure does not trigger automatic retries, namespace creation, record deletion
or VM cleanup. Keep the request logs and reconcile any reserved IPs after errors.
Keep all pending network profiles disabled until their Infoblox settings are ready.

References:
- https://kubernetes.io/docs/reference/access-authn-authz/rbac/
- https://kubernetes.io/docs/concepts/workloads/pods/downward-api/
- https://tekton.dev/docs/pipelines/workspaces/
