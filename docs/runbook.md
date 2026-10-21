# Client setup: direct Infoblox

Tekton calls Infoblox NIOS WAPI directly. No AAP endpoint, token, job template,
playbook, artifacts, or callback is used. This restores the direct implementation
while keeping the catalog form, VM labels and native VM provisioning intact.

## Switching from the 0.2 image

`vm-ipam:0.2` contained the AAP caller. The restored direct helper builds as
**vm-ipam:0.3**. Rebuild once and use the returned digest; changing only the
ConfigMap cannot convert the old image into a WAPI caller.

1. Pause request submissions; let existing runs finish or investigate them before
   changing anything. Do not modify existing VMs, disks, NADs or boot sources.
2. Fetch the updated repository. If the checkout has client-specific local edits,
   preserve it and create a separate checkout with `git worktree add --detach
   ../vm-pipelines-direct origin/main` after `git fetch origin`. Do not reset,
   clean, or blindly reapply an old stash over the restored Task.
3. Create a new `site.yaml` from the direct example. Copy only approved OpenShift
   settings from the previous file, then fill the Infoblox fields below. Keep the
   old site file in the original directory; do not overwrite it. Remove AAP
   fields from the new file. No client settings should be pushed to Git.
4. Build `vm-ipam:0.3`, set the Task's new helper digest and approved CLI digest,
   and supply `vm-ipam-credentials` plus any needed `vm-ipam-ca` bundle.
5. Apply the new site ConfigMap, Task, Pipeline and request Template together.
   Existing native VM Template/instance types/RBAC need no changes for this
   switch. Use the fresh-install section only for resources not installed yet.
6. Do not submit a VM until network settings, WAPI permissions, TLS and DNS policy
   have been verified with the Infoblox team. Any obsolete AAP Secret/CA remains
   unused; remove it only through the approved process, not as an automatic step.

## 1. Prerequisites

Require OpenShift Virtualization/CDI, Pipelines with `tekton.dev/v1`, Builds,
the internal registry, a prepared RHEL DataSource, compatible CSI storage and a
bridged NetworkAttachmentDefinition. Guest images need cloud-init/NoCloud and
appropriate virtio drivers. Read-only discovery:

```sh
oc version
oc get datasources.cdi.kubevirt.io -A
oc get storageclass
oc get network-attachment-definitions.k8s.cni.cncf.io -A
oc get pipelines.tekton.dev -A
```

Default namespace is **vm-provisioning** for both pipeline execution and VMs.
Have an administrator create it and the appropriate NAD. If changing namespaces,
update metadata, RoleBinding subjects and site configuration consistently. Do not
move the source-image Role into the target namespace with a blanket transformer.

The source must be Ready/Bound, and the target disk at least as large. This MVP
requires matching source/target StorageClass and volume mode as a conservative
guard, not a general CDI limitation. Actual clone, capacity, thin provisioning
and live-migration support still need validation. A NAD does not create the
host bridge or prove VLAN connectivity on all eligible nodes.

## 2. Site configuration

Copy `config/site.example.yaml` to `site.yaml` (ignored by Git). Do not overwrite
an existing site file without saving it. All ConfigMap values must be strings.

