#!/usr/bin/env python3
"""Request VM networking through AAP; standard library only, no Infoblox calls.

Commands: check-ssh-key (offline), check-aap (GET only), or no arguments to
allocate. Write the existing allocation file only after a verified AAP result.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
INPUTS = {"request_id", "vm_name", "mac_address", "network_profile"}
ED25519_PREFIX = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)


class AllocationError(RuntimeError):
    """Only curated, credential-safe messages are printed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)


def required(key):
    value = os.environ.get(key, "")
    if not value or "REPLACE_ME" in value.upper():
        raise AllocationError(f"Missing or placeholder setting: {key}")
    return value


def describe(exc):
    return str(exc) if isinstance(exc, AllocationError) else type(exc).__name__


def validate_ssh_key(value):
    if (not isinstance(value, str) or len(value) > 1024 or value != value.strip()
            or not all(c.isprintable() for c in value)):
        raise AllocationError("SSH public key must be one printable line")
    parts = value.split(" ", 2)
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise AllocationError("SSH ed25519 public key required")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise AllocationError("SSH public key is not valid base64") from None
    if (base64.b64encode(blob).decode() != parts[1] or len(blob) != 51
            or not blob.startswith(ED25519_PREFIX)):
        raise AllocationError("SSH public key is not a valid ed25519 key")
    return value


def positive_int(value, label, maximum):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)) or int(value) > maximum:
        raise AllocationError(f"Invalid {label}")
    return int(value)


def settings():
    base = required("AAP_API_BASE_URL").rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(c.isspace() for c in base)
            or parts.path not in ("/api/v2", "/api/controller/v2")):
        raise AllocationError("AAP_API_BASE_URL must be HTTPS ending /api/v2 or /api/controller/v2")
    token = required("AAP_TOKEN")
    if any(c.isspace() for c in token) or not token.isascii():
        raise AllocationError("AAP_TOKEN must be a single ASCII token")
    profile = required("NETWORK_PROFILE")
    if not IDENTIFIER.fullmatch(profile):
        raise AllocationError("Invalid NETWORK_PROFILE identifier")
    return dict(base=base, token=token, profile=profile,
                template=positive_int(required("AAP_JOB_TEMPLATE_ID"), "AAP_JOB_TEMPLATE_ID", 2147483647),
                timeout=positive_int(os.environ.get("AAP_JOB_TIMEOUT_SECONDS", "600"), "AAP_JOB_TIMEOUT_SECONDS", 3600),
                interval=positive_int(os.environ.get("AAP_POLL_INTERVAL_SECONDS", "5"), "AAP_POLL_INTERVAL_SECONDS", 60))


def request(config, method, path, data=None, timeout=30):
    # Paths are constructed here from integer IDs, never followed from API responses.
    url = config["base"] + "/" + path
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Authorization": "Bearer " + config["token"]}
    body = None if data is None else json.dumps(data).encode()
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers, data=body,
                                               method=method), timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise AllocationError("AAP response exceeds size limit")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise AllocationError("AAP response must be a JSON object")
            return result
    except urllib.error.HTTPError as exc:
        raise AllocationError(f"AAP {method} failed: HTTP {exc.code}") from None
    except OSError:
        raise AllocationError(f"AAP {method} failed: network or TLS error") from None
    except (ValueError, TypeError):
        raise AllocationError("AAP returned invalid JSON") from None


def preflight(config):
    """Read-only template checks. The real playbook must still validate the inputs."""
    path = f'job_templates/{config["template"]}/'
    template = request(config, "GET", path)
    if template.get("job_type") != "run" or template.get("job_slice_count", 1) != 1:
        raise AllocationError("AAP template must be a regular run job, without slicing")
    if template.get("ask_variables_on_launch") is not True:
        if template.get("survey_enabled") is not True:
            raise AllocationError("Enable AAP Prompt on launch for variables or a survey for all four inputs")
        spec = request(config, "GET", path + "survey_spec/").get("spec")
        if not isinstance(spec, list) or not INPUTS <= {x.get("variable") for x in spec if isinstance(x, dict)}:
            raise AllocationError("AAP survey must accept all four contract inputs")
    launch = request(config, "GET", path + "launch/")
    if any(launch.get(key) for key in ("passwords_needed_to_start", "credential_needed_to_start", "inventory_needed_to_start")):
        raise AllocationError("AAP job requires launch credentials or inventory; configure unattended execution")
    needed = launch.get("variables_needed_to_start", [])
    if not isinstance(needed, list) or any(x not in INPUTS for x in needed):
        raise AllocationError("AAP job requires extra inputs outside the agreed contract")


def ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.ip_address(value)
        if (address.version != 4 or address.is_unspecified or address.is_multicast
                or address.is_loopback or address == ipaddress.ip_address("255.255.255.255")):
            raise ValueError
        return address
    except (ValueError, TypeError):
        raise AllocationError("Invalid IPv4 address in AAP result") from None


