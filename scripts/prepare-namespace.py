#!/usr/bin/env python3
"""Prepare an existing VM namespace. Python 3.9+ stdlib and oc; admin use only.

Copy explicitly selected NADs and reuse tekton/target-namespace.yaml. Preview by
default; --apply creates only missing objects. Never update or delete objects.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NAD = "network-attachment-definitions.k8s.cni.cncf.io"
RESOURCE_ANNOTATION = "k8s.v1.cni.cncf.io/resourceName"
RESOURCES = {
    "NetworkAttachmentDefinition": NAD,
    "Role": "roles.rbac.authorization.k8s.io",
    "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
    "VirtualMachineInstancetype": "virtualmachineinstancetypes.instancetype.kubevirt.io",
}
NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")


class PrepError(RuntimeError):
    """A failed check; existing resources must be left alone."""


class Oc:
    def __init__(self):
        self.context = ""
        self.context = self.run("config", "current-context").strip()
        if not self.context:
            raise PrepError("Select the intended cluster with oc login first.")

    def run(self, *args: str, document: dict | None = None) -> str:
        command = ["oc", "--request-timeout=30s"]
        if self.context:
            command += ["--context", self.context]
        try:
            result = subprocess.run(
                command + list(args), input=json.dumps(document) if document is not None else None,
                text=True, capture_output=True, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PrepError(f"oc could not complete {args[0]}: {exc}") from None
        if result.returncode:
            raise PrepError(f"oc {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout

    def get(self, resource: str, name: str, namespace: str = "") -> dict | None:
        args = ["get", resource, name, "--ignore-not-found", "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        text = self.run(*args)
        return json.loads(text) if text.strip() else None


def validate_inputs(target: str, source: str, networks: list[str]) -> None:
    for name in [target, source, *networks]:
        if not isinstance(name, str) or len(name) > 63 or not NAME.fullmatch(name):
            raise PrepError(f"Invalid namespace/NAD name: {name!r}")
    if target == source:
        raise PrepError("The destination must not be the source namespace.")
    if target in ("default", "openshift") or target.startswith(("openshift-", "kube-")):
        raise PrepError("System namespaces are not provisioning targets.")
    if not networks or len(networks) != len(set(networks)):
        raise PrepError("Select at least one NAD; duplicate names are not allowed.")


def config(nad: dict) -> dict:
    try:
        value = json.loads(nad["spec"]["config"])
    except (KeyError, TypeError, ValueError):
        raise PrepError(f"NAD {nad['metadata']['name']} needs valid JSON in spec.config.") from None
    if not isinstance(value, dict) or not value:
        raise PrepError("NAD spec.config must be a nonempty JSON object.")
    return value


def copy_nad(source: dict, target: str) -> dict:
    value = config(source)
    # OVN namespace-qualified configs need a separate design, not a blind rewrite.
    if any("netAttachDefName" in plugin for plugin in [value, *value.get("plugins", [])]):
        raise PrepError("NAD contains netAttachDefName; review namespace-specific configuration manually.")
    metadata = {"name": source["metadata"]["name"], "namespace": target}
    annotation = source["metadata"].get("annotations", {}).get(RESOURCE_ANNOTATION)
    if annotation is not None:
        metadata["annotations"] = {RESOURCE_ANNOTATION: annotation}
    # Fresh metadata: no UIDs, owners, finalizers, status, or source GitOps labels.
    return {"apiVersion": "k8s.cni.cncf.io/v1", "kind": "NetworkAttachmentDefinition",
            "metadata": metadata, "spec": copy.deepcopy(source["spec"])}


def payload(obj: dict) -> dict:
    """Compare functional content, not API bookkeeping or tracking metadata."""
    kind = obj["kind"]
    if kind == "NetworkAttachmentDefinition":
        spec = copy.deepcopy(obj["spec"])
        spec["config"] = config(obj)  # Ignore JSON spacing and object-key order.
        return {"spec": spec, "resourceName": obj["metadata"].get("annotations", {}).get(RESOURCE_ANNOTATION)}
    if kind == "Role":
        rules = [{key: sorted(value) if isinstance(value, list) else value
                  for key, value in rule.items() if value not in (None, [])}
                 for rule in obj.get("rules", [])]
        return {"rules": sorted(rules, key=lambda rule: json.dumps(rule, sort_keys=True))}
    if kind == "RoleBinding":
        subjects = copy.deepcopy(obj.get("subjects", []))
        for subject in subjects:
            if subject.get("kind") == "ServiceAccount":
                subject.setdefault("apiGroup", "")
        return {"roleRef": obj["roleRef"],
                "subjects": sorted(subjects, key=lambda subject: json.dumps(subject, sort_keys=True))}
    if kind == "VirtualMachineInstancetype":
        return {"spec": obj["spec"]}
    raise PrepError(f"Unexpected target resource kind: {kind}")


def identity(obj: dict) -> str:
    return f"{obj['kind']}/{obj['metadata']['name']}"


def check_match(live: dict, wanted: dict) -> None:
    if live["metadata"].get("deletionTimestamp"):
        raise PrepError(f"{identity(wanted)} is being deleted; wait and review.")
    actual, expected = payload(live), payload(wanted)
    if actual != expected:
        difference = "\n".join(difflib.unified_diff(
            json.dumps(actual, indent=2, sort_keys=True).splitlines(),
            json.dumps(expected, indent=2, sort_keys=True).splitlines(),
            fromfile="existing", tofile="requested", lineterm="",
        ))
        raise PrepError(f"Conflict: {identity(wanted)} differs. Nothing will be overwritten.\n{difference}")


def desired_objects(oc: Oc, target: str, source: str, networks: list[str]) -> list[dict]:
    for namespace in (source, target):
        obj = oc.get("namespaces", namespace)
        if (not obj or obj["metadata"].get("deletionTimestamp")
                or obj.get("status", {}).get("phase") != "Active"):
            raise PrepError(f"Namespace {namespace} must already exist and be Active.")
    sa = oc.get("serviceaccounts", "vm-provision", source)
    if not sa or sa["metadata"].get("deletionTimestamp"):
        raise PrepError(f"Missing active central ServiceAccount: {source}/vm-provision")
    objects = []
    for name in networks:
        nad = oc.get(NAD, name, source)
        if not nad or nad["metadata"].get("deletionTimestamp"):
            raise PrepError(f"Missing active source NAD: {source}/{name}")
        objects.append(copy_nad(nad, target))
    rendered = json.loads(oc.run(
        "process", "--local", "-f", str(ROOT / "tekton/target-namespace.yaml"),
        "-p", f"TARGET_NAMESPACE={target}", "-p", f"PROVISIONING_NAMESPACE={source}", "-o", "json",
    ))
    setup = rendered["items"]
    if sorted(obj["kind"] for obj in setup) != sorted(
            ["Role", "RoleBinding"] + ["VirtualMachineInstancetype"] * 3):
        raise PrepError("Unexpected resources in target-namespace.yaml; review before using this script.")
    objects += setup
    if any(obj["metadata"].get("namespace") != target for obj in objects):
        raise PrepError("Rendered resources must stay in the target namespace.")
    if len({identity(obj) for obj in objects}) != len(objects):
        raise PrepError("Duplicate rendered resources.")
    # Grant access last, after the network and sizing resources exist.
    return sorted(objects, key=lambda obj: obj["kind"] == "RoleBinding")


def verify_access(oc: Oc, target: str, source: str, networks: list[str]) -> None:
    account = f"system:serviceaccount:{source}:vm-provision"
    checks = [("create", "virtualmachines.kubevirt.io"), ("get", "virtualmachines.kubevirt.io"),
              ("get", "datavolumes.cdi.kubevirt.io"), ("get", "persistentvolumeclaims")]
    checks += [("get", f"{NAD}/{name}") for name in networks]
    checks += [("get", f"virtualmachineinstancetypes.instancetype.kubevirt.io/poc-{size}")
               for size in ("small", "medium", "large")]
    for verb, resource in checks:
        answer = oc.run("auth", "can-i", verb, resource, "-n", target, "--as", account).strip()
        if answer != "yes":
            raise PrepError(f"Runtime permission not confirmed: {account} {verb} {resource} in {target}")


def prepare(oc: Oc, target: str, source: str, networks: list[str], apply: bool = False) -> None:
    validate_inputs(target, source, networks)
    wanted = desired_objects(oc, target, source, networks)
    missing = []
    # Inspect ALL resources before the first create, including conflicts at the end.
    for obj in wanted:
        live = oc.get(RESOURCES[obj["kind"]], obj["metadata"]["name"], target)
        if live is None:
            missing.append(obj)
        else:
            check_match(live, obj)
    for obj in wanted:
        print(f"{'CREATE' if obj in missing else 'MATCH ':6} {target}/{identity(obj)}")
    if missing:
        oc.run("create", "--dry-run=server", "-f", "-", "-o", "json",
               document={"apiVersion": "v1", "kind": "List", "items": missing})
    if not apply:
        print("DRY RUN passed. No resources changed. Use --apply after reviewing the plan.")
        return
    for obj in missing:
        # Create, never apply/replace: a concurrent create cannot be overwritten.
        result = oc.run("create", "-f", "-", "-o", "name", document=obj)
        print(result.strip())
    for obj in wanted:
        live = oc.get(RESOURCES[obj["kind"]], obj["metadata"]["name"], target)
        if live is None:
            raise PrepError(f"Verification failed: missing {identity(obj)}")
        check_match(live, obj)
    verify_access(oc, target, source, networks)
    print(f"PREPARED: {target}; {len(networks)} NAD(s), three sizes, scoped access verified.")
    print("No VMs, IP allocations, Secrets, or ConfigMaps were created. Guest connectivity is not tested.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace", help="Existing approved destination namespace")
    parser.add_argument("--networks", nargs="+", required=True, help="Explicit NAD names to copy")
    parser.add_argument("--source-namespace", default="vm-provisioning",
                        help="Central NAD and vm-provision ServiceAccount namespace (default: vm-provisioning)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview and server-validate only (default)")
    mode.add_argument("--apply", action="store_true", help="Create missing resources and verify runtime access")
    args = parser.parse_args(argv)
    try:
        validate_inputs(args.namespace, args.source_namespace, args.networks)
        oc = Oc()
        print(f"Context: {oc.context}\nSource: {args.source_namespace}\nTarget: {args.namespace}")
        prepare(oc, args.namespace, args.source_namespace, args.networks, args.apply)
        return 0
    except (PrepError, KeyError, TypeError, ValueError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        print("No overwrite, rollback or automatic retry. Inspect any partial creates before rerunning.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