| Variable | Required value |
| --- | --- |
| `VM_NAMESPACE` | Execution and VM namespace, default vm-provisioning. |
| `NETWORK_ATTACHMENT` | Existing NAD in that namespace. |
| `DATASOURCE_NAME`, `DATASOURCE_NAMESPACE` | Approved CDI boot source. Check its source PVC namespace too. |
| `STORAGE_CLASS` | Approved target class, matching source for this MVP. |
| `ACCESS_MODE` | ReadWriteOnce or ReadWriteMany, supported by storage. |
| `VOLUME_MODE` | Filesystem or Block, matching the source. |
| `GUEST_USER` | Initial SSH user, default cloud-user. |
| `CLOUD_USER_SUDO` | `false`, or explicitly approved `ALL=(ALL) NOPASSWD:ALL`. |
| `IPAM_URL` | HTTPS Infoblox origin; no credentials, path, query or /wapi suffix. |
| `INFOBLOX_WAPI_VERSION` | Appliance-supported version, such as 2.12 or v2.12, not a guessed default. |
| `INFOBLOX_NETWORK` | Parent IPv4 subnet in CIDR form. |
| `INFOBLOX_NETWORK_VIEW` | Approved network view used for allocation. |
| `INFOBLOX_DNS_VIEW` | Approved DNS view containing the existing zone. |
| `STATIC_START`, `STATIC_END` | Inclusive bounded static pool approved for VM provisioning. |
| `GATEWAY` | Usable subnet gateway, outside the static/DHCP pools. |
| `DNS_SERVERS` | Comma-separated IPv4 DNS addresses. |
| `DOMAIN` | Search domain and suffix for the VM's FQDN. |
| `DHCP_START`, `DHCP_END` | Real DHCP bounds, not overlapping static pool or infrastructure addresses. |
| `DHCP_DISABLED` | `false` with the range, or exactly `true` with both DHCP range keys removed. |

The helper reads these settings from the ConfigMap and allocates an address via
Infoblox. It does not synchronize IPAM data into ConfigMaps or discover effective
gateway/DNS/pool options automatically. The NAD and subnet must describe the same
network. Do not edit this ConfigMap during a running provisioning request.

`CLOUD_USER_SUDO=false` means no new cloud-init sudo rule; it does not revoke
privileges already baked into an image. Template changes do not reconfigure
existing guests. Salt, subscriptions, AD and agents remain separate integrations.

## 3. Build the helper inside OpenShift

Set `BASE_IMAGE` in `build/buildconfig.yaml` to the approved UBI Python 3.12 image
or mirror **by digest**. No pip, Ansible, SDK, WAPI access or credentials are
needed during the build. Use a source directory containing only required files:

```sh
oc apply -k build
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow --wait
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.3 -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Set `ipam-image` in `tekton/task.yaml` to the returned digest. Set `cli-image`
to an approved, client-version-matched OpenShift CLI image with bash, also by
digest. These are administrator settings, not form inputs. Keep release 0.2
unchanged; do not select its digest. Review runtime support lifecycle and scan
both images with the approved tools. Mirror images for disconnected sites.

Build/registry proxy and trust are separate from workload HTTPS traffic.
Optional HTTP_PROXY, HTTPS_PROXY and NO_PROXY values can be supplied in site.yaml;
include cluster API/internal destinations in NO_PROXY where appropriate. Verify
Tekton-to-Infoblox HTTPS connectivity. There is no orchestration-service hop.

## 4. Credentials and certificate trust

The IPAM step reads Secret **vm-ipam-credentials** with two keys:

- **INFOBLOX_USERNAME**
- **INFOBLOX_PASSWORD**

Use an API-enabled, scoped account provided by the Infoblox administrators.
Deliver credentials through the approved secret store/External Secrets process
or a secure administrator procedure. Do not paste them into Git, site.yaml,
form fields, command-line arguments or logs. Other Task steps do not receive
the Infoblox Secret. Do not create dummy credentials just to start a request.

For a private Infoblox CA, install the approved PEM chain/bundle as:

```sh
oc -n vm-provisioning create configmap vm-ipam-ca \
  --from-file=ca-bundle.crt=/approved/path/infoblox-ca-bundle.pem
```

The IPAM step mounts that optional ConfigMap read-only and sets SSL_CERT_FILE
when its bundle exists. Otherwise it uses the normal image trust store. Verify
the CA and server hostname; do not disable TLS checks. Replacing settings,
credentials or this mounted CA later does not require another helper rebuild.
Registry certificates/pull credentials are a separate configuration.

## 5. Install or update definitions without provisioning

Fresh install, after image and site values are set and resource ownership reviewed:

```sh
oc apply --dry-run=server -k catalog
oc apply --dry-run=server -k tekton
oc apply -k catalog
oc apply -f site.yaml
oc apply -k tekton
oc apply -f tekton/request-template.yaml
```

For an already-installed version, leave catalog/RBAC resources alone and update
only the changed definitions after preparing the new helper and site values:

```sh
oc apply --dry-run=server -f site.yaml
oc apply --dry-run=server -f tekton/task.yaml -f tekton/pipeline.yaml \
  -f tekton/request-template.yaml
