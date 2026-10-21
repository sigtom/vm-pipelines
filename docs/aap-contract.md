# AAP network-allocation contract, version 1

Give this document to the AAP playbook team. This repository implements the
caller, not the customer's Infoblox playbook. AAP owns credentials, pools, DNS
policy, MAC association, and duplicate/recovery behavior.

## Job template

Use a **regular Job Template**, job type Run, one slice, with all inventory,
credentials, project and execution-environment settings configured for unattended
execution. Workflow jobs and approvals are outside this MVP.

Allow these four `extra_vars` through **Prompt on launch for Variables**, or an
enabled survey accepting all four. No other mandatory input may be required.
Treat the values as untrusted: validate the name/MAC and allow-list the profile
in the playbook even though the caller also checks them. No credentials travel
in these variables. Limit the token's identity to reading and executing the
approved template and reading its jobs/results. The token needs write scope to
launch, with restricted RBAC; this is not an AAP administrator credential.

## Request

`POST {AAP_API_BASE_URL}/job_templates/{AAP_JOB_TEMPLATE_ID}/launch/`

```json
{
  "extra_vars": {
    "request_id": "<pipeline-run-uid>/app01/lan",
    "vm_name": "app01",
    "mac_address": "02:11:22:33:44:55",
    "network_profile": "application-network"
  }
}
```

The caller generates the locally administered MAC once and records it before
launch. A batch launches jobs sequentially, one per VM. The profile maps to an
approved network, pool, DNS view/zone and network view; on OpenShift it must
match the selected NetworkAttachmentDefinition. The short name is **not** an
unqualified DNS record: construct the FQDN using the approved domain.

The playbook must check conflicts, reserve the address, associate the supplied
MAC as agreed with the Infoblox team, create/verify DNS, and read back the actual
record. An echoed MAC alone does not prove the Infoblox record was updated.
This VM workflow uses **static cloud-init addressing**. Do not silently change
it to DHCP; any DHCP-reservation requirement is a separate agreed policy.

## Result

Publish a dictionary named **`vm_network`** with `ansible.builtin.set_stats`.
The completed job detail response must contain it at **`artifacts.vm_network`**:

```json
{
  "artifacts": {
    "vm_network": {
      "schema_version": 1,
      "request_id": "<pipeline-run-uid>/app01/lan",
      "vm_name": "app01",
      "mac_address": "02:11:22:33:44:55",
      "network_profile": "application-network",
      "fqdn": "app01.example.com",
      "ip_cidr": "192.0.2.40/24",
      "gateway": "192.0.2.1",
      "dns_servers": ["192.0.2.2", "192.0.2.3"],
      "domain": "example.com"
    }
  }
}
```

**`ip_cidr` is the address the VM will use**, including the prefix length.
The documentation addresses above must never be returned as a live assignment.
Use numeric `schema_version: 1`, a list of IPv4 DNS strings, and strings for the
other fields. Return the request ID, name, MAC and profile unchanged. The helper
requires the FQDN to equal `vm_name + '.' + domain`; it rejects missing results,
mismatches, invalid addressing/gateway/DNS, and duplicate IPs within a batch.
IPv4 with ordinary /1-/30 subnets is supported, not /31, /32 or IPv6.

Example final task, **after** your allocation and read-back verification:

```yaml
- name: Publish verified VM networking
  ansible.builtin.set_stats:
    per_host: false
    aggregate: false
    data:
      vm_network:
        schema_version: 1
        request_id: "{{ request_id }}"
        vm_name: "{{ vm_name }}"
        mac_address: "{{ mac_address }}"
        network_profile: "{{ network_profile }}"
        fqdn: "{{ verified_fqdn }}"
        ip_cidr: "{{ verified_ip_cidr }}"
        gateway: "{{ verified_gateway }}"
        dns_servers: "{{ verified_dns_servers }}"
        domain: "{{ verified_domain }}"
```

`verified_*` are example playbook variable names. Map them to your actual checked
values. Gateway/DNS/domain may come from Infoblox effective settings or your
approved AAP profile; the caller does not assume the allocation response supplies
them automatically. Publish only non-secret data. The caller does not scrape
stdout or run another playbook to retrieve the result.

## Failure and recovery

AAP API launch is asynchronous. The helper records the numeric job ID and polls
`GET {AAP_API_BASE_URL}/jobs/{id}/`. Only `status: successful` plus a valid result
allows VM creation. Failed/error/canceled jobs, ignored launch variables, API
failures, unknown states or malformed results stop the Task. No launch retry or
automatic cancellation is performed. A locally timed-out job may still finish.

A request ID is **not** automatic AAP idempotency. The playbook must prevent
duplicate/conflicting allocations, including requests made through other tools.
For an explicitly approved recovery, return an existing assignment only when
name, MAC, profile and request identity match. Reject a conflicting record.
A new PipelineRun has a new UID: do not use a new run as blind retry/recovery.

If AAP succeeds but VM creation fails, retain the assignment and investigate.
Do not automatically delete DNS/IP records or disks. Coordinate recovery using
the caller's request ID, MAC, AAP job ID and actual Infoblox records.

## Acceptance with the AAP team

Verify exact endpoint/version and token roles, GET-only readiness, then one
approved job/VM. Test conflict rejection and failure output without deleting
existing records. Confirm that the emitted artifact is a dictionary, not a JSON
string, and that the recorded MAC/IP/DNS agree with the resulting VM.

References:
- Red Hat API authentication/launch: https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/html/automation_execution_api_overview/controller-api-auth-methods
- Launch variables: https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/html/using_automation_execution/controller-job-templates
- set_stats artifacts: https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/html/using_automation_execution/controller-workflows
- PipelineRun UID: https://tekton.dev/docs/pipelines/variables/
