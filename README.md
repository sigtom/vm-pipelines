# VM Pipelines

Native OpenShift VM templates and a small Tekton workflow for **Infoblox NIOS**.
The operator requests a name, size, count, and SSH key. Tekton reserves addresses,
processes the native VM Template, and creates VMs through the Kubernetes API.
Individual VMs are **not** written to Git or reconciled by Argo CD.

```text
Software Catalog request (four fields)
  -> PipelineRun with the dedicated ServiceAccount
  -> validate -> preflight -> Infoblox allocation -> oc process + oc create
  -> CDI disk clone -> guest cloud-init
```

**Reference implementation, not a turnkey production service.** Target:
OpenShift Virtualization 4.21+ and OpenShift Pipelines serving `tekton.dev/v1`.
The client must validate its own versions, permissions, storage, networking,
image pulls, catalog behavior, and Infoblox appliance before use. Offline tests
and API-contract checks do not establish successful client integration or a
vendor support commitment for this composition.

## Scope

| Size | vCPU | Memory | Root disk |
| --- | ---: | ---: | ---: |
| small | 2 | 4 GiB | 40 GiB |
| medium | 4 | 8 GiB | 80 GiB |
| large | 8 | 16 GiB | 160 GiB |

- Native `VirtualMachineInstancetype` sizing; one approved RHEL CDI DataSource.
- One bridged NIC and one admin-approved network per deployment profile.
- Static IPv4 allocated through an Infoblox host record; DNS zones must exist.
- Count 1-5, with sequential allocation and creation. No automatic retries/deletion.
- SSH ed25519 public-key access. No new passwordless sudo rule by default.
- A separately installed native catalog request Template with the four inputs.

No custom VM YAML generator, runtime Git checkout, runtime package installation,
custom portal, automatic network discovery, multi-NIC workflow, or guest Salt/AD
implementation. Gateway, DNS, domain, and approved pool bounds are explicit
admin configuration. This is **not** a DHCP fixed-address reservation workflow.

## Configure first

Start with **[docs/runbook.md](docs/runbook.md)**. Configuration is deliberately
incomplete; `REPLACE_ME` values are not deployment defaults.

| File | Administrator action |
| --- | --- |
| `config/site.example.yaml` | Copy to `site.yaml`; fill boot source, storage, network, WAPI and pool settings. |
| `build/buildconfig.yaml` | Set `BASE_IMAGE` to an approved UBI Python image digest or mirror. |
| `tekton/task.yaml` | Set `cli-image` and `ipam-image` defaults to tested image digests. |
| `tekton/serviceaccount.yaml` | Verify execution/VM namespace and source-image namespace permissions. |
| `tekton/pipelinerun.example.yaml` | Set an unused VM name and a real **public** key for the first approved test. |

Credentials belong in the external secret store/Kubernetes Secret
`vm-ipam-credentials`, not these files. No prebuilt private image is required;
build the small IPAM image in the target cluster. Namespace `vm-provisioning`
is a generic deployment default, not an existing-resource assumption.

## Operator path

After validation and separate installation of `tekton/request-template.yaml`:

**Project vm-provisioning -> Ecosystem -> Software Catalog -> Request RHEL VM
-> Instantiate Template -> name / size / count / SSH key -> Create.**

The request fixes the Pipeline, `vm-provision` ServiceAccount and emptyDir
workspace. Follow progress in **Pipelines -> PipelineRuns**; the console may
redirect to Topology. Plain **Start Pipeline** is not the documented entry point
for this workflow. Client console behavior still needs validation.

Use trusted platform operators only. Hidden form fields are **not** an
authorization boundary. Generic TaskRun/Pod creation rights can expose namespace
credentials; do not grant them to untrusted users merely to enable the form.

## Tests

```sh
python3 -m pip install PyYAML==6.0.3    # Test-only; never installed by a VM request.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

Install the client-matched `oc` CLI for real Template processing and Kustomize
tests. Without `oc`, three native tests skip; CI requires them. Tests use mock
API calls and documentation-range addresses, never a real cluster or appliance.

**Pipeline success means VM resources were created, not that the guests are
ready.** Confirm CDI, VM readiness, cloud-init, DNS and SSH separately.

For an existing VMware golden image, see [docs/golden-image.md](docs/golden-image.md).
Guest hardening, subscription registration, security agents and AD enrollment
remain customer-owned integrations. Keep the native templates and API contracts
reusable when the long-term orchestration platform is introduced.
