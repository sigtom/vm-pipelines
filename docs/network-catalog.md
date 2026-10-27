# One catalog entry per network, one shared pipeline

Each request Template fixes a network profile. The operator supplies name, size,
count, SSH key, optional labels and **Target VM namespace**. Keep the native
form's top Namespace selector on the central provisioning namespace.
No NAD dropdown, duplicate Pipeline/Task, or helper-image rebuild is needed.

For an existing five-field installation, first follow
[target-namespaces.md](target-namespaces.md). Its offline form upgrade preserves
all current display names and network workspace bindings without changing any
ConfigMap. Updating this authoring file alone does not change installed forms.

## Shared and per-network settings

The central vm-provision-config supplies the default VM namespace, golden source,
storage, machine type, guest account, Infoblox HTTPS endpoint and WAPI version.
The request can override only the VM destination. Credentials remain in central
vm-ipam-credentials; CA trust stays in vm-ipam-ca.

Each network ConfigMap defines the NAD, subnet/views/pool, gateway, DNS, domain,
DHCP declaration and PROFILE_ENABLED. The selected profile must be complete:
there is no fallback from a missing profile field to another network's values.
Preflight snapshots the selected settings; IP allocation and VM creation use
the same snapshot. Unknown keys cannot override namespaces, images or credentials.
Wait for active requests to finish before editing any profile.

The chosen NAD name is resolved in the **target VM namespace**. It must refer to
the same approved VLAN/IPAM network there. Before use, enroll that target with
its local NADs, three instance types and the scoped central-SA RoleBinding.
See [target setup](target-namespaces.md#enroll-each-approved-target-once).

The legacy single-network request can omit the network workspace and use site
configuration. That mode remains compatible. Its VM destination can now be
selected independently too; an omitted target parameter uses VM_NAMESPACE.

## Render a new, disabled profile and request entry

tekton/network-catalog.yaml is an **administrator authoring Template**.
Process it locally; do not install the wrapper. Its output contains one ConfigMap
and one request Template, not a live PipelineRun. The parameter NAMESPACE selects
the central namespace. TARGET_NAMESPACE belongs to the inner operator form.

```sh
mkdir -p site-networks
# Use a new output filename; do not overwrite approved local configuration.
oc process --local -f tekton/network-catalog.yaml \
  -p NETWORK_ATTACHMENT=example-nad \
  -p REQUEST_TEMPLATE_NAME=vm-provision-example-nad \
  -p 'DISPLAY_NAME=Provision VM on Example Network' \
  -o yaml > site-networks/example-nad.yaml
```

The generated resource names are vm-network-example-nad and
vm-provision-example-nad. The site-networks directory is ignored by Git. Keep
real network names/settings local. New profiles start with PROFILE_ENABLED false.
Inspect existing owners before installation; do not overwrite existing profiles
with generated placeholders.

```sh
oc create --dry-run=server -f site-networks/example-nad.yaml &&
oc create -f site-networks/example-nad.yaml
```

This creates definitions only. Do not submit a request while Infoblox is pending.
Disabled profiles still have a visible Create button; the Task refuses allocation.

## Configure and enable a profile later

In the existing profile, supply the real subnet, DNS/network views, allocation
bounds, gateway, DNS and domain. They must all describe the chosen NAD network.
Keep DHCP_START and DHCP_END as keys. For no DHCP, set both values to empty
strings and DHCP_DISABLED to true after confirming with the network owner.
For DHCP present, set its real bounds and DHCP_DISABLED false.

Use the existing approved credential/CA process. Only after configuration,
permissions, TLS and routing checks should an administrator set PROFILE_ENABLED
true for the selected profile. Do not enable all profiles merely to test one.
Changing runtime settings/credentials/CA does not need another helper build.

## Verify the forms

```sh
oc -n vm-provisioning get templates \
  -o custom-columns='NAME:.metadata.name,TITLE:.metadata.annotations.openshift\.io/display-name'
```

Choose vm-provisioning in the **catalog Project** selector to see these local
entries. The native Instantiate Template page will show the same six inputs and
list PipelineRun as the created resource. A golden-image VM Template that lists
VirtualMachine is a different entry, not the pipeline request.

The fixed profile is a convenience, not an authorization boundary. Restrict
central task/pod/template edits to trusted operators. Cross-namespace permissions
come from explicit target RoleBindings, not the visible form fields.

Reference: https://tekton.dev/docs/pipelines/workspaces/
