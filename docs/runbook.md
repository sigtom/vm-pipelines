# Client setup and validation

## 1. Prerequisites and scope

Obtain approval before installing resources, allocating an IP, or submitting a
VM request. Do not adopt or overwrite resources owned by another GitOps app.

Required: OpenShift Virtualization/CDI, OpenShift Pipelines (`tekton.dev/v1`),
OpenShift Builds, a working internal image registry, an approved cloneable RHEL
DataSource, compatible CSI storage, and a bridged NetworkAttachmentDefinition
(NAD). The guest needs cloud-init/NoCloud and appropriate virtio drivers.

The supplied layout uses **one execution and VM namespace**, `vm-provisioning`.
An administrator creates it and the needed platform network before installation.
If changing the namespace, update every manifest namespace and RoleBinding
subject consistently. Do not apply a blanket namespace transformer to the
source-image Role: that Role belongs in the source PVC's namespace.

Read-only discovery:

```sh
oc version
oc get datasource -A
oc get storageclass
oc get network-attachment-definitions -A
oc get pipelines.tekton.dev -A
oc get imagestreamtags -n openshift
```

The preflight deliberately requires source and destination to use the same
StorageClass and volume mode. That is a conservative MVP guard, **not** a general
CDI restriction. Verify the source PVC is Bound, DataSource Ready, root disk is
large enough, and an actual clone works. Thin provisioning and live migration
also depend on the storage backend; choosing a StorageClass does not prove them.

## 2. Fill the client settings

Copy `config/site.example.yaml` to `site.yaml` (ignored by Git). It creates
ConfigMap `vm-provision-config`. All values are strings and non-secret.

| Variable | Required value / owner |
| --- | --- |
| `VM_NAMESPACE` | Approved execution/VM namespace; default `vm-provisioning`. |
| `NETWORK_ATTACHMENT` | Existing bridged NAD name in that namespace. |
| `DATASOURCE_NAME` | Approved CDI DataSource name, not an image URL. |
| `DATASOURCE_NAMESPACE` | DataSource namespace; inspect its `status.source.pvc.namespace` too. |
| `STORAGE_CLASS` | Approved target class; must match source for this MVP. |
| `ACCESS_MODE` | `ReadWriteOnce` or `ReadWriteMany`, supported by target storage. |
| `VOLUME_MODE` | `Filesystem` or `Block`, matching source. |
| `GUEST_USER` | Initial local SSH username; default `cloud-user`. |
| `CLOUD_USER_SUDO` | `false` (no cloud-init sudo rule), or explicitly approved `ALL=(ALL) NOPASSWD:ALL`. |
| `IPAM_URL` | HTTPS Infoblox NIOS origin, such as `https://infoblox.example.com`; no `/wapi`, query or credentials. |
| `INFOBLOX_WAPI_VERSION` | Version supported by that appliance, for example `2.12` or `v2.12`. Not a guessed default. |
| `INFOBLOX_NETWORK` | Parent IPv4 subnet in CIDR form. |
| `INFOBLOX_NETWORK_VIEW` | Approved network view used for allocation and host creation. |
| `INFOBLOX_DNS_VIEW` | Approved DNS view containing the existing zone. |
| `STATIC_START`, `STATIC_END` | Inclusive approved static pool inside that subnet; not the whole subnet. |
| `GATEWAY` | Usable subnet gateway, outside allocation and DHCP pools. |
| `DNS_SERVERS` | Comma-separated IPv4 DNS server addresses. |
| `DOMAIN` | DNS search domain / suffix used to form the VM FQDN. |
| `DHCP_START`, `DHCP_END` | Actual DHCP bounds; must not overlap static pool or infrastructure addresses. |
| `DHCP_DISABLED` | `false` with the DHCP range, or exactly `true` with both range keys removed. |

Network settings come from this approved ConfigMap. The helper **does not** read
gateway/DNS/pool metadata from Infoblox or synchronize it into ConfigMaps.
The NAD and IPAM subnet must refer to the same network. Do not change the profile
while a PipelineRun is active; steps receive their configuration independently.

`CLOUD_USER_SUDO=false` does not remove existing privileges baked into a golden
image. Review that image and the customer's administrator-access policy. Existing
VMs are not updated by later cloud-init Template changes.

## 3. Build and select images

