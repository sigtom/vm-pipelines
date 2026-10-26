# VM Pipelines

**Tekton calls Infoblox NIOS directly. No AAP service, token, job template, or
network-allocation playbook is required.**

The operator supplies a VM name, size, count, SSH public key, and optional
semicolon-separated labels. Tekton reserves addresses in Infoblox, processes
the native OpenShift VM Template, and creates VMs through the Kubernetes API.
Individual VMs are not written to Git or reconciled by Argo CD.

```text
Software Catalog request
  -> validate -> OpenShift preflight -> direct Infoblox WAPI allocation
  -> oc process + oc label --local + oc create
  -> CDI disk clone -> guest cloud-init
```

**The current helper remains `vm-ipam:0.3` (direct Infoblox).** The earlier `0.2`
image contained the AAP caller and must not be reused. The golden-image/disk-size
update changes manifests and tests only: an already-built `0.3` helper does
**not** need rebuilding. No deployed resources or existing VMs change on Git push.

## Scope and sizes

| Size | vCPU | Memory | Root disk request |
| --- | ---: | ---: | ---: |
| small | 2 | 4 GiB | **110Gi** |
| medium | 4 | 8 GiB | **500Gi** |
| large | 8 | 16 GiB | **1Ti** (1024 GiB) |

Disk sizes use binary Kubernetes quantities, not decimal GB/TB. In particular,
`110G` is smaller than a 104Gi source; `110Gi` accommodates it. Preflight checks
the actual source PVC capacity and rejects a target that is too small. These
are virtual disk/PVC sizes, not a promise that the guest's LVM/filesystems
will grow automatically. Verify guest partition/LVM layout before resizing it.

- Native OpenShift VM Template and VirtualMachineInstancetype resources.
- One approved, generalized RHEL golden DataSource; BIOS boot and VirtIO disks.
- Admin-selected QEMU `MACHINE_TYPE`, matching the tested golden-image VM.
- One bridged NIC with static IPv4 and Infoblox DNS host-record creation.
- Requests for 1-5 VMs; sequential allocation and creation, no automatic retries.
- SSH ed25519 key added to an **existing non-root account** in the golden image.
- Optional VM labels; no change to credentials, labels, or provisioning identity.

**Guest account policy stays with the image.** The key-only cloud-init uses
`users: []` and a short first-boot command to append the key to the existing
account's real home directory. It does not create users, modify password/lock
state, add groups/sudo, or rewrite sshd policy. Existing keys are preserved.
The account must already exist and be allowed by the image's SSH policy.
`CLOUD_USER_SUDO`/`SUDO_POLICY` are no longer used; remove them from site files.

This replaces the older generic cloud-user creation block. Native user creation
options can apply password-lock defaults to existing accounts on some cloud-init
versions; merely omitting `lock_passwd` is not enough to guarantee preservation.
See [golden-image.md](docs/golden-image.md) for the guest acceptance checks.

**Reference implementation, not a turnkey production service.** Target OpenShift
Virtualization 4.21+ and Pipelines serving `tekton.dev/v1`. Client firmware,
networking, storage, guest boot, permissions and Infoblox behavior need live
validation. Tests render manifests and mock APIs, not the customer's cluster.

The helper does not discover gateway/DNS/pool settings automatically. They are
admin-owned site configuration. It creates host records, not DHCP reservations;
the generated MAC configures the guest NIC, not an Infoblox DHCP reservation.
Multi-NIC and automatic guest hardening remain outside this scope.

## Client configuration

Start with **[docs/runbook.md](docs/runbook.md)**. Copy `config/site.example.yaml`
to ignored `site.yaml` and fill the placeholders. Never commit client endpoints,
subnets, image digests, credentials, or exported live templates to this public repo.

| File or resource | Purpose |
| --- | --- |
| `config/site.example.yaml` | Golden DataSource, storage/NAD, machine type, existing guest user, Infoblox settings. |
| `build/buildconfig.yaml` | Approved UBI Python base digest and in-cluster helper build, output `vm-ipam:0.3`. |
| `tekton/task.yaml` | Administrator-selected CLI and direct helper image digests; root disk size policy. |
| `tekton/serviceaccount.yaml` | Scoped VM/dependency permissions and source-image access. |
| Secret `vm-ipam-credentials` | `INFOBLOX_USERNAME` and `INFOBLOX_PASSWORD`; supplied outside Git. |
| Optional ConfigMap `vm-ipam-ca` | Approved PEM trust bundle under `ca-bundle.crt`. |

The default target/execution namespace is `vm-provisioning`. The NAD and IPAM
subnet must match. There is no private prebuilt image, runtime Git checkout,
Ansible, SDK, or runtime pip installation. `rhel-standard` remains the pipeline's
VM Template: selecting a DataSource does not import a different catalog Template.
Do not overwrite the original tested golden-image Template.

## Operator path

**Project vm-provisioning -> Ecosystem -> Software Catalog -> Request RHEL VM
-> Instantiate Template -> name / size / count / SSH key / optional labels.**

The request fixes the Pipeline, `vm-provision` ServiceAccount and `scratch:
emptyDir`. Follow progress under Pipelines -> PipelineRuns. Verify client-console
and non-admin access separately. Hidden fields are not an authorization boundary;
arbitrary Pod/TaskRun creation rights can expose namespace credentials.

Labels use `foo=bar;boo=far`, or leave the field empty. Spaces around entries are
trimmed. Invalid labels, duplicate keys and attempts to replace Template-owned
labels fail before IP allocation. Empty values (`key=`) are valid; trailing
semicolons are not. Native `oc label --local --overwrite=false` adds labels to
every VM's metadata before creation, not to the VMI, disks or PipelineRun.

## Validation and operations

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI requires 4.21 oc for native rendering, labels and Kustomize. Tests cover
104Gi-source fit, each size's upper boundary, BIOS/VirtIO settings, key-only
bootstrap in a temporary home, label handling and mocked WAPI behavior.
No automatic retries, rollback or deletion: preserve failure logs and reconcile
any existing IPAM records/VM disks before another request.

**Pipeline success means VM resources were created, not that guests are ready.**
Verify CDI, boot, cloud-init, distinct machine IDs/host keys, DNS, guest account
policy, SSH and guest filesystem capacity. Subscriptions, AD, Salt and security
agents remain customer-owned. See [golden-image.md](docs/golden-image.md).
