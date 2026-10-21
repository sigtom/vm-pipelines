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

**Helper release `vm-ipam:0.3` restores direct Infoblox.** The earlier `0.2`
image contained the AAP caller and must not be reused. Build `0.3` in the target
cluster and pin its new digest in the Task. See the
[runbook](docs/runbook.md#switching-from-the-02-image) for the safe update path.
This is a new commit, not a force-push or history rewrite.

## Scope

| Size | vCPU | Memory | Root disk |
| --- | ---: | ---: | ---: |
| small | 2 | 4 GiB | 40 GiB |
| medium | 4 | 8 GiB | 80 GiB |
| large | 8 | 16 GiB | 160 GiB |

- Native OpenShift VM Template and VirtualMachineInstancetype resources.
- One approved RHEL DataSource, one bridged NIC and one approved network profile.
- Static IPv4 allocation and DNS host-record creation in Infoblox NIOS.
- Requests for 1-5 VMs; sequential allocation and creation, no automatic retries.
- SSH ed25519 public-key access; no new passwordless sudo rule by default.
- Optional VM labels, unchanged from the direct-Infoblox baseline.

**Reference implementation, not a turnkey production service.** Target OpenShift
Virtualization 4.21+ and Pipelines serving `tekton.dev/v1`. The client must verify
its installed versions, network, image, storage, permissions and Infoblox API.
Offline tests do not establish successful integration with a client appliance.

The helper is restored from the direct-WAPI implementation. It does not discover
gateway/DNS/pool settings automatically; administrators supply them in the site
ConfigMap. It creates DNS host records, not DHCP fixed-address reservations.
The generated MAC configures the guest NIC; it is not registered for DHCP in
Infoblox by this implementation. Multi-NIC and automatic guest hardening are
outside the current scope.

## Client configuration

Start with **[docs/runbook.md](docs/runbook.md)**. Copy `config/site.example.yaml`
to ignored `site.yaml` and fill the `REPLACE_ME` values. Never commit real client
settings or credentials to this public repository.

| File or resource | Purpose |
| --- | --- |
| `config/site.example.yaml` | OpenShift resources, Infoblox endpoint/version/views, network/pool, gateway, DNS and domain. |
| `build/buildconfig.yaml` | Approved UBI Python base digest and in-cluster helper build, output `vm-ipam:0.3`. |
| `tekton/task.yaml` | Administrator-selected CLI and newly built helper image digests. |
| `tekton/serviceaccount.yaml` | Scoped VM/dependency permissions and source-image access. |
| Secret `vm-ipam-credentials` | `INFOBLOX_USERNAME` and `INFOBLOX_PASSWORD`; supplied outside Git. |
| Optional ConfigMap `vm-ipam-ca` | Approved PEM trust bundle under `ca-bundle.crt`. |

The default target/execution namespace is `vm-provisioning`. The NAD and IPAM
subnet must match. No private prebuilt image, runtime Git checkout, Ansible,
SDK, or runtime pip install is required. The Python helper uses the standard
library. VM sizing, native Template processing, labels, cloud-init and day-2
console operations remain unchanged.

## Operator path

Install the request Template separately, then use:

**Project vm-provisioning -> Ecosystem -> Software Catalog -> Request RHEL VM
-> Instantiate Template -> name / size / count / SSH key / optional labels.**

The request fixes the Pipeline, dedicated `vm-provision` ServiceAccount and
`scratch: emptyDir`. Follow progress in Pipelines -> PipelineRuns; the console
may redirect to Topology. Verify client-console and non-admin access separately.
Use trusted platform operators only. Hidden fields are not an authorization
boundary, and arbitrary Pod/TaskRun rights can expose namespace credentials.

### Additional VM labels

Use `foo=bar;boo=far`, or leave the field empty. The Pipeline parameter is
`vm-labels` and defaults to empty. Spaces around entries are trimmed. Invalid
Kubernetes labels, duplicate keys, empty entries and attempts to replace
Template-managed labels fail before IP allocation. Empty values (`key=`) are
valid; trailing semicolons are not.

Labels are placed on every VM's `metadata.labels` before creation, not on the
VMI, disks, or PipelineRun. Native `oc label --local --overwrite=false` performs
the edit. Existing VMs are not changed. No new label helper or RBAC is needed.

## Validation and operations

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only, not a runtime dependency.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI requires the 4.21 oc CLI for real native rendering, label application and
Kustomize tests. IPAM requests are mocked; tests never allocate live addresses.
No automatic rollback, retries or deletes are performed. A failed request may
have left IPAM allocations; retain logs and reconcile before another request.

**Pipeline success means VM resources were created, not that guests are ready.**
Verify CDI, boot, cloud-init, DNS and SSH separately. Subscriptions, AD, Salt and
security-agent enrollment remain customer-owned integrations. Existing VMware
golden images can be reused after preparation; see [golden-image.md](docs/golden-image.md).
