# Client setup: golden image and direct Infoblox

Tekton calls Infoblox NIOS WAPI directly. No AAP dependency is used.
The five-field catalog request, labels, dedicated ServiceAccount and IPAM
helper remain. The pipeline now requests **110Gi / 500Gi / 1Ti** root disks
and adds the requester's key to an existing approved guest account.

## 1. Get the current main without losing local configuration

Use `main`, not a feature branch. For a clean, already-cloned main checkout:

```sh
git status --short
git branch --show-current
git fetch origin
git pull --ff-only origin main
```

Only pull into a clean main checkout. If the old checkout has local image
edits, an ignored site.yaml, or a detached worktree, preserve it. The simplest
fresh-install path is a separate directory on **main**:

```sh
git clone --branch main https://github.com/sigtom/vm-pipelines.git vm-pipelines-rhel9
cd vm-pipelines-rhel9
git log -1 --oneline
```

Do not reset/clean the old checkout or blindly reapply its Task manifest. Save
its site configuration and image references outside the new tracked files.
Do not commit client data or exported live Templates to the public repository.

**No helper rebuild for this golden-image update:** scripts/ipam.py,
Containerfile and the build context are unchanged from direct release 0.3.
Reuse the verified vm-ipam:0.3 image digest. Release 0.2 was the AAP caller and
must not be used. See the build section only if 0.3 has not been built yet.

## 2. Inspect the approved source before installation

Require Virtualization/CDI, Pipelines with tekton.dev/v1, Builds/internal
registry, an existing bridged NAD and a prepared RHEL golden DataSource.
The default execution and VM namespace is vm-provisioning.

Read the live golden Template and DataSource. Verify BIOS vs UEFI, machine
type, disk bus, and the source PVC's namespace, capacity, class and volume mode.
The pipeline uses its own rhel-standard Template; the DataSource does not
import a catalog Template's VM settings automatically.

This version explicitly uses **BIOS**, VirtIO boot disk/NIC, boot order 1,
and the admin-configured MACHINE_TYPE. Set that to the working VM's approved
machine type. A UEFI source requires a reviewed Template change before use.
Do not assume an operating-system version determines the machine type.

The target disk must be at least as large as the source. This project's
preflight also requires source and target StorageClass/volumeMode to match.
A **104Gi** source fits small **110Gi**, medium **500Gi**, and large **1Ti**.
Sizes are binary (1Ti = 1024Gi); do not replace 110Gi with decimal 110G.
CPU/RAM remain small 2/4Gi, medium 4/8Gi, and large 8/16Gi.

A NAD does not establish the host bridge/uplink. Verify the chosen VLAN on
all eligible nodes. Existing source disks, golden Templates and VMs must not
be altered during installation.

## 3. Prepare local site configuration

```sh
test -f site.yaml || cp config/site.example.yaml site.yaml
```

Edit the ignored site.yaml. All ConfigMap values are strings. Do not overwrite
an existing file without saving it. Copy only approved settings from an old
checkout; remove obsolete AAP fields and CLOUD_USER_SUDO.

| Setting | Required value |
| --- | --- |
| VM_NAMESPACE | Execution/VM namespace, default vm-provisioning. |
| NETWORK_ATTACHMENT | Existing NAD in that namespace. |
| DATASOURCE_NAME | Custom approved golden DataSource, not a similarly named stock source. |
| DATASOURCE_NAMESPACE | Actual DataSource namespace; inspect its source PVC namespace too. |
| STORAGE_CLASS | Approved target class, matching source for this MVP. |
| ACCESS_MODE | ReadWriteOnce or ReadWriteMany, supported by storage. |
| VOLUME_MODE | Filesystem or Block, matching the source. |
| MACHINE_TYPE | Approved machine type from the working BIOS VM. No inferred default. |
| GUEST_USER | Existing non-root account in the image, permitted by SSH policy. |
| IPAM_URL | HTTPS Infoblox origin, without credentials, path or /wapi suffix. |
| INFOBLOX_WAPI_VERSION | Appliance-supported version; quote it, for example "2.12". |
| INFOBLOX_NETWORK | Parent IPv4 subnet in CIDR form. |
| INFOBLOX_NETWORK_VIEW | Approved network view used for allocation. |
| INFOBLOX_DNS_VIEW | DNS view containing the existing zone. |
| STATIC_START, STATIC_END | Inclusive static pool approved for provisioning. |
| GATEWAY | Usable subnet gateway outside the static/DHCP pools. |
| DNS_SERVERS | Comma-separated IPv4 DNS addresses. |
| DOMAIN | Search domain / suffix for the VM's FQDN. |
| DHCP_START, DHCP_END | Real DHCP bounds, not overlapping the static pool/infrastructure addresses. |
| DHCP_DISABLED | "false" with the range, or exactly "true" with both range keys removed. |

Infoblox network settings are administrator-supplied, not discovered or synced
into this ConfigMap. The subnet and NAD must describe the same network. Unknown
Infoblox fields can remain placeholders while definitions are prepared, but
**do not submit a request** until those fields, credentials and trust are ready.
Do not change configuration while a provisioning request is running.

### Existing guest account, not a new cloud-user

The native VM Template uses users: [] and a small first-boot key-install command.
It requires GUEST_USER to exist, appends the public key without removing existing
keys, sets .ssh ownership/permissions, and restores SELinux contexts. It does not
create a new account, lock/unlock a password, add groups, write sudo policy or
rewrite sshd configuration. The source image still owns those policies.

