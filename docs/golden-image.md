# Use a prepared RHEL golden image

Reuse a migrated/exported VMware golden image after testing it on OpenShift.
Do not rebuild or re-generalize a known-good source merely to change destination
disk size. Preserve the source image and original working catalog Template.

## Integration into this pipeline

The pipeline processes its own `rhel-standard` Template in the execution namespace.
It does not automatically inherit the VM configuration from the source Template
or from DataSource annotations. Set these admin-owned values in local site.yaml:

- `DATASOURCE_NAME` and `DATASOURCE_NAMESPACE`: the custom golden source, not an
  automatically imported stock OS image with a similar name.
- `STORAGE_CLASS`, `VOLUME_MODE`, `ACCESS_MODE`: verified source/target settings.
- `MACHINE_TYPE`: exact approved type from the working VM, supported on target nodes.
- `GUEST_USER`: existing non-root account permitted by the guest's SSH policy.

The VM definition explicitly uses **BIOS**, a VirtIO boot disk with boot order 1,
and a VirtIO bridged NIC. UEFI/Secure Boot images require a reviewed template
change; do not silently force them to BIOS. CPU/RAM come from the selected
instance type, not the golden VM's sizing. Provisioned VMs start automatically;
a halted golden Template need not dictate the new VM's run strategy.

## Disk capacity and guest filesystems

The root disk mapping is small **110Gi**, medium **500Gi**, large **1Ti**.
CPU/RAM remain 2/4Gi, 4/8Gi and 8/16Gi. A 104Gi source fits all three.
Use binary quantities: 110G decimal would be too small for that source.
The preflight requires matching source/target StorageClass and volume mode;
this is a conservative project guard rather than a general CDI restriction.

A larger PVC/virtual disk does not prove the guest's root filesystem grew.
Inspect `lsblk`, `findmnt /`, `df -hT`, and (for LVM) `pvs`, `vgs`, `lvs`.
No automatic pvresize/lvextend command is added: do not assign all extra space
to one filesystem without agreeing on the image's partition/LVM policy.

## Preserve the existing account

The new key-only bootstrap deliberately avoids the cloud-init users module's
user creation and password-lock/sudo behavior. It sets `users: []`, writes the
public key as literal data to `/run/vm-provision-public-key`, and runs a short
root-owned first-boot command to:

1. Require the selected account to exist with a nonzero UID and an existing home.
2. Append the key only if absent; preserve existing authorized_keys entries.
3. Set correct .ssh/key permissions/ownership and restore SELinux contexts.

It makes no password, group, sudo or sshd configuration changes. It does not
unlock root or create a fallback account. Symlinked .ssh/key paths fail rather
than write elsewhere. The image must contain getent/id/install/restorecon and
allow that account through its existing SSH rules. `CLOUD_USER_SUDO` is obsolete.
A missing account means cloud-init fails; VM resource creation alone cannot
verify guest accounts or prove login access.

Do not assume `users: [{name: ..., ssh_authorized_keys: ...}]` preserves all
password state: cloud-init versions can default lock_passwd to true. Also review
the golden image's existing cloud.cfg and vendor-data; this repo cannot cancel
independent policy already configured inside the image.

References:
- https://docs.cloud-init.io/en/latest/reference/modules.html
- https://github.com/canonical/cloud-init/blob/24.4/cloudinit/distros/__init__.py

## Acceptance before wider use

Read the actual live golden Template and DataSource before installing the
pipeline. Compare firmware, machine, disk bus, network and guest account policy.
Do not copy old MACs, machine UUIDs, IPs or host keys into the pipeline Template.

Test one approved clone: source remains unchanged, CDI/PVC ready, guest boots,
NoCloud/cloud-init succeeds, provided key works for the selected account, and
password lock state, group membership, sudo rules and SSH policy are unchanged.
Verify unique machine IDs and SSH host keys across clones. Check usable disk
space independently of PVC capacity. Then test other sizes as needed.

No disk transfer to an ACM hub is required. Disk bytes and catalog resources
are separate; this pipeline only consumes the approved DataSource.
