# Client installation: Tekton -> AAP -> Infoblox

This installs OpenShift resources only. A working AAP job returning the
[vm_network contract](aap-contract.md) is required before provisioning can pass.
No customer AAP/Infoblox or live cluster was accessed during this code change.

## 1. Confirm prerequisites and ownership

Use an approved administrator. Require Virtualization/CDI, Pipelines
(`tekton.dev/v1`), OpenShift Builds/internal registry, an approved RHEL
DataSource, compatible CSI storage and an existing bridged NAD.

```sh
oc version
oc get datasource -A
oc get storageclass
oc get network-attachment-definitions -A
oc get pipelines.tekton.dev -A
oc get imagestreamtags -n openshift
```

Default execution/VM namespace is **vm-provisioning**. Have the platform team
create it and its network attachment first. If using another namespace, update
manifest namespaces, RoleBinding subjects, and site settings consistently.
Do not move the source-image Role into the target namespace with a blanket
namespace transformation. Do not overwrite resources already owned by GitOps.

The preflight requires source/target to have the same StorageClass and volume
mode and the target disk to be large enough. This is a conservative MVP guard,
not a general CDI restriction. Thin provisioning/live migration depend on the
backend. See [golden-image.md](golden-image.md) for VMware-derived boot sources.

## 2. Fill site settings and agree on AAP output

```sh
cp config/site.example.yaml site.yaml
```

Edit ignored `site.yaml`. Keep every value a string. Required OpenShift settings:
`VM_NAMESPACE`, `NETWORK_ATTACHMENT`, `DATASOURCE_NAME`, `DATASOURCE_NAMESPACE`,
`STORAGE_CLASS`, `ACCESS_MODE` (ReadWriteOnce/ReadWriteMany), `VOLUME_MODE`
(Filesystem/Block), `GUEST_USER` and `CLOUD_USER_SUDO`.

| AAP variable | Value |
| --- | --- |
| `AAP_API_BASE_URL` | Full HTTPS controller base, `/api/controller/v2` for gateway deployments or `/api/v2` for direct controllers. |
| `AAP_JOB_TEMPLATE_ID` | Positive numeric ID of the approved regular job template. |
| `NETWORK_PROFILE` | Approved profile ID, matching the NAD's network. Letters/digits/dot/underscore/hyphen; starts with letter/digit. |
| `AAP_JOB_TIMEOUT_SECONDS` | Per VM, including launch/queue/run/read-back; default 600. |
| `AAP_POLL_INTERVAL_SECONDS` | Status polling; default 5. |

AAP owns Infoblox credentials, pool/views, gateway, DNS and domain. It returns
the real assigned `ip_cidr`. Remove the old `IPAM_URL`, `INFOBLOX_*`, pool and
DHCP fields from the deployment settings. They are no longer used. No fallback
to direct Infoblox is provided.

Give the AAP team **docs/aap-contract.md** before launching. Configure unattended
inventory/credentials, job type Run, one slice, and either Prompt on launch for
Variables or a survey covering the four contract inputs. Their playbook must
publish `artifacts.vm_network` with `set_stats`, not just print an IP in stdout.

`CLOUD_USER_SUDO: "false"` means no new cloud-init sudo rule. Passwordless sudo
requires explicit approval with `ALL=(ALL) NOPASSWD:ALL`. This setting does not
remove privileges baked into an image or update an existing VM. Salt, AD,
subscriptions and agents remain separate, customer-owned bootstrap work.

## 3. Build the AAP caller inside the virtualization cluster

Set `BASE_IMAGE` in `build/buildconfig.yaml` to an approved UBI Python 3.12 image
or mirror **by digest**. Review its support lifecycle. No pip/Ansible/SDK is
needed in the image. No token or Infoblox access is needed during the build.

