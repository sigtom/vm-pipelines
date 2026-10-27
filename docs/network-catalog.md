# One catalog entry per network, one shared pipeline

Use this after the initial installation in [runbook.md](runbook.md).
This adds request Templates and non-secret network ConfigMaps, not more
Pipelines, Tasks, VM Templates, namespaces, images, or controllers.

Each operator chooses a catalog entry, then sees the same five fields: name,
size, count, SSH public key and optional VM labels. The entry fixes a read-only
`network` workspace to its own ConfigMap. There is no NAD dropdown or sixth
request parameter. The native page still says Instantiate Template / Create.

## What is shared and what belongs to each network

The existing `vm-provision-config` supplies VM namespace, golden DataSource,
storage, machine type, existing guest user, and the Infoblox HTTPS endpoint and
WAPI version. Credentials stay in `vm-ipam-credentials`; private CA trust stays
in `vm-ipam-ca`. The IPAM helper remains the already-built direct-Infoblox 0.3
image. **No rebuild or new runtime RBAC is needed.**

A network ConfigMap contains its NAD, Infoblox subnet/views/pool, gateway, DNS,
domain, DHCP declaration, and `PROFILE_ENABLED`. All of its network keys are
mandatory; the two DHCP range values can be empty only when DHCP is absent.
The shared ConfigMap's network values are overridden as one complete set.
There is no fallback from an incomplete selected profile to another network.

Preflight snapshots the selected settings once. IP allocation and VM creation
read that same snapshot, not a ConfigMap that may have changed between steps.
Treat profile updates as administrator changes and wait for active requests to
finish before editing them. Unknown profile keys cannot override images, guest
accounts, namespaces, credentials or other process settings.

The existing request Template still works without a network workspace and uses
the original site configuration. This preserves a current single-network entry
while more entries are added. To convert that entry too, generate a profile using
its existing Template name; its direct form URL will remain valid.

## 1. Update the shared definitions

Use the current main branch. Preserve your local Task image digests, renamed
request display name and ignored site.yaml. In an existing main checkout:

```sh
git pull --ff-only --autostash origin main
git status --short
```

Stop for any merge conflict. Do not reset/clean or replace your local Task with
an upstream file containing image placeholders. Do not reapply site.yaml or the
old request Template just to add other networks.

After checking both image defaults still contain the approved digests, validate
then update only the Task and Pipeline, with no provisioning requests running:

```sh
oc apply --dry-run=server -f tekton/task.yaml -f tekton/pipeline.yaml
oc apply -f tekton/task.yaml -f tekton/pipeline.yaml
```

Optional ConfigMap workspaces are a native Tekton feature:
https://tekton.dev/docs/pipelines/workspaces/
Do not substitute parameters into envFrom names; that is not used here.

## 2. Render a new entry and disabled network profile

`tekton/network-catalog.yaml` is an **administrator authoring Template**.
Process it locally; do not install the wrapper itself. Its output contains only
one ConfigMap and one request Template. The PipelineRun inside that request
Template remains a definition until an operator submits the form.

Use the actual NAD name, a unique Template name, and a clear display name:

```sh
mkdir -p site-networks
oc process --local -f tekton/network-catalog.yaml \
  -p NETWORK_ATTACHMENT=example-nad \
  -p REQUEST_TEMPLATE_NAME=vm-provision-example-nad \
  -p 'DISPLAY_NAME=Provision VM on Example Network' \
  -o yaml > site-networks/example-nad.yaml
```

Run this once per new network with different values and filenames. Refuse to
overwrite a previously edited generated file; it may contain approved settings.
The `site-networks/` directory is ignored by Git. Client NAD names and network
settings must remain local, not committed to the public repository.

A ConfigMap named `vm-network-example-nad` and request Template named
`vm-provision-example-nad` are generated. They refer to the same network. The
namespace defaults to vm-provisioning; use `-p NAMESPACE=...` only if the shared
pipeline, NAD and configuration already live in that namespace.

## 3. Install definitions, without submitting anything

Inspect generated names and existing owners first; use new names for new entries.
Then validate and install the generated files:

```sh
oc apply --dry-run=server -f site-networks/
oc apply -f site-networks/
```

**These commands do not allocate addresses or create VMs.** The entries are
visible in the catalog, but `PROFILE_ENABLED: "false"` blocks the network path
if a run is submitted. It does not disable the console's Create button. Missing
credentials also prevent execution. Do not submit a request just to test the
block; inspect the form only while Infoblox configuration is pending.

The four/five request inputs stay unchanged. The network choice is the catalog
entry itself. A direct URL uses the new request Template name:

```text
/catalog/instantiate-template?template=vm-provision-example-nad&template-ns=vm-provisioning&preselected-ns=vm-provisioning
```

Old generic request Templates are not removed automatically. Do not change or
delete an existing VLAN entry solely because you added another one.

## 4. Complete and enable each profile later

In its generated file, edit the ConfigMap object's string-valued data:
`INFOBLOX_NETWORK`, `INFOBLOX_NETWORK_VIEW`, `INFOBLOX_DNS_VIEW`, `STATIC_START`,
`STATIC_END`, `GATEWAY`, `DNS_SERVERS`, `DOMAIN`, `DHCP_START`, `DHCP_END`, and
`DHCP_DISABLED`. The generated NETWORK_ATTACHMENT already contains the NAD.

For a network without DHCP, **retain both range keys with empty string values**
and set `DHCP_DISABLED: "true"`. Do not omit the keys or assume the absence of
DHCP merely because the requested VMs use static addresses.

The shared site configuration must also have the real IPAM_URL and WAPI version,
plus approved credential delivery, CA trust and proxy/egress settings. Confirm
that the chosen NAD really connects to that subnet on all eligible nodes.

Only after approval, set `PROFILE_ENABLED: "true"`, reapply that network's file,
and approve one controlled VM request. Blank, missing and REPLACE_ME profile
values fail closed before allocation. The helper validates actual IPv4, gateway,
DNS, static/DHCP overlap and allocation responses as before.

Do not regenerate a configured profile from the default example; that would
reset it to disabled placeholders. Edit the local generated file instead.
Hidden fields and fixed workspace bindings are convenience, not an authorization
boundary against a user allowed to create arbitrary Pods/TaskRuns/PipelineRuns.
Keep this service restricted to trusted platform operators.

## Verification

CI processes the authoring Template locally, then processes each generated
request Template, and checks five inputs, fixed profile bindings, identity and
labels. It also tests disabled/incomplete profiles, snapshot reuse through IPAM
and VM creation, no fallback, and the original single-network path. API calls
are mocked; installed behavior still requires a server dry-run and a controlled
client test after Infoblox details arrive.
