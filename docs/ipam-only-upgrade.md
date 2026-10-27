# Upgrade an installed pipeline to IPAM-only helper 0.4

This guide updates an existing deployment without reapplying its VM Template,
instance types, request forms, target RBAC or unrelated network profiles.
Pause submissions while changing the Task/image/profile contract.

## What changed

- Search record:host using the short name and network_view, not FQDN/DNS view.
- Create one host with configure_for_dns=false and configure_for_dhcp=false.
- Request func:nextavailableip:<subnet-CIDR>,<network-view>; no range object.
- Accept references ending /%20 and preserve their encoding for readback.
- Verify name, network view, flags and one usable non-infrastructure IPv4 address.
- Keep the existing receipt, local VM MAC, user-supplied gateway/resolvers/domain,
  six-field catalog forms, sizes, labels, golden image and target routing.

No MAC is sent to Infoblox. No DNS or DHCP resource is created for the VM.
INFOBLOX_DNS_VIEW, STATIC_START/END and DHCP_START/END/DISABLED are obsolete.
The explicit INFOBLOX_ALLOCATION_MODE=subnet setting prevents silently broadening
old bounded policies. Setting it acknowledges administrator-approved subnet-wide
allocation. Infoblox must reserve/exclude all infrastructure/DHCP-owned space.

## 1. Pull and preserve local changes

```sh
cd ~/vm-pipelines-rhel9
git pull --ff-only --autostash origin main
git status --short
git diff --name-only --diff-filter=U
```

Stop on conflicts; do not reset/clean. Preserve site.yaml and site-networks.
Use the new scripts/ipam.py and Task together. Back up current BuildConfig,
Task, Pipeline, site ConfigMap and intended network ConfigMap outside Git.
Do not export credentials into backups or logs.

## 2. Build 0.4 using the existing approved BuildConfig

This path preserves its base-image digest, registry trust, proxy and other
client-approved settings. It changes only the output tag before a binary build.
For a fresh build use [runbook.md](runbook.md) instead.

```sh
oc -n vm-provisioning patch buildconfig vm-ipam --type=merge \
  -p '{"spec":{"output":{"to":{"kind":"ImageStreamTag","name":"vm-ipam:0.4"}}}}'
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow --wait
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.4 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Use the returned digest, not 0.3's old digest. Set it in tekton/task.yaml's
ipam-image default. Keep the approved cli-image digest. Replace the existing
default even if it is already a digest; placeholder-only sed replacements will
not update a previously configured file. Validate, then apply the Task/Pipeline:

```sh
oc apply --dry-run=server -f tekton/task.yaml -f tekton/pipeline.yaml &&
oc apply -f tekton/task.yaml -f tekton/pipeline.yaml
```

Do not reapply a placeholder BuildConfig or regenerate catalog/network resources.
Keep the corresponding local BuildConfig output tag consistent with 0.4 before
using it for future builds. If SSH disconnects, inspect the existing Build and
its logs instead of assuming the build failed. The build creates no allocation.

## 3. Configure the one tested network

Keep shared VM/image/storage/account settings unchanged. Set IPAM_URL to the
approved HTTPS origin and INFOBLOX_WAPI_VERSION to the tested appliance version.
The endpoint must match its certificate; a successful curl -k test does not prove
that the image's TLS verification will work. Use an approved CA bundle in
vm-ipam-ca/ca-bundle.crt when normal trust is insufficient. Do not disable TLS.

Edit only the intended existing network ConfigMap. Keep it disabled initially:

```yaml
PROFILE_ENABLED: "false"
NETWORK_ATTACHMENT: REPLACE_ME_EXISTING_NAD
INFOBLOX_ALLOCATION_MODE: subnet
INFOBLOX_NETWORK: REPLACE_ME_CONFIRMED_SUBNET_CIDR
INFOBLOX_NETWORK_VIEW: REPLACE_ME_CONFIRMED_NETWORK_VIEW
GATEWAY: REPLACE_ME_CLIENT_GATEWAY
DNS_SERVERS: REPLACE_ME_CLIENT_RESOLVERS_COMMA_SEPARATED
DOMAIN: REPLACE_ME_CLIENT_SEARCH_DOMAIN
```

Remove obsolete DNS-view/range/DHCP keys from this profile. Do not guess gateway,
resolvers or domain from the allocated address. Keep all other profiles unchanged
and disabled pending their own configuration. Update the local saved settings
too, but do not blindly reapply old generated Template objects.

For a legacy request without a network workspace, put these same network keys
(except PROFILE_ENABLED) in site.yaml/the shared ConfigMap. Do not enable a legacy
request against a subnet whose configuration or account permissions are unknown.

Use central Secret vm-ipam-credentials with INFOBLOX_USERNAME and
INFOBLOX_PASSWORD. Keep credentials out of Git, command-line passwords and logs.
If it exists, inspect ownership and rotate through the approved secret process;
do not overwrite an external-secret-managed value. No new target Secret is needed.

## 4. Verify from a cluster pod without allocating

Fill an unused proposed VM name and the selected ConfigMap name below. Process
only the authoring Template; creating its output starts a GET-only TaskRun.
It uses the new helper, the same central SA/credential/CA and selected profile.
PROFILE_ENABLED can remain false for this read-only diagnostic.

```sh
HELPER_IMAGE=$(oc -n vm-provisioning get istag vm-ipam:0.4 \
  -o jsonpath='{.image.dockerImageReference}')
oc process --local -f tekton/ipam-check.yaml \
  -p "HELPER_IMAGE=$HELPER_IMAGE" \
  -p NETWORK_CONFIGMAP=REPLACE_ME_EXISTING_PROFILE \
  -p VM_NAME=REPLACE_ME_UNUSED_SHORT_NAME \
  -o yaml > "$HOME/ipam-check.yaml"
oc create --dry-run=server -f "$HOME/ipam-check.yaml" &&
oc create -f "$HOME/ipam-check.yaml"
```

Read its check-step logs under Pipelines -> TaskRuns. Expect:
`IPAM read-only checks passed; no IP allocated`.
A TLS/auth/egress/configuration error must be resolved before proceeding.
A pass proves host-search access, not write permissions, free-address capacity,
VM admission, cloning, guest boot or guest-network connectivity.

The same check can run locally with the appropriate environment and securely
supplied credentials: `python3 scripts/ipam.py check-ipam <unused-name>`.
Running the helper without arguments performs real allocation; do not confuse
that with the read-only command.

## 5. Enable one profile and submit one new VM

After approval and a successful check, set PROFILE_ENABLED to the string true
on that profile only. Open the existing matching catalog entry. Keep the top
Namespace on vm-provisioning, use an approved Target VM namespace, choose small,
count 1, supply a real ed25519 public key, and optional labels. Use a new name,
not the disposable host already created by curl. Do not manually assign its IP.
Submit once. The helper will select another available address from Infoblox.

Follow the PipelineRun in vm-provisioning. Retain reserve-ip logs and the returned
reference. Check the host's name/view/IP and DNS/DHCP=false. Check CDI/PVC, VM Ready,
cloud-init, actual guest IP/gateway/resolvers/domain and support-account SSH.
Use the allocated IP for SSH; no DNS record was created. Check guest disk capacity
separately; 110Gi PVC size does not automatically resize a golden-image LVM layout.

On an error, timeout or disconnect, inspect the original run and the named IPAM
host before any retry. No automatic record/VM cleanup is performed. The older
disposable curl allocation remains separate until its approved manual cleanup.
