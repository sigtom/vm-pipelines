#!/usr/bin/env python3
"""Offline, additive upgrade of existing vm-provision request Templates.

Read `oc get templates -o json` from stdin; write ONLY matching updated Templates.
No API calls, YAML dependency, credentials or ConfigMap changes. Review and apply
with oc separately. Names, display names, network workspaces and other parameters
are preserved. The original JSON is the rollback copy.
"""
from __future__ import annotations

import copy
import json
import sys


def upgrade(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Expected a Kubernetes JSON object")
    items = document.get("items", []) if document.get("kind") == "List" else [document]
    updated = []
    for original in items:
        if original.get("kind") != "Template":
            continue
        runs = [obj for obj in original.get("objects", [])
                if obj.get("kind") == "PipelineRun"
                and obj.get("spec", {}).get("pipelineRef", {}).get("name") == "vm-provision"]
        if not runs:
            continue
        item = copy.deepcopy(original)
        namespace = item.get("metadata", {}).get("namespace")
        if not namespace:
            raise ValueError("Request Template has no execution namespace")
        fields = item.setdefault("parameters", [])
        matches = [p for p in fields if p.get("name") == "TARGET_NAMESPACE"]
        if len(matches) > 1:
            raise ValueError("Duplicate TARGET_NAMESPACE parameter")
        if not matches:
            fields.append({"name": "TARGET_NAMESPACE", "displayName": "Target VM namespace",
                "description": "Existing approved VM destination. Keep the top Namespace selector on " + namespace + ".",
                "value": namespace, "required": True})
        for obj in item["objects"]:
            if obj.get("kind") != "PipelineRun" or obj.get("spec", {}).get("pipelineRef", {}).get("name") != "vm-provision":
                continue
            params = obj["spec"].setdefault("params", [])
            existing = [p for p in params if p.get("name") == "target-namespace"]
            expected = {"name": "target-namespace", "value": "${TARGET_NAMESPACE}"}
            if existing and existing != [expected]:
                raise ValueError("Unexpected target-namespace binding; review the Template manually")
            if not existing:
                params.append(expected)
        # Retain resourceVersion for conflict detection. Strip server bookkeeping
        # and the old apply annotation, not user labels/annotations or workspaces.
        item.pop("status", None)
        for key in ("managedFields", "uid", "creationTimestamp", "generation"):
            item["metadata"].pop(key, None)
        item["metadata"].get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
        updated.append(item)
    if not updated:
        raise ValueError("No vm-provision PipelineRun request Templates found; nothing to apply")
    return {"apiVersion": "v1", "kind": "List", "items": updated}


if __name__ == "__main__":
    try:
        result = upgrade(json.load(sys.stdin))
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        print(f"Prepared {len(result['items'])} request Templates; no API calls made.", file=sys.stderr)
    except (ValueError, TypeError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
