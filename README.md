# VM Pipelines

Native OpenShift VM templates and Tekton provisioning with **AAP-owned IPAM/DNS**.
Tekton calls one AAP job per VM; only AAP talks to Infoblox. No direct Infoblox
credentials or WAPI calls remain in the pipeline.

```text
Catalog form: name / size / count / SSH key / optional labels
  -> validate request and OpenShift dependencies
  -> AAP job: reserve IP, associate MAC, register and verify DNS
  -> successful job artifacts.vm_network
  -> oc process + oc label --local + oc create
  -> CDI disk clone -> cloud-init -> VM
```

**Start here:** [Client installation runbook](docs/runbook.md).
**Give the AAP team:** [Input/output contract and set_stats task](docs/aap-contract.md).

This is a reference implementation for OpenShift Virtualization 4.21+ and
OpenShift Pipelines serving `tekton.dev/v1`, not a turnkey production service.
The AAP integration is tested offline, not against your controller, playbook,
Infoblox appliance, or virtualization cluster. Install the resources after
review; do not submit requests until AAP and the client configuration are ready.

## What is unchanged

| Size | vCPU | Memory | Root disk |
| --- | ---: | ---: | ---: |
| small | 2 | 4 GiB | 40 GiB |
| medium | 4 | 8 GiB | 80 GiB |
| large | 8 | 16 GiB | 160 GiB |

Native instance types, one approved boot source, one bridged NIC, static IPv4,
requests for 1-5 VMs, SSH ed25519 access, and optional VM labels remain.
Individual VMs are not committed to Git or reconciled by Argo CD.
Guest Salt/AD/subscription/security-agent configuration is still customer-owned.
No custom VM generator, callback service, Ansible installation in Tekton, or
extra runtime Python packages are required.

## Client settings

Copy `config/site.example.yaml` to ignored `site.yaml`. Fill all `REPLACE_ME`
values. Keep OpenShift namespace, image, storage and network settings there.
The network attachment and AAP network profile must identify the same network.

| Setting | Required value |
| --- | --- |
| `AAP_API_BASE_URL` | HTTPS controller base ending `/api/controller/v2` or `/api/v2`. |
| `AAP_JOB_TEMPLATE_ID` | Numeric ID of an unattended regular job template (no slicing). |
| `NETWORK_PROFILE` | Approved identifier understood by the AAP playbook. |
| `AAP_JOB_TIMEOUT_SECONDS` | Per-VM queue/run/read-back deadline; default 600. |
| `AAP_POLL_INTERVAL_SECONDS` | Status polling interval; default 5. |
| Secret `vm-aap-credentials` | Key `AAP_TOKEN`, supplied outside Git. |
| Optional ConfigMap `vm-aap-ca` | Key `ca-bundle.crt` for the approved AAP CA bundle. |

Gateway, DNS, domain, address pool and Infoblox policy belong to AAP. AAP returns
the assigned **`ip_cidr`**, such as `192.0.2.40/24`; Tekton uses that exact value.
`artifacts.vm_network` is this project's agreed contract, not an automatic AAP
result. The playbook team must implement and publish it using `set_stats`.

Build the helper inside the client cluster using `Containerfile` and
`build/buildconfig.yaml`, then set tested image **digests** in the Task's
`cli-image` and `ipam-image` defaults. The existing `vm-ipam` image name/script
path are retained to avoid needless renaming; tag `0.2` contains the AAP caller.

## Request form and labels

After separate installation of `tekton/request-template.yaml`:

**Project vm-provisioning -> Ecosystem -> Software Catalog -> Request RHEL VM
-> Instantiate Template -> name / size / count / SSH key / optional labels.**

Labels use `foo=bar;boo=far`. Empty input is valid. Spaces around entries are
trimmed; invalid/duplicate keys and Template-managed labels are rejected before
AAP launches. Labels apply to every VM's `metadata.labels` before creation,
not merely to the PipelineRun, VMI or disk. Existing requests without labels work.

The request fixes the Pipeline, dedicated `vm-provision` ServiceAccount and
`scratch: emptyDir`. Track progress in Pipelines -> PipelineRuns; the console may
redirect to Topology. Use trusted platform operators only. Hidden fields are not
an authorization boundary; arbitrary TaskRun/Pod creation can expose Secrets.
Validate non-admin and client-console behavior separately.

## Tests and limits

```sh
python3 -m pip install PyYAML==6.0.3  # Test-only dependency
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI installs the 4.21 `oc` client and requires native rendering tests. AAP tests
mock the API; native tests process real Templates and labels without a cluster.
No test allocates a live address. `check-aap` performs GET-only readiness checks;
see the runbook for an in-cluster check before any provisioning request.

The helper records request/MAC/job IDs before and after launching. It never
relaunches, cancels jobs, or deletes allocations automatically. A timeout does
not imply AAP stopped. Preserve logs and reconcile failures before retrying.
The request ID is correlation, not built-in idempotency; the playbook must
handle duplicate/conflicting records safely.

**Upgrade from the direct Infoblox version:** rebuild the helper, replace the
site settings and Secret reference, and apply the Task, Pipeline and request
Template together. Do not mix the new Task with the old image. See the runbook.
VM creation success is not guest readiness: verify CDI, boot, cloud-init, DNS,
SSH and any customer hardening separately.
