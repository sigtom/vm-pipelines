# VM Pipelines

**Tekton calls Infoblox directly for IPAM-only allocation. No AAP, DNS record
creation, DHCP reservation, or MAC registration is used.**

Operators supply name, size, count, SSH key, labels and Target VM namespace.
A per-network catalog entry fixes the NAD and network profile. Requests run in
vm-provisioning; VMs and disks go to the approved target namespace.

```text
Catalog request -> target/network/storage preflight
  -> GET host by short name and network view
  -> POST IPAM-only host using next available IP from the configured subnet
  -> GET created record, verify identity/address and DNS/DHCP disabled
  -> native VM Template + labels -> Kubernetes API -> CDI clone -> cloud-init
```

**Upgrade required: helper vm-ipam:0.4.** Version 0.3 used DNS-enabled host records
and range-based allocation; 0.2 called AAP. Rebuild the helper and update the
Task digest and network profiles together. The Task rejects older helpers before
allocation. Follow **[the IPAM-only upgrade](docs/ipam-only-upgrade.md)**.
No existing VM, disk or Infoblox record is changed by a repository update.

## Allocation contract

The API payload matches the successful IPAM-only test (documentation values):

```json
{
  "name": "vm-01",
  "network_view": "default",
  "configure_for_dns": false,
  "ipv4addrs": [{
    "ipv4addr": "func:nextavailableip:192.0.2.0/25,default",
    "configure_for_dhcp": false
  }]
}
```

Use the actual approved network/view, not these example values. No DNS view,
DNS domain, MAC or explicit free-IP list is sent. Readback accepts encoded host
references such as /%20 without decoding/re-encoding them. It checks the short
name, network view, DNS/DHCP flags, and one usable IP in the configured subnet.
The receipt remains `name|ip/prefix|mac|gateway|dns1,dns2|domain`.

**INFOBLOX_ALLOCATION_MODE must explicitly be subnet.** This selects from the
whole subnet, not the obsolete STATIC_START/END range. Infoblox administrators
must approve that scope and reserve/exclude gateway, infrastructure, DHCP-owned
and otherwise unavailable addresses in IPAM. No local scan, range creation or
fallback broadening occurs. An empty /range result is not evidence of no DHCP.
DNS/DHCP false applies to the new host, not the whole subnet.

Gateway, guest resolver IPs, search domain and prefix remain administrator-supplied.
A MAC is generated once for the VM and logged; it is not stored in Infoblox.
Names must be unique in the selected network view across target namespaces.
DNS registration outside Infoblox remains a separate client workflow.

## Scope and sizes

| Size | vCPU | Memory | Root disk |
| --- | ---: | ---: | ---: |
| small | 2 | 4 GiB | 110Gi |
| medium | 4 | 8 GiB | 500Gi |
| large | 8 | 16 GiB | 1Ti |

Binary quantities accommodate a 104Gi source. Guest filesystem/LVM growth is a
separate policy; a larger PVC does not prove a larger root filesystem.
The approved golden image uses BIOS/VirtIO and an explicitly configured machine
type. Cloud-init adds the supplied key to an existing non-root account without
changing password, groups, sudo or sshd policy. See [golden-image.md](docs/golden-image.md).

The six-field forms, network workspace snapshots, 1-5 VM batches, labels and
approved-target routing remain. Individual VMs are not stored in Git. There is
no VM YAML generator, runtime Ansible, SDK or runtime pip installation.

## Setup

For fresh installation use [runbook.md](docs/runbook.md). For network entries see
[network-catalog.md](docs/network-catalog.md); for each new destination follow
[target-namespaces.md](docs/target-namespaces.md).

| Resource | Location / purpose |
| --- | --- |
| Pipeline, Task, rhel-standard Template, ServiceAccount | Central vm-provisioning namespace. |
| vm-provision-config | Shared image/storage/account/API configuration. |
| vm-network-<NAD> | Central per-network profile; initially disabled. |
| vm-ipam-credentials | Central Secret: INFOBLOX_USERNAME and INFOBLOX_PASSWORD. |
| vm-ipam-ca | Optional approved CA bundle, key ca-bundle.crt. TLS is always verified. |
| Target namespace | Its NADs, poc-small/medium/large and scoped central-SA RoleBinding. |
| tekton/ipam-check.yaml | Admin-only authoring Template for a GET-only connectivity TaskRun. |

Keep client settings in ignored site.yaml/site-networks, never in public Git.
Runtime connection/credential/CA changes after the 0.4 build do not require
another build. Registry trust and API TLS trust are separate.

## Operator path

Select project vm-provisioning -> Software Catalog -> Provision VM on the desired
network -> Instantiate Template. Keep the top Namespace on vm-provisioning;
use Target VM namespace for the VM destination. The intended form lists a
PipelineRun, not a VirtualMachine, as the resource it will create.

Size is a text field: small, medium or large. Labels are optional, e.g.
foo=bar;boo=far. Invalid/duplicate/Template-owned labels fail before allocation.
Labels are applied to VM metadata, not VMI/disks. Follow runs centrally, VMs in
the target. The shared pipeline does not impersonate the requesting human.

**Trusted platform operators only:** target RoleBindings determine the central
SA's allowed destinations. Hidden fields are not an authorization boundary;
arbitrary Pod/TaskRun or Template edits can expose credentials. No cluster-admin
grant is needed for the provisioning SA. Check non-admin use separately.

## Validation and recovery

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

The helper uses only the standard library and Python 3.9-compatible syntax;
the cluster image uses UBI Python 3.12. CI uses real oc for native rendering and
mocks external APIs. It does not prove the client's API TLS, pods, CSI clone or
guest boot. Use the GET-only cluster check, then one approved small VM.

**No automatic retries or deletions.** A failed/timed-out POST may have created a
host. Capture ALLOCATING/RESERVED log lines, its reference and receipt; reconcile
before another request. Partial batches may leave allocations without VMs.
emptyDir is temporary, not a durable recovery database. Pipeline success means
VM creation, not guest readiness or completed AD/Salt/agent hardening.
