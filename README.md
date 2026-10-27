# VM Pipelines

**Tekton calls Infoblox NIOS directly. No AAP service, token, job template, or
network-allocation playbook is required.**

Operators request a VM name, size, count, SSH public key, optional labels, and
**Target VM namespace**. A per-network catalog entry fixes the NAD/IPAM profile.
The pipeline reserves addresses, processes a native OpenShift VM Template, and
creates VMs through the Kubernetes API. Individual VMs are not written to Git.

```text
Catalog request runs centrally in vm-provisioning
  -> validate target namespace, permissions, local NAD, instance type and source
  -> direct Infoblox allocation
  -> oc process + oc label --local + oc create in the target VM namespace
  -> CDI disk clone -> guest cloud-init
```

**Existing deployments:** follow [target-namespaces.md](docs/target-namespaces.md)
to update the shared Task/Pipeline and all installed request forms without
resetting network profiles, display names, credentials or local image digests.
Keep the native form's top **Namespace** selector on **vm-provisioning**. Use
**Target VM namespace** for the VM destination; this field is text, not a dropdown.

The helper stays at **vm-ipam:0.3** (direct Infoblox). Its script, Containerfile,
build and IPAM contract have not changed for multi-namespace support. Reuse the
already-built digest; **no helper rebuild**. Never use the old 0.2 AAP image.

## Scope and sizes

| Size | vCPU | Memory | Root disk request |
| --- | ---: | ---: | ---: |
| small | 2 | 4 GiB | 110Gi |
| medium | 4 | 8 GiB | 500Gi |
| large | 8 | 16 GiB | 1Ti (1024 GiB) |

Disk sizes use binary Kubernetes quantities. A 104Gi golden source fits all
three. Preflight rejects a target disk smaller than the source. These are PVC/
virtual disk capacities, not a promise that guest LVM/filesystems grow automatically.

Native VM Template and instance types, one approved RHEL golden DataSource,
BIOS/VirtIO, an approved MACHINE_TYPE, one bridged NIC, static IPv4, requests for
1-5 VMs, and an existing non-root SSH account remain. No custom VM generator,
runtime Git checkout, Ansible installation, SDK, or runtime pip install is used.

**Central versus target:** Task/Pipeline, rhel-standard Template, configuration,
network profiles and credentials stay central. Each target needs an existing
namespace, the same approved NAD names, the three namespaced instance types,
and a scoped RoleBinding for the central ServiceAccount. No duplicate pipeline
or copied IPAM Secret is needed. See [target setup](docs/target-namespaces.md).

**Guest account policy stays with the image.** Cloud-init uses users: [] and a
key-only first-boot command to append the supplied public key to the existing
account's home. It does not create accounts, change password lock state, add
groups/sudo, or rewrite sshd policy. Existing keys remain. The account must
already exist and be allowed by guest SSH policy. CLOUD_USER_SUDO/SUDO_POLICY
are obsolete. See [golden-image.md](docs/golden-image.md).

**Reference implementation, not a turnkey production service.** Target OpenShift
Virtualization 4.21+ and Pipelines serving tekton.dev/v1. Offline tests do not
prove the client's permissions, Infoblox, storage clone or guest boot work.

The helper creates Infoblox DNS host records, not DHCP fixed-address reservations.
The generated MAC configures the guest NIC, not a DHCP reservation. Gateway/DNS/
pool settings are approved admin configuration, not automatically discovered.
Multi-NIC, AD, Salt, subscriptions and agent enrollment remain separate work.

## Configuration

Start with [runbook.md](docs/runbook.md). Copy config/site.example.yaml to ignored
site.yaml. Keep client settings and credentials out of this public repository.

| File or resource | Purpose |
| --- | --- |
| config/site.example.yaml | Default VM destination, golden source, storage, machine type, guest account and direct Infoblox settings. |
| tekton/task.yaml | Approved CLI/helper image digests and shared provisioning logic. |
| tekton/network-catalog.yaml | Admin authoring Template for a disabled network profile and request form. |
| tekton/target-namespace.yaml | Admin authoring Template for scoped target RBAC and instance types. |
| scripts/add-target-namespace.py | Offline additive upgrade of existing request forms; not runtime helper code. |
| tekton/serviceaccount.yaml | Central identity and source-image access. |
| Secret vm-ipam-credentials | INFOBLOX_USERNAME / INFOBLOX_PASSWORD, outside Git. |
| Optional ConfigMap vm-ipam-ca | Approved trust bundle in ca-bundle.crt. |

The five network entries use the same shared pipeline. Profile updates are
administrator operations. NADs must describe the same network in every enrolled
target. Names must remain unique in the selected Infoblox DNS view/domain.

## Operator path

Select project vm-provisioning -> Software Catalog -> the desired Provision VM
network entry -> Instantiate Template. Keep the top Namespace on vm-provisioning.
Enter the VM name, size, count, key, optional labels and target VM namespace.
The request fixes the Pipeline, central ServiceAccount and workspaces.
Track PipelineRuns centrally; view resulting VMs in their target namespace.

Labels use foo=bar;boo=far. Invalid labels, duplicate keys and attempts to replace
Template-owned labels fail before IP allocation. Labels apply to VM metadata
before creation, not to the VMI, disks or PipelineRun. Empty values are valid;
trailing semicolons are not. Existing VMs are never modified by a request update.

Use **trusted platform operators only**. The central SA's target RoleBindings
set the allowed destinations. Requests do not impersonate the human operator;
any allowed requester can choose any target permitted to that SA. Hidden fields
are not an authorization boundary. Arbitrary Pod/TaskRun rights can expose Secrets.

## Validation and operations

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI requires oc for actual native rendering, local labels and Kustomize. Tests
cover target routing/denial, local NAD checks, preserved network profiles,
request-form upgrades, disk sizes, guest key handling and mocked IPAM calls.
No live API allocation or client-cluster access is performed by these tests.

**Pipeline success means VM resources were created, not that guests are ready.**
Verify CDI, VM Ready, cloud-init, unique identities, DNS, SSH and filesystem
capacity. No automatic retries, cleanup or rollback: preserve logs and reconcile
existing records/disks after a failure. Keep Infoblox placeholders/profile gates
unchanged until the network team supplies and approves the real settings.
