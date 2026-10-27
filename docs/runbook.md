# Client setup: golden image and direct Infoblox

Tekton calls Infoblox NIOS WAPI directly. No AAP dependency is used.
The six-field request covers name, size, count, SSH key, labels and target VM
namespace. Root disks remain 110Gi / 500Gi / 1Ti. Guest SSH keys are added to an
existing approved account without changing its password/groups/sudo policy.

**Already installed:** use [target-namespaces.md](target-namespaces.md) for the
additive multi-namespace upgrade. Do not reinstall catalog resources or overwrite
site settings merely to add the new field. Keep your already-built direct 0.3
helper image; this change does not modify its code or build context.

## 1. Checkout and prerequisites

Use the main branch. Preserve existing local image/display-name edits, ignored
site.yaml and site-networks files. In an existing main checkout:

```sh
git pull --ff-only --autostash origin main
git status --short
git diff --name-only --diff-filter=U
```

Stop on conflicts. Do not reset/clean or apply old manifests over the updated
ones. A fresh installation can instead clone main to an unused directory.

Require Virtualization/CDI, Pipelines with tekton.dev/v1, a working internal
registry, prepared golden DataSource, compatible storage and approved NADs/node
bridges. Default central execution namespace is vm-provisioning. Read the actual
source Template/DataSource/PVC before installation. Do not modify source disks.

The pipeline uses its own central rhel-standard Template, not the original
golden-image catalog Template. Check BIOS vs UEFI, machine type and disk bus.
This version specifies BIOS and VirtIO with boot order 1. A UEFI source requires
a reviewed change. MACHINE_TYPE comes from the working VM, not an OS-version guess.

A 104Gi source fits 110Gi, 500Gi and 1Ti. These are binary quantities: 110G decimal
would be too small. Preflight requires the target disk to fit the source and
matches source/target StorageClass and volumeMode as a conservative project guard.
Guest filesystem/LVM growth still needs its own approved policy and validation.

## 2. Local site settings

```sh
test -f site.yaml || cp config/site.example.yaml site.yaml
```

All ConfigMap values must be strings. Fill the existing local file rather than
replacing it with a new example. No client configuration belongs in public Git.

| Setting | Required value |
| --- | --- |
| VM_NAMESPACE | Default VM destination for old/CLI requests without a target parameter; normally vm-provisioning. |
| NETWORK_ATTACHMENT | Legacy single-network NAD name; per-network requests use their selected profile instead. |
| DATASOURCE_NAME / DATASOURCE_NAMESPACE | Approved custom golden DataSource and its namespace. |
| STORAGE_CLASS / ACCESS_MODE / VOLUME_MODE | Approved source/target storage; ReadWriteOnce or ReadWriteMany, Filesystem or Block. |
| MACHINE_TYPE | Approved machine type from the working BIOS VM. |
| GUEST_USER | Existing non-root account allowed by the image's SSH policy. |
| IPAM_URL | HTTPS Infoblox origin, no credentials, path or /wapi suffix. |
| INFOBLOX_WAPI_VERSION | Supported WAPI version, quoted as a string. |
| INFOBLOX_NETWORK | Parent IPv4 subnet in CIDR notation. |
| INFOBLOX_NETWORK_VIEW / INFOBLOX_DNS_VIEW | Approved views containing the network/zone. |
| STATIC_START / STATIC_END | Inclusive bounded allocation pool approved for provisioning. |
| GATEWAY / DNS_SERVERS / DOMAIN | Gateway, comma-separated IPv4 DNS servers, and DNS suffix. |
| DHCP_START / DHCP_END / DHCP_DISABLED | Actual DHCP range and "false", or remove both range keys and use "true" when DHCP is absent. |

Per-network profiles use the distinct complete-key contract in
[network-catalog.md](network-catalog.md), including empty DHCP keys when absent.
Profile updates are admin operations. The selected NAD must exist in the target
VM namespace and represent the same VLAN/IPAM network there.

Unknown Infoblox fields can remain placeholders while definitions are installed.
Do not submit requests or invent credentials. Do not change configuration during
active runs. Remove obsolete AAP fields and CLOUD_USER_SUDO from old site files.

Guest bootstrap uses users: [] plus a key-only first-boot command. It does not
create a fallback user, alter password lock state, change groups/sudo or rewrite
sshd. It preserves existing keys, enforces .ssh permissions and restores SELinux
contexts. The selected account and tools must exist in the golden image. See
[golden-image.md](golden-image.md) for guest acceptance checks.