```sh
oc apply -k build
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.2 -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Set the two administrator-only defaults in **tekton/task.yaml**:
`ipam-image` = the newly built image digest; `cli-image` = the approved,
client-version-matched OpenShift CLI image digest with bash. Do not deploy with
REPLACE_ME image values or reuse the old direct-Infoblox image. The image/script
names are retained; the content is now an AAP caller.

Build/registry proxy and CA settings are separate from runtime API traffic.
Verify cluster/build proxy and image trust. Runtime HTTP(S)_PROXY/NO_PROXY can
be set in site.yaml. Include API/internal endpoints in NO_PROXY as appropriate.
Only Tekton -> AAP HTTPS is needed for this integration; AAP -> Infoblox is
owned by the automation team. Mirror images for disconnected environments.

## 4. Provide the AAP token and optional CA

Create Secret **vm-aap-credentials**, key **AAP_TOKEN**, through approved
Vault/External Secrets or another secure process. The token must be able to
read/execute the agreed template and read jobs/results; do not grant full AAP
admin. AAP stores Infoblox credentials. No credentials belong in site.yaml,
Git, form inputs, VM YAML, or extra_vars.

For a controlled initial creation from bash, without putting the value in
arguments/history or a file (do not enable shell tracing):

```sh
set +x
read -r -s -p 'AAP token: ' AAP_TOKEN; printf '\n'
export AAP_TOKEN
python3 - <<'PY' | oc create -f -
import json, os
print(json.dumps({"apiVersion":"v1","kind":"Secret","type":"Opaque",
    "metadata":{"name":"vm-aap-credentials","namespace":"vm-provisioning"},
    "stringData":{"AAP_TOKEN":os.environ["AAP_TOKEN"]}}))
PY
unset AAP_TOKEN
```

An existing Secret is not overwritten by that command; inspect its owner and
use the approved rotation process. Agree on token expiry and rotation ownership.

For a private AAP CA, supply its trusted PEM chain/bundle:

```sh
oc -n vm-provisioning create configmap vm-aap-ca \
  --from-file=ca-bundle.crt=/approved/path/aap-ca-bundle.pem
```

The Task mounts this optional ConfigMap read-only and sets SSL_CERT_FILE when
the bundle exists. Otherwise Python uses its normal trust store. There is no
skip-TLS flag and redirects are not followed. Wrong hostnames or incomplete CA
chains must be fixed, not bypassed. Registry trust remains a separate concern.

## 5. Install without provisioning

Review source-image access in **tekton/serviceaccount.yaml**: put the source
Role/RoleBinding in the DataSource/source PVC namespaces. Grant source reads in
both if they differ; clone-source `datavolumes/source` applies in the source PVC
namespace. Runtime VM permissions remain get/create only, with scoped reads;
no new cluster-wide access, delete privileges, or Secret-read API rights.

```sh
oc apply --dry-run=server -k catalog
oc apply --dry-run=server -k tekton
oc apply -k catalog
oc apply -f site.yaml
oc apply -k tekton
oc apply -f tekton/request-template.yaml
```

Installing these definitions does not launch AAP or create VMs. On an existing
deployment, pause submissions and replace the site settings, image reference,
Task, Pipeline and request Template together. Do not alter/delete existing VMs.
Review any existing field ownership before applying. The old Infoblox Secret
is no longer referenced; remove it only through the approved secret process
once no running or retained old workloads need it. Do not change a ConfigMap
while requests using it are running.

## 6. Check AAP from the cluster WITHOUT allocating an IP

After installation and approval to create a diagnostic TaskRun, set HELPER_IMAGE
to the same pinned digest used by the Task. This example performs **GET only**;
it does not launch a job or create a VM. A passed check does not prove result
artifacts or Infoblox behavior; those need one approved end-to-end request.

```sh
HELPER_IMAGE='REPLACE_ME_BUILT_IPAM_IMAGE_BY_DIGEST'
oc create -f - <<EOF
apiVersion: tekton.dev/v1
kind: TaskRun
metadata:
  generateName: aap-readonly-check-
  namespace: vm-provisioning