Remove CLOUD_USER_SUDO from site files. A legacy "false" value is tolerated;
requests asking to change sudo policy are rejected rather than silently ignored.
Missing accounts or incompatible SSH policy must be fixed through the approved
image process. No fallback user or root access is enabled.

Guest partition/LVM/filesystem growth is separate from the PVC size. Inspect the
guest layout before allocating extra capacity; this update adds no pvresize or
lvextend command. See [golden-image.md](golden-image.md) for acceptance checks.

## 4. Select existing images, or build direct release 0.3

For an already-built direct helper:

```sh
oc -n vm-provisioning get istag vm-ipam:0.3 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
oc -n openshift get istag cli:latest \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Pin those approved digest references in the ipam-image and cli-image defaults
in tekton/task.yaml. The CLI must contain bash and match the client release.
No image choices appear in the request form. Do not use the AAP helper digest.

Only when direct release 0.3 has not been built, set BASE_IMAGE in
build/buildconfig.yaml to an approved UBI Python 3.12 image/mirror by digest:

```sh
oc apply -k build
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow --wait
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.3 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

No pip, SDK, Ansible, IPAM account, or Infoblox connectivity is needed to build.
Review runtime lifecycle and scan images with approved tools. Registry/build
proxy and trust are separate from runtime API traffic. Runtime HTTP_PROXY,
HTTPS_PROXY and NO_PROXY can be supplied in site.yaml as required; include
cluster API/internal endpoints appropriately. Never disable certificate checks.

## 5. Credentials and certificate trust

The reserve-ip step alone receives Secret **vm-ipam-credentials**, with keys
**INFOBLOX_USERNAME** and **INFOBLOX_PASSWORD**. Use a scoped API account from the
Infoblox administrators, delivered through the approved secret store or secure
admin process. Do not put credentials in Git, site.yaml, form fields or logs.
Do not create dummy credentials just to run a request.

For a private Infoblox CA, create the optional ConfigMap:

```sh
oc -n vm-provisioning create configmap vm-ipam-ca \
  --from-file=ca-bundle.crt=/approved/path/infoblox-ca-bundle.pem
```

The Task mounts it read-only and sets SSL_CERT_FILE when the bundle exists.
Otherwise the helper uses its image's trust store. Settings, credentials and
mounted CA changes do not require an image rebuild.

## 6. Fresh install: definitions only

Review field ownership before applying. If resources exist unexpectedly, inspect
them first; do not overwrite a GitOps-managed resource. Source-image roles in
tekton/serviceaccount.yaml must cover the actual DataSource/source PVC namespaces
and CDI clone-source authorization. No new cluster-wide rights are needed.

After filling the OpenShift settings and both image digests:

```sh
oc apply --dry-run=server -k catalog
oc apply --dry-run=server -f site.yaml
oc apply --dry-run=server -k tekton
oc apply --dry-run=server -f tekton/request-template.yaml

oc apply -k catalog
oc apply -f site.yaml
oc apply -k tekton
oc apply -f tekton/request-template.yaml
```

These install Templates, instance types, ConfigMap, ServiceAccount/RBAC, Task and
Pipeline. They do not create a PipelineRun, VM, or Infoblox record. Provisioning
remains blocked until Infoblox settings and credentials are ready. Do not click
the catalog form's final Create button or create the example PipelineRun yet.

The original golden-image Template is not modified. The pipeline's rhel-standard
Template is a separate resource. For an existing pipeline installation, update
its native VM Template, Task, request Template and site configuration together
after review; existing VMs are not automatically reconfigured.

## 7. One approved request and recovery

Verify WAPI version, views, DNS zones, pools, permissions, HTTPS connectivity and
certificate trust first. Check the actual operator's permissions. A cluster-admin
test does not prove non-admin access. Arbitrary Pod/TaskRun creation can expose
mounted credentials; restrict to trusted platform operators. Hidden form fields
are not an authorization boundary.

Use **Ecosystem -> Software Catalog -> Request RHEL VM -> Instantiate Template**
in vm-provisioning. Start with one unused name, small, a real ed25519 public key,
and optional labels like foo=bar;boo=far. Creating the request allocates real IPs.
Track progress under Pipelines -> PipelineRuns; Topology may be the first redirect.
Count 1 uses the exact name; counts 2-5 append -001, -002, etc.

Labels are added to each VM's metadata before creation. Invalid/duplicate labels
and attempts to replace Template-managed keys fail before IP allocation. Labels
do not alter VMI/disk metadata and must not serve as an authorization mechanism.

Verify the Infoblox record, allocated address, CDI clone, guest boot, NoCloud,
unique machine ID/host keys, existing-account policy, SSH and disk capacity.
Pipeline success means VM resources were created, not that guests are ready.
The key-only guest path and new sizes require a controlled live test before
wider use even when offline tests pass.

IPAM and Kubernetes are not transactional. Preserve logs and the allocations
receipt before Pod cleanup (emptyDir is not durable). A timeout may occur after
Infoblox created a host, and a batch can leave allocations without VMs. Inspect
names, records and disks before recovery; no automatic retries, rollback or
deletion are performed.

The helper still uses record:host and bounded func:nextavailableip allocation.
It creates DNS host records, not DHCP fixed-address reservations. The VM MAC is
used in guest networking, not registered for DHCP. No API behavior changes were
made for the new golden image. Salt, subscriptions, AD and security-agent setup
remain separate customer-owned integrations.

## Tests

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI requires oc for native processing, label application and Kustomize. Tests
exercise 104Gi-source fit, disk-size boundaries, hardware fields, key-only guest
bootstrap in a temporary home, and mocked IPAM behavior. They do not replace
live checks of the actual image, cluster and Infoblox appliance.
