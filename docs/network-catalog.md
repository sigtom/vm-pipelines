# One catalog entry per network, one shared pipeline

Each request Template fixes a network profile. Operators enter name, size,
count, SSH key, labels and Target VM namespace. Keep the native form's top
Namespace on vm-provisioning. There is no NAD dropdown or duplicate pipeline.

For existing deployments, first complete the [0.4 upgrade](ipam-only-upgrade.md).
It requires rebuilding the helper once. The forms and their network workspace
bindings do not change for this IPAM update. For an older five-field installation,
[target-namespaces.md](target-namespaces.md) explains the additive form upgrade.
Do not regenerate profiles to add a form field.

## Shared and per-network settings

vm-provision-config holds source/storage/guest defaults, API origin/version and
optional runtime proxy settings. Credentials stay in vm-ipam-credentials; CA
trust stays in vm-ipam-ca. The target field changes only the VM destination.

Every selected profile must contain this complete set of string values:

```yaml
PROFILE_ENABLED: "false"
NETWORK_ATTACHMENT: REPLACE_ME_NAD
INFOBLOX_ALLOCATION_MODE: subnet
INFOBLOX_NETWORK: REPLACE_ME_CIDR
INFOBLOX_NETWORK_VIEW: REPLACE_ME_NETWORK_VIEW
GATEWAY: REPLACE_ME_GATEWAY
DNS_SERVERS: REPLACE_ME_COMMA_SEPARATED_RESOLVERS
DOMAIN: REPLACE_ME_GUEST_SEARCH_DOMAIN
```

Mode subnet explicitly approves allocation from the whole subnet. Only enable
the profile once Infoblox owns all required reservations/exclusions and the
network owner approves this use. This does not enable DHCP or DNS on the host.
No INFOBLOX_DNS_VIEW, STATIC_START/END or DHCP range declarations are needed.
Remove these obsolete keys from existing profile data during the upgrade;
0.4 ignores them after explicit subnet-mode selection, rather than treating
them as allocation bounds. Never assume a range-free subnet is DHCP-free.

Preflight snapshots the selected network once. Allocation and VM creation use
that snapshot, not subsequent ConfigMap edits. Missing/disabled/invalid profiles
fail closed, without falling back to a different network. Unknown keys cannot
override namespaces, image defaults or credentials. Do not edit active profiles.

The selected NAD must exist in the target namespace and connect to the same
VLAN/subnet as the profile. See [target setup](target-namespaces.md).
A legacy request without the network workspace reads its network from site
configuration; it also requires explicit INFOBLOX_ALLOCATION_MODE=subnet.

## Render a new, disabled profile and request entry

Process the administrator authoring Template locally; do not install its wrapper.
It outputs only a ConfigMap and an inner request Template, not a live PipelineRun.

```sh
mkdir -p site-networks
# Use a new filename. Do not overwrite previously approved settings.
oc process --local -f tekton/network-catalog.yaml \
  -p NETWORK_ATTACHMENT=example-nad \
  -p REQUEST_TEMPLATE_NAME=vm-provision-example-nad \
  -p 'DISPLAY_NAME=Provision VM on Example Network' \
  -o yaml > site-networks/example-nad.yaml
oc create --dry-run=server -f site-networks/example-nad.yaml &&
oc create -f site-networks/example-nad.yaml
```

The names are vm-network-example-nad and vm-provision-example-nad. Real NADs,
subnets and addresses stay in the gitignored site-networks directory. Inspect
owners before creation. The wrapper's NAMESPACE means central execution;
TARGET_NAMESPACE belongs to the inner form.

## Configure and enable an existing profile

Edit only the intended network ConfigMap. Keep PROFILE_ENABLED false while
filling subnet/view/gateway/resolvers/domain and approving subnet allocation.
Use [the read-only cluster check](ipam-only-upgrade.md) to verify helper version,
TLS, credentials and host-search access before enabling that one profile.
Do not enable other networks merely to test one. A successful read-only check
does not prove allocation permissions or guest connectivity.

Old generated site-networks files may still contain earlier request forms and
network keys. Update the local files too, but do not blindly apply them: that
can revert the target-namespace field or approved live profile values.

## Verify forms

```sh
oc -n vm-provisioning get templates \
  -o custom-columns='NAME:.metadata.name,TITLE:.metadata.annotations.openshift\.io/display-name'
```

Choose vm-provisioning in the catalog Project selector. The correct entry lists
PipelineRun as the resource and shows six request inputs. A golden-image entry
that lists VirtualMachine is not this provisioning workflow. Disabled profiles
remain visible; Create is not hidden. Restrict request/Pod/Task edits to trusted
operators; the network title is not an authorization boundary.

Reference: https://tekton.dev/docs/pipelines/workspaces/