def validate_result(result, sent):
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise AllocationError("Missing or unsupported artifacts.vm_network schema_version")
    for key in INPUTS:
        if result.get(key) != sent[key]:
            raise AllocationError(f"AAP result does not match request field: {key}")
    cidr = result.get("ip_cidr")
    try:
        if not isinstance(cidr, str) or not re.fullmatch(r"[0-9.]+/[0-9]{1,2}", cidr):
            raise ValueError
        iface = ipaddress.ip_interface(cidr)
        if iface.version != 4 or not 1 <= iface.network.prefixlen <= 30:
            raise ValueError
    except (ValueError, TypeError):
        raise AllocationError("AAP ip_cidr must contain a usable IPv4 address and /1 through /30 prefix") from None
    address = ipv4(str(iface.ip))
    gateway = ipv4(result.get("gateway"))
    edges = (iface.network.network_address, iface.network.broadcast_address)
    if address in edges or gateway in edges or gateway not in iface.network or gateway == address:
        raise AllocationError("AAP result has an invalid guest address or gateway")
    servers = result.get("dns_servers")
    if not isinstance(servers, list) or not servers:
        raise AllocationError("AAP dns_servers must be a nonempty list")
    dns = [str(ipv4(server)) for server in servers]
    domain = result.get("domain")
    if not isinstance(domain, str) or len(domain) > 253 or not DOMAIN.fullmatch(domain):
        raise AllocationError("Invalid domain in AAP result")
    fqdn = sent["vm_name"] + "." + domain
    if len(fqdn) > 253 or result.get("fqdn") != fqdn:
        raise AllocationError("AAP fqdn must match vm_name plus domain")
    return [sent["vm_name"], str(iface), sent["mac_address"], str(gateway), ",".join(dns), domain]


def event(journal, **data):
    line = json.dumps(data, sort_keys=True)
    journal.write(line + "\n")
    journal.flush()
    print("AAP_REQUEST " + line, flush=True)


def allocate(config, sent, journal):
    # Persist identity BEFORE POST. Any failed/uncertain launch must be reconciled,
    # not replayed. No automatic cancellation; a timed-out job may still be running.
    event(journal, event="prepared", template_id=config["template"], **sent)
    deadline = time.monotonic() + config["timeout"]
    launched = request(config, "POST", f'job_templates/{config["template"]}/launch/',
                       {"extra_vars": sent}, timeout=min(30, config["timeout"]))
    job_id = launched.get("job", launched.get("id"))
    if type(job_id) is not int or job_id <= 0:
        raise AllocationError("Launch may have succeeded but returned no valid job ID; reconcile by request_id")
    event(journal, event="launched", job_id=job_id, **sent)
    if launched.get("ignored_fields"):
        raise AllocationError("AAP ignored launch inputs; job may already be running; reconcile before retrying")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AllocationError(f"AAP job {job_id} timed out locally; it was NOT canceled")
        job = request(config, "GET", f"jobs/{job_id}/", timeout=min(30, remaining))
        if job.get("id") != job_id or job.get("job_template") != config["template"]:
            raise AllocationError("AAP job identity does not match the launched job/template")
        if time.monotonic() >= deadline:
            raise AllocationError(f"AAP job {job_id} exceeded the local deadline; reconcile its status")
        status = job.get("status")
        if status == "successful":
            artifacts = job.get("artifacts")
            record = validate_result(artifacts.get("vm_network") if isinstance(artifacts, dict) else None, sent)
            event(journal, event="validated", job_id=job_id, ip_cidr=record[1], **sent)
            return record
        if status in ("failed", "error", "canceled"):
            raise AllocationError(f"AAP job {job_id} ended {status}; allocation may exist")
        if status not in ("new", "pending", "waiting", "running"):
            raise AllocationError(f"AAP job {job_id} returned an unknown status")
        time.sleep(min(config["interval"], max(0, deadline - time.monotonic())))


def main(argv=()):
    if list(argv) == ["check-ssh-key"]:
        validate_ssh_key(required("REQ_SSH_KEY"))
        print("SSH public key format OK")
        return
    if argv and list(argv) != ["check-aap"]:
        raise AllocationError("Unknown command; use check-ssh-key, check-aap, or no arguments")
    config = settings()
    if argv:
        preflight(config)
        print("AAP read-only template checks passed; no job launched")
        return
    names = Path(required("VM_NAMES_FILE")).read_text().splitlines()
    if not names or len(names) > 5 or len(set(names)) != len(names) or not all(HOST.fullmatch(x) for x in names):
        raise AllocationError("Invalid, empty or duplicate VM names")
    run_id = required("REQUEST_ID")
    if not IDENTIFIER.fullmatch(run_id):
        raise AllocationError("REQUEST_ID must be the safe PipelineRun UID")
    output = Path(required("ALLOCATIONS_FILE"))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Files are exclusive and never resumed automatically. Keep both plus job logs.
    with output.open("x", encoding="utf-8") as receipt, output.with_suffix(".requests.jsonl").open("x", encoding="utf-8") as journal:
        preflight(config)
        seen = set()
        for name in names:
            sent = dict(request_id=f"{run_id}/{name}/lan", vm_name=name,
                        mac_address="02:" + ":".join(f"{x:02x}" for x in secrets.token_bytes(5)),
                        network_profile=config["profile"])
            try:
                record = allocate(config, sent, journal)
                address = str(ipaddress.ip_interface(record[1]).ip)
                if address in seen:
                    raise AllocationError("AAP returned a duplicate address in this batch; reconcile both jobs")
                seen.add(address)
                receipt.write("|".join(record) + "\n")
                receipt.flush()
            except Exception as exc:
                raise AllocationError(
                    f"Request stopped at {name}; inspect AAP_REQUEST logs and journal before retrying. "
                    f"No jobs canceled or records deleted. {describe(exc)}") from None


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print("ERROR:", describe(exc), file=sys.stderr)
        sys.exit(1)
