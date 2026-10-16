# Reuse a VMware golden image

Request an export of a powered-off VM cloned from the approved VMware template.
Leave the original untouched. Obtain an OVA or complete OVF/VMDK package through
an approved transfer path, with checksums, OS version, firmware mode, disk layout,
image owner and required hardening details. Never publish the image or credentials.

Use the migration/import mechanism supported by the installed Migration Toolkit
for Virtualization version. An MTV OVA provider may be appropriate; confirm its
export-format, conversion and source-storage prerequisites. An exported disk alone
does not prove the guest is prepared for KVM or cloud-init.

1. Import a disposable copy and verify firmware, drivers, disks and guest agent.
2. Confirm cloud-init supports NoCloud and the guest accepts the intended network data.
3. Generalize a copy: machine-id, SSH host keys, AD membership, subscriptions and
   security-agent registrations must not be cloned as shared machine identities.
4. Publish the clean disk as an approved CDI DataSource.
5. Verify source-clone permission, target storage, and a root disk at least as large
   as the source. This MVP requires matching source/target StorageClass and volume mode.
6. Test a newly provisioned VM, not just the imported source.

The current VM Template does not override firmware settings. Adjust it to the
approved image's BIOS/UEFI requirements before use; do not assume all exported
VMware images boot with the same settings. No image transfer to an ACM hub is
required by this workflow. Cluster-management manifests do not transport disk bytes.
