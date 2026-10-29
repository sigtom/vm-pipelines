# SSH public keys: Ed25519 and RSA

The request accepts one bare OpenSSH public key: `ssh-ed25519` or `ssh-rsa`.
RSA keys must be at least 2048 bits. The maximum input is one printable line
of 8192 characters, including its optional comment. Private keys, authorized_keys
options, certificates, unsupported types, malformed encodings, and multiple
key lines are rejected before IPAM. Original accepted text is preserved.
One key is used for all VMs in a batch; there is no second key input for count=2.

This is format/size validation, not proof of private-key ownership or of the
client's guest security policy. The golden image may impose stricter policy.
No sshd, password, sudo, system crypto-policy or host-key setting is changed.
An `ssh-rsa` public key can use RSA/SHA-2 signatures. Accepting its key format
is not an instruction to enable the obsolete RSA/SHA-1 signature algorithm.
See RFC 4253 section 6.6 and RFC 8332 sections 2-3.

## Existing 0.4 installations

The IPAM 0.4 contract, Task scripts and build tag stay unchanged. This change
updates the helper's SSH parser; **a fresh image build and digest update are
required**. `--version` alone does not distinguish an older Ed25519-only 0.4
image. Verify the new source with an RSA key and deploy the new digest.
The runtime helper still needs no third-party Python package or ssh-keygen.

1. Pull main, preserving site files and local image references. Stop on conflicts.
2. Validate the supplied public key offline using the updated source.
3. Rebuild with the existing approved BuildConfig and only the required source.
4. Update the deployed Task's ipam-image default and the local task.yaml to the
   resulting digest. Preserve the CLI image, Infoblox settings, credentials,
   trusted certificate, lowercase no_proxy, namespace routing and network profiles.
5. Run one new request (or rerun a request proven to have stopped at key validation).
   Confirm the new TaskRun actually uses the new digest. Verify SSH separately.

Offline key check (set the path to an existing **public** key):

```sh
REQ_SSH_KEY="$(cat /approved/path/client-key.pub)" \
  python3 -S scripts/ipam.py check-ssh-key
```

This command neither contacts Infoblox nor creates any cluster resources.
Do not run the helper without `check-ssh-key`: no arguments invokes allocation.

Minimal binary build, retaining the already configured BuildConfig:

```sh
stage=$(mktemp -d)
mkdir -p "$stage/scripts"
cp Containerfile "$stage/"
cp scripts/ipam.py "$stage/scripts/"
oc -n vm-provisioning start-build vm-ipam --from-dir="$stage" --follow --wait
rm -rf "$stage"
oc -n vm-provisioning get istag vm-ipam:0.4 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

Confirm the existing build outputs vm-ipam:0.4 before using these commands.
Do not reapply a placeholder BuildConfig. Stop on build errors; after an SSH
session disconnect, inspect the existing build instead of blindly starting one.
Use the returned new digest, not the previous 0.4 image's digest.

## Existing catalog descriptions

The repository's Pipeline and both request Template definitions now describe
Ed25519 and RSA. Existing generated per-VLAN forms keep their old help text until
updated. That text does not enforce the key type; the helper does.
Do not regenerate network-catalog output over approved ConfigMaps merely to edit
help text. Update only the SSH_PUBLIC_KEY parameter description on the installed
request Templates; preserve titles, workspaces, targets, identity and other fields.
A suitable description is:

`One OpenSSH Ed25519 or RSA public key (RSA at least 2048 bits) for the existing guest account. Public key only; used for every VM in the request.`

There is no golden-image, guest bootstrap, Infoblox, batch naming, namespace
setup or Windows change in this patch. Customer public keys and internal
configuration must not be committed as test fixtures.

## Testing and recovery

Run `python3 -S -m unittest discover -s tests -p test_ssh_keys.py -v` for the
parser/CLI checks. Native rendering uses oc locally; CI requires that test too.
The full suite also checks unchanged IPAM behavior and existing request flows.
Tests do not prove a client's key ownership, actual guest login or a live batch.

For an old failure in validate-key, confirm preflight/reserve-ip/create-vms were
skipped before rerunning. A later allocation or VM-creation failure needs manual
reconciliation first. Do not delete existing VMs, disks or IPAM records merely
to retry, and do not enable RSA/SHA-1 or relax guest policy to work around this
parser restriction.