## 3. Image references

For an already-built direct helper:

```sh
oc -n vm-provisioning get istag vm-ipam:0.3 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
oc -n openshift get istag cli:latest \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Set the approved helper/CLI digests in tekton/task.yaml. The CLI must have bash
and match the cluster release. The old 0.2 helper called AAP: do not use it.

Only when direct 0.3 has not been built, set BASE_IMAGE in build/buildconfig.yaml
to an approved UBI Python 3.12 digest/mirror, then build only required source:

```sh
oc apply -k build
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow --wait
rm -rf "$stage"
```

No pip, Ansible, SDK or API credentials are needed in the image. Review runtime
lifecycle and scan images with approved tools. Registry/build proxy/CA settings
are separate from runtime HTTPS. Optional HTTP_PROXY, HTTPS_PROXY, NO_PROXY
values belong in the local site settings. Never disable TLS verification.

## 4. Credentials and trust

Only reserve-ip receives central Secret vm-ipam-credentials, with keys
INFOBLOX_USERNAME and INFOBLOX_PASSWORD. Use an approved scoped API account,
delivered through the secure secret process, never Git or form fields.

For a private Infoblox CA, install the optional approved PEM bundle:

```sh
oc -n vm-provisioning create configmap vm-ipam-ca \
  --from-file=ca-bundle.crt=/approved/path/infoblox-ca-bundle.pem
```

The helper uses SSL_CERT_FILE when the mounted bundle exists, otherwise its
normal trust store. Later settings/credentials/CA updates need no image rebuild.
Do not copy this Secret or CA into every VM namespace: execution stays central.

## 5. Fresh installation: definitions only

Review ownership before applying. Do not overwrite existing GitOps-managed
resources. Source-image Roles/RoleBindings in tekton/serviceaccount.yaml must
cover the actual DataSource/PVC namespaces and CDI source clone permission.

```sh
oc apply --dry-run=server -k catalog &&
oc apply --dry-run=server -f site.yaml &&
oc apply --dry-run=server -k tekton &&
oc apply --dry-run=server -f tekton/request-template.yaml &&
oc apply -k catalog &&
oc apply -f site.yaml &&
oc apply -k tekton &&
oc apply -f tekton/request-template.yaml
```

These install only definitions and RBAC, not a PipelineRun, VM or Infoblox record.
The source image and original golden-image Template stay unchanged. For other
VM namespaces, [enroll each approved target](target-namespaces.md) once with its
NADs, three namespaced instance types and scoped central-SA RoleBinding.
Create per-network entries using [network-catalog.md](network-catalog.md).

## 6. Operator and acceptance path

Keep the catalog Project and native request form's top Namespace on vm-provisioning.
Choose the network entry, then enter name, size, count, SSH key, optional labels
and Target VM namespace. The request runs centrally; VMs/disks go to that target.
A missing target parameter in an older request uses site VM_NAMESPACE.

Do not click Create until Infoblox settings, credentials, profiles, permissions
and TLS are ready. Start with one approved small VM. Follow PipelineRuns in the
central namespace and VM readiness in the destination. Counts 2-5 append -001,
-002, etc. Labels apply to VM metadata before creation, not VMI/disks. Invalid or
Template-owned labels are rejected before allocation.

Check runtime RBAC and actual non-admin access separately. This is a trusted
platform-operator service, not a multi-tenant authorization portal. Anyone with
request access can use any target allowed to the central SA; the pipeline does
not impersonate the human. Arbitrary central Pod/TaskRun rights expose credentials.
Do not grant cluster-admin to the provisioning SA or use labels as authorization.

Verify Infoblox DNS/IP records, CDI clone/PVC, boot, NoCloud, unique machine ID/
SSH host keys, guest account policy, SSH, labels and filesystem capacity.
Pipeline success means VM resources were created, not that guest readiness or
hardening passed. No additional automatic guest changes are part of this update.

## Failure handling and tests

The helper's direct record:host/bounded nextavailableip behavior is unchanged.
It creates DNS host records, not DHCP reservations or MAC-based DHCP registration.
IPAM and Kubernetes are not one transaction: errors can leave reservations or
partial batches. Preserve logs/receipts before Pod cleanup; emptyDir is temporary.
No blind retries, namespace creation, rollback or automatic deletions occur.

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI requires oc for native rendering/labels/Kustomize and mocks remote calls.
A successful CI run is not a live customer Infoblox/clone/guest acceptance test.