spec:
  serviceAccountName: vm-provision
  timeout: 5m
  taskSpec:
    volumes:
      - name: aap-ca
        configMap:
          name: vm-aap-ca
          optional: true
    steps:
      - name: check
        image: ${HELPER_IMAGE}
        securityContext:
          runAsNonRoot: true
          allowPrivilegeEscalation: false
          capabilities:
            drop: [ALL]
          seccompProfile:
            type: RuntimeDefault
        env:
          - name: HOME
            value: /tmp
          - name: DOCKER_CONFIG
            value: /tmp/.docker
        envFrom:
          - configMapRef:
              name: vm-provision-config
          - secretRef:
              name: vm-aap-credentials
        volumeMounts:
          - name: aap-ca
            mountPath: /etc/aap-ca
            readOnly: true
        script: |
          #!/usr/bin/env sh
          set -eu
          if [ -f /etc/aap-ca/ca-bundle.crt ]; then
            export SSL_CERT_FILE=/etc/aap-ca/ca-bundle.crt
          fi
          python3 /opt/vm/ipam.py check-aap
EOF
```

Read its logs in the Pipelines console. Expect:
`AAP read-only template checks passed; no job launched`.
The same command can run locally with the AAP settings/token supplied securely,
but a local success does not verify cluster egress or certificate trust.

## 7. First approved VM request

Check the actual operator permissions; cluster-admin success is not proof of
non-admin access. The catalog creates a parameter Secret and TemplateInstance
before the PipelineRun. Hidden fields are not an authorization boundary.
Keep arbitrary Pod/TaskRun creation and editable Templates restricted to
trusted platform operators; do not grant broad edit merely for a form.

Open **Ecosystem -> Software Catalog -> Request RHEL VM -> Instantiate Template**
in vm-provisioning. Enter unused name, small, count 1, real ed25519 public key,
and optional labels such as `foo=bar;boo=far`. This launches real AAP/Infoblox
work and a VM. Follow progress under Pipelines -> PipelineRuns. Explicit
alternative: fill `tekton/pipelinerun.example.yaml` and create it once.

Verify AAP job ID and result, actual Infoblox name/MAC/IP/DNS, CDI/PVC, VM Ready,
cloud-init, SSH, and labels. Pipeline success means VM resources were created,
not that boot, hardening or DNS testing completed. Test batches separately.

Labels remain optional; semicolon-separated key=value entries apply to every
VM's metadata before creation. Invalid/duplicate keys and any labels owned by
the native Template are rejected before AAP. Labels do not modify the VMI,
disk, AAP job, or Infoblox record. Do not use request labels as authorization.

## Failure recovery and deadlines

Default per-VM AAP deadline is 600 seconds, poll interval 5 seconds. The supplied
Task timeout is 60 minutes and PipelineRun timeout 65 minutes, allowing five
sequential 10-minute jobs plus preflight. If changing AAP_JOB_TIMEOUT_SECONDS,
adjust both Tekton timeouts for maximum batch size and execution overhead.
A pipeline/HTTP timeout or cancellation **does not cancel the remote AAP job**.

Retain the `AAP_REQUEST` log lines and workspace files `allocations` and
`allocations.requests.jsonl`. Prepared events record request/MAC before launch;
launched events add job IDs; validated events record verified addresses.
No token or unfiltered API output is logged. emptyDir is temporary: collect logs
and receipts before Pod cleanup; it is not a durable transaction database.

No automatic retries, relaunches, cancellations, deletes or rollback. An
uncertain POST can have launched a job. A partial batch may have allocations
without VMs. Investigate the AAP job, request ID/MAC, external records, and VM/
root disk state before an approved manual recovery. Do not create a new run to
blindly retry; it has a new UID. AAP's playbook owns duplicate-safe allocation.

## Tests and references

```sh
python3 -m pip install PyYAML==6.0.3   # Tests only
python3 -m unittest discover -s tests -v
python3 -S -m unittest discover -s tests -p test_ipam.py -v
```

Native tests require the client-matched oc executable; CI installs 4.21 and
fails rather than silently skipping those tests. AAP tests mock job launch,
polling and artifacts, not a live controller. See [aap-contract.md](aap-contract.md)
for official API/variable/artifact references and the playbook team's deliverable.