The helper uses Python's standard library only. Build dependencies: an approved
**UBI Python 3.12** base image/mirror, the source Containerfile/script, and build
permissions. No pip, Git credential, Infoblox access, or cluster credential is
needed inside the build image. Review the selected runtime's support lifecycle.

Set `BASE_IMAGE` in `build/buildconfig.yaml` to the approved image **by digest**.
Build only the required files, not a working directory containing secrets:

```sh
oc apply -k build
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.1 -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Set the two **administrator-only Task parameter defaults** in `tekton/task.yaml`:

- `cli-image`: approved OpenShift CLI image digest with `bash`, matching the
  client's OCP release. Do not assume an `openshift/cli` tag or cached image exists.
- `ipam-image`: the newly built `vm-ipam` image reference **by digest**. The internal
  registry usually uses `image-registry.openshift-image-registry.svc:5000`, but
  use the actual verified output reference.

No image parameters appear in the request form. Scan both images
using the customer's approved process. Builds on another architecture need a
matching base/CLI image. For offline environments, mirror those images first;
there are no Python wheels to mirror for runtime.

**Proxy and CA:** base-image pulls/builds and runtime API traffic are separate
paths. Verify cluster/build proxy and registry trust. Runtime workloads do not
implicitly acquire the desired proxy settings: optional HTTP(S)_PROXY/NO_PROXY
values can be supplied in the site ConfigMap; include API/internal endpoints in
NO_PROXY. For private Infoblox CAs, mount an approved PEM trust bundle into the
reserve-ip step and set `SSL_CERT_FILE` to its absolute path, or add the approved
CA to the helper image's trust store. Never disable certificate verification.
Registry pull credentials belong in approved imagePullSecrets, not source code.

## 4. Permissions, secrets and installation

The runtime ServiceAccount has only scoped VM create/get and dependency reads.
It cannot delete VMs/PVCs, read namespaces globally, or read Secret values through
the API. Pod creation rights may still permit reading mounted Secrets: restrict
who can create Pods, TaskRuns, PipelineRuns, and editable Templates in this namespace.

In `tekton/serviceaccount.yaml`, put `vm-provision-source-reader` Role and its
RoleBinding in the actual DataSource/source PVC namespace. If they differ, grant
required source reads in both. The clone-source `datavolumes/source` permission
must be evaluated against the source PVC namespace. Verify this with the actual
runtime ServiceAccount before allocating an IP.

Create Secret **`vm-ipam-credentials`** through the approved external-secret store
or a secure admin process. It must contain **`INFOBLOX_USERNAME`** and
**`INFOBLOX_PASSWORD`**. No example credential values are provided in this repo.
Use a scoped NIOS API account approved by the Infoblox administrators.

Once configurations, images, ownership and permissions are approved:

```sh
oc apply -k catalog               # VM Template and instance types only
oc apply -f site.yaml             # Admin settings; does not provision a VM
oc apply -k tekton                # SA/Roles/Task/Pipeline; not a PipelineRun
oc apply -f tekton/request-template.yaml  # Optional catalog request entry
```

Installing the catalog entry does not provision a VM. Instantiating it does.
Do not apply catalog resources already owned by another app; reuse verified
identical instance types or agree on new names first. Never merge this code into
an unrelated platform repo as a prerequisite.

## 5. First approved request

Use a **trusted platform operator**. Check their effective permissions for
Templates, TemplateInstances, request-parameter Secrets and PipelineRuns; testing
as cluster-admin does not prove non-admin access. Do not grant broad `edit` to
untrusted users to make the form work. The catalog path creates a parameter
Secret and TemplateInstance before the PipelineRun. Those are separate from the
provisioning credential Secret. Confirm support status for the combined catalog
workflow under the customer's installed product versions.

Open **Ecosystem -> Software Catalog -> Request RHEL VM -> Instantiate Template**
in `vm-provisioning`. Name and public key are required; size defaults to small,
count to 1, and additional VM labels to empty. Fields are text, not promised
dropdowns. ServiceAccount `vm-provision` and workspace `scratch: emptyDir` are
fixed. After Create, track PipelineRuns; Topology may be the initial redirect.
The explicit alternative is `tekton/pipelinerun.example.yaml`, with the same
execution identity.

Count 1 uses the name unchanged; counts 2-5 append `-001`, `-002`, etc.
The catalog uses `vm-request-<name>` for its PipelineRun name, so resubmitting the
same name is rejected. This is not a recovery mechanism. Begin with one VM and
verify runtime permissions, SCC/image pulls, one IP reservation, CDI clone,
VM Ready, cloud-init, DNS and SSH. Test batch/failure scenarios separately.
**Creating the VM resource is not a guest-ready or hardening-complete signal.**

### Additional VM labels

The fifth form field, **Additional VM labels**, is optional. Enter semicolon-
separated pairs, for example `foo=bar;boo=far`. CLI callers use the optional
Pipeline parameter `vm-labels`, which defaults to `""` for existing callers.
The catalog request Template calls the same parameter `VM_LABELS`.

- A blank field adds no labels. Whitespace around entries, keys and values is trimmed.
- Keys and values must satisfy Kubernetes label syntax. Qualified keys such as
  `example.com/team=ops` and empty values such as `reviewed=` are supported.
- Duplicate keys, missing `=`, empty entries (including a trailing `;`), invalid
  characters/lengths, and any key already present on the approved VM Template
  fail in preflight, before the IPAM step. This protects standard ownership,
  size and OS labels, as well as labels administrators add to the Template.
- The same labels are applied to **VM `metadata.labels`** for every VM in a batch.
  They are not added to the PipelineRun, VMI template, DataVolume or PVC.

The Task passes input through an environment variable, never interpolates it
into executable shell text, and stores canonical label arguments in the scratch
workspace. `oc label --local --overwrite=false` merges them into each rendered
VM immediately before `oc create`. No live patch permission, additional helper,
package or image rebuild is needed. A late label conflict stops creation and
leaves any reservations for deliberate reconciliation, as with other failures.

To roll out this change, preserve the approved image defaults and namespace
settings, then update the Task, Pipeline and request Template together. Do not
apply the sample PipelineRun merely to update the form. Existing VMs remain
unchanged. Validate an approved new VM with `oc get vm <name> --show-labels`;
the offline tests already exercise native form rendering and the real local
label command without allocating an IP or contacting a cluster.

Labels may select backup or other automation policies. This remains a trusted-
operator interface; agree on allowed business labels before broader access.
Kubernetes syntax reference: https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/

## 6. Failure handling and guest ownership

IPAM and Kubernetes do not share a transaction. The helper performs one bounded
host insertion per VM, then reads back the assigned IP. It does not scan for free
addresses. There are no POST retries and no automatic deletion/rollback. The
workspace receipt and logs show completed reservations, but a timeout may occur
after the server wrote a record. Inspect Infoblox by FQDN in the configured views
before resubmitting. Do not assume an error means no allocation occurred.

On a failed batch, some reservations or VMs may exist. Retain logs and inspect
each name, record and root disk. Capture the receipt before Pod cleanup; emptyDir
is ephemeral, not a durable audit store. Do not allocate again to repair a failed
VM create. Resolve the cause and have an administrator reconcile deliberately.
Define ownership, token rotation, backup/restore, retention and decommissioning
procedures before using the service for real workloads.

Cloud-init configures initial networking, hostname and SSH. Salt/bootstrap remains
a disabled placeholder; customer teams must provide package source, enrollment,
trust, subscription, AD and hardening procedures. Do not place those credentials
in VM YAML. Registration may be required before packages are available; do not
clone a registered machine's identity. AD also needs correct DNS/time and an
approved join identity. Existing images may already have security agents that
need unique enrollment during cloning.

## 7. API scope and references

NIOS WAPI only, not Universal DDI/BloxOne. The implementation uses `record:host`
with `func:nextavailableip:<start>-<end>,<network-view>` and explicit DNS/network
views. It creates host DNS records in existing zones, **not** DHCP reservations.
The MAC is used by the VM's guest configuration; it is not registered for DHCP.
Verify zone/forward/reverse DNS policy, permissions, supported WAPI version and
an approved pool with the client before the first mutation. No appliance access
was used to validate the public source.

- NIOS WAPI vendor reference (published mirror): https://ipam.illinois.edu/wapidoc/objects/record.host.html
- Address selection contract: https://ipam.illinois.edu/wapidoc/objects/record.host_ipv4addr.html
- Native template processing: https://docs.redhat.com/en/documentation/openshift_container_platform/4.21/html/cli_tools/openshift-cli-oc
- PipelineRun identity/workspaces: https://tekton.dev/docs/pipelines/pipelineruns/

Use the exact appliance's schema or OpenAPI specification where available; a
newer reference is not proof of support on an older appliance. Tests are offline
contract/behavior checks. Repeat one controlled end-to-end test at the client.