oc apply -f site.yaml
oc apply -f tekton/task.yaml -f tekton/pipeline.yaml \
  -f tekton/request-template.yaml
```

These commands do not create a PipelineRun, VM or Infoblox record. Do not run a
request with missing placeholders/credentials. No uninstall/reinstall of
Virtualization, Pipelines, storage, the boot source or networking is needed.

The supplied runtime RBAC permits scoped VM creation and dependency reads, not
VM/PVC deletion or cluster-wide reads. Source-image roles must cover the actual
DataSource/source PVC namespaces and CDI clone authorization. Generic TaskRun or
Pod creation rights may expose mounted Secrets; restrict them to trusted
operators. Hidden form fields do not enforce authorization. Verify actual user
permissions; cluster-admin tests do not establish non-admin access.

## 6. One approved VM request

Confirm WAPI version, view/zone names, approved static/DHCP ranges, account
permissions, TLS and connectivity with the Infoblox team first. Use an unused
hostname and one small VM for the first test.

**Ecosystem -> Software Catalog -> Request RHEL VM -> Instantiate Template**
in vm-provisioning. Enter name, size, count, public key and optional labels.
The request fixes `vm-provision` as the Pipeline and ServiceAccount and supplies
`scratch: emptyDir`. After Create, inspect Pipelines -> PipelineRuns; the console
may redirect to Topology. The CLI alternative is the filled-in
`tekton/pipelinerun.example.yaml`. Creating either request allocates real IPs.

Count 1 uses the exact name; higher counts append -001, -002, etc. Confirm the
host record, address, DNS behavior, VM MAC, clone/PVC, guest boot, cloud-init,
SSH and labels. Pipeline success is not a guest-ready or hardening-complete signal.

## Additional VM labels

The optional field accepts `foo=bar;boo=far`; the Pipeline parameter is vm-labels.
Spaces around entries are trimmed. Empty input preserves standard labels;
`key=` is valid. Invalid keys/values, duplicates, empty entries and attempts to
replace native Template labels fail before IP allocation. Labels are placed
on every VM's metadata before creation, not on the VMI, disks or PipelineRun.
Native `oc label --local --overwrite=false` does the merge. No helper/image/RBAC
change is required for labels alone. Update Task, Pipeline and request Template
together; existing VMs are untouched. Do not use requester labels as authorization.

## 7. Failure recovery and current API scope

IPAM and Kubernetes are not transactional. A timeout may occur after Infoblox
created the host; a batch may have allocations without VMs. Preserve Task logs
and the allocations receipt before Pod cleanup; emptyDir is not durable storage.
Inspect existing FQDNs in the configured views, VMs and disks before recovery.
No automatic retries, deletions or rollback are performed. Do not blindly
resubmit a request or delete a reservation because a later VM operation failed.

The restored helper uses NIOS WAPI `record:host` with
`func:nextavailableip:<start>-<end>,<network-view>` and explicit views. It creates
DNS host records in existing zones; it does not create DHCP fixed addresses or
register the VM MAC for DHCP. The guest uses a static address via cloud-init.
Forward/reverse behavior and permissions need appliance-specific validation.

References for the restored implementation:
- Host records: https://ipam.illinois.edu/wapidoc/objects/record.host.html
- Bounded allocation: https://ipam.illinois.edu/wapidoc/objects/record.host_ipv4addr.html
- Native template CLI: https://docs.redhat.com/en/documentation/openshift_container_platform/4.21/html/cli_tools/openshift-cli-oc

## Tests

```sh
python3 -m pip install PyYAML==6.0.3  # Test-only dependency
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI requires oc for real native rendering, local labels and Kustomize tests.
The WAPI calls in tests are mocked. A successful CI run does not prove the
client's API account, DNS policies or actual address allocation work.
