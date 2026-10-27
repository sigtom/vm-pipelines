# Client setup: golden image and direct Infoblox IPAM

Use helper **vm-ipam:0.4** with the current Task. It creates IPAM-only host
records using short names and next-available subnet allocation. DNS and DHCP
are disabled for the host; the generated VM MAC is not sent to Infoblox.
For an existing installation follow [ipam-only-upgrade.md](ipam-only-upgrade.md).
The earlier 0.3 helper is NOT interchangeable and must be rebuilt.

## 1. Checkout and prerequisites

Use main, preserving local image references, site.yaml and site-networks:

```sh
git pull --ff-only --autostash origin main
git status --short
git diff --name-only --diff-filter=U
```

Stop for conflicts. Do not reset/clean or apply placeholder image references.
Require Virtualization/CDI, Pipelines tekton.dev/v1, Builds/internal registry,
a prepared golden DataSource, compatible CSI storage, and approved NAD/node
bridge configuration. The default central namespace is vm-provisioning.

The pipeline uses its own rhel-standard Template. Inspect the actual approved
golden VM: BIOS/VirtIO/boot order 1 and MACHINE_TYPE must match. UEFI images
need a reviewed change. Do not alter the source disks or working golden Template.

Root disk requests are small 110Gi, medium 500Gi, large 1Ti. A 104Gi source fits
all three; source/target class and volumeMode must match for this MVP. CPU/RAM
are 2/4Gi, 4/8Gi, 8/16Gi. Guest partition/LVM growth is a separate policy.

## 2. Site and network configuration

```sh
test -f site.yaml || cp config/site.example.yaml site.yaml
```

Edit the existing ignored file without replacing approved settings. All values
are strings. Shared settings are VM_NAMESPACE (default destination), golden
DataSource/namespace, StorageClass/access/volume modes, MACHINE_TYPE, GUEST_USER,
IPAM_URL (verified HTTPS origin only) and supported INFOBLOX_WAPI_VERSION.

A selected network profile overrides the site's network settings as one complete
set: NETWORK_ATTACHMENT, INFOBLOX_ALLOCATION_MODE=subnet, INFOBLOX_NETWORK,
INFOBLOX_NETWORK_VIEW, GATEWAY, DNS_SERVERS and DOMAIN. Resolver addresses are a
comma-separated IPv4 list. DOMAIN is for the guest, not Infoblox registration.
Legacy single-network requests need these same network keys in site.yaml.

No DNS-view, static start/end, or DHCP-range fields are used by 0.4. Whole-subnet
selection is explicit; Infoblox administrators must approve it and reserve all
infrastructure/DHCP-owned space. Do not infer approval from UNUSED status or an
empty range query. Missing mode fails before allocation rather than silently
broadening the old policy. DNS/DHCP false refers to the new host only.

Keep unconfigured network profiles disabled. Never overwrite their known values
with generated examples. See [network-catalog.md](network-catalog.md).
Remove old AAP fields and CLOUD_USER_SUDO; never commit client data or credentials.

Guest cloud-init uses users: [] and a key-only script for the existing approved
non-root account. No password/group/sudo/sshd changes or fallback accounts are
created. See [golden-image.md](golden-image.md) for required guest checks.

## 3. Build and pin helper 0.4

Set BASE_IMAGE in build/buildconfig.yaml to an approved UBI Python 3.12 digest
or mirror. This must not be REPLACE_ME. Build only the required files:

```sh
oc apply -k build
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow --wait
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.4 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Set that new digest in tekton/task.yaml's ipam-image default; keep the approved
CLI digest in cli-image. The Task's offline version check prevents running old
0.3 or AAP images. No pip/SDK/Ansible or API credentials are used during the build.
Review runtime lifecycle and image scanning. Runtime API trust/proxy settings
are separate from registry/build configuration. Configure HTTP(S)_PROXY/NO_PROXY
only as required; the cluster proxy is not assumed to be inherited.

## 4. Credentials and certificate trust

Only reserve-ip receives Secret vm-ipam-credentials, keys INFOBLOX_USERNAME and
INFOBLOX_PASSWORD. Use the approved scoped account and secure secret process.
For a private CA install the approved PEM bundle in ConfigMap vm-ipam-ca, key
ca-bundle.crt. The helper uses SSL_CERT_FILE when that bundle exists; otherwise
it uses normal image trust. Both chain and API hostname must validate.

A successful curl -k test does not prove verified TLS will work from cluster
pods. There is no TLS-bypass flag in this helper. No credentials/CA need copying
to VM namespaces. Later connection, secret or CA changes do not need a rebuild.

## 5. Fresh installation: definitions only

Review existing owners; do not overwrite GitOps-managed resources. The source
Roles/RoleBindings in tekton/serviceaccount.yaml must cover actual source
DataSource/PVC namespaces and clone-source permission for the central SA.

```sh
oc apply --dry-run=server -k catalog &&
oc apply --dry-run=server -f site.yaml &&
oc apply --dry-run=server -k tekton &&
oc apply --dry-run=server -f tekton/request-template.yaml &&
oc apply -k catalog &&
oc apply -f site.yaml &&
oc apply -k tekton &&
oc apply -f tekton/request-template.yaml
```

This installs definitions, not a PipelineRun or VM. Source images remain unchanged.
Create network entries with [network-catalog.md](network-catalog.md). Enroll each
approved destination once using [target-namespaces.md](target-namespaces.md).

## 6. Read-only check, then one approved VM

Follow [ipam-only-upgrade.md](ipam-only-upgrade.md) to process
tekton/ipam-check.yaml into a GET-only TaskRun using the new digest and intended
profile. This verifies runtime image, settings, credentials/TLS and host search;
it does not reserve an IP or prove clone/guest readiness.

Only after it passes, enable the intended profile and open its catalog entry.
Catalog Project and the top Namespace stay vm-provisioning. Enter name, size,
count, SSH key, labels and Target VM namespace. Start with one small VM and a
new name, not the previous curl test name. Submit exactly once and follow
PipelineRuns centrally. Examine VM/disks in the chosen target namespace.

Each target needs its NADs, three instance types and scoped central-SA RoleBinding.
No duplicated pipeline or IPAM Secret is needed. Trusted operators can choose any
target allowed to the central SA; there is no human impersonation. Hidden fields
are not a security boundary. Never grant cluster-admin to the provisioning SA.

Verify IPAM host name/view/address and DNS/DHCP=false, then CDI/PVC, boot, cloud-init,
unique machine/SSH identities, guest networking, SSH, labels and disk capacity.
Use the allocated IP for SSH: this workflow intentionally creates no DNS record.
Pipeline success means resource creation, not successful guest hardening.

## Failure handling and tests

No retries, rollback or deletion. A failed/timed-out POST can have created a host.
Preserve logs, the encoded reference and receipt before Pod cleanup. Never replay
an uncertain request without inspecting Infoblox and any VM/disks. A partial
batch may leave allocations without VMs. emptyDir is temporary storage.

```sh
python3 -m pip install PyYAML==6.0.3  # Tests only.
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

CI mocks WAPI and uses actual oc for native rendering. It is not a live client
API/cluster test. Guest AD/Salt/subscriptions/agents are separate integrations.
