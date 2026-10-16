#!/usr/bin/env python3
"""Infoblox NIOS WAPI allocation for VM requests; Python standard library only.

Writes a recovery receipt, not VM YAML. No retries, rollback, or guest credentials.
Use check-ssh-key for offline SSH validation without contacting IPAM.
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
import urllib.error
import urllib.parse
import urllib.request

HOST = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
WAPI_VERSION = re.compile(r"^v?([0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?)$")
HOST_REF = re.compile(r"^record:host/[A-Za-z0-9_.:/-]{1,300}$")
ED25519_PREFIX = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)


class AllocationError(RuntimeError):
    """Curated, credential-safe error text."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward authentication to a redirected host.


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


def ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.ip_address(value)
        if address.version != 4:
            raise ValueError
        return address
    except (ValueError, TypeError):
        raise AllocationError("Invalid IPv4 address") from None


def network_settings(prefix, gateway, dns, domain, start, end, dhcp_start="", dhcp_end=""):
    try:
        net = ipaddress.ip_network(prefix, strict=True)
    except (ValueError, TypeError):
        raise AllocationError("Invalid network prefix") from None
    if net.version != 4 or net.num_addresses < 8:
        raise AllocationError("A usable IPv4 network is required")
    first, last, gw = ipv4(start), ipv4(end), ipv4(gateway)
    if not net.network_address < first <= last < net.broadcast_address:
        raise AllocationError("Static pool is outside network or includes reserved addresses")
    if first == net.network_address + 1 and last == net.broadcast_address - 1:
        raise AllocationError("Refusing whole-subnet allocation")
    if (not isinstance(dns, str) or not isinstance(domain, str) or len(domain) > 253
            or not DOMAIN.fullmatch(domain)):
        raise AllocationError("Invalid DNS configuration")
    servers = [ipv4(x.strip()) for x in dns.split(",") if x.strip()]
    edges = (net.network_address, net.broadcast_address)
    if not servers or any(x in edges for x in servers):
        raise AllocationError("Invalid DNS server address")
    if gw not in net or gw in edges:
        raise AllocationError("Gateway must be a usable address in the network")
    if any(first <= x <= last for x in [gw, *servers]):
        raise AllocationError("Static pool overlaps gateway or DNS")
    if dhcp_start or dhcp_end:
        if not dhcp_start or not dhcp_end:
            raise AllocationError("Incomplete DHCP range")
        ds, de = ipv4(dhcp_start), ipv4(dhcp_end)
        if (not net.network_address < ds <= de < net.broadcast_address
                or ds <= last and de >= first):
            raise AllocationError("Static pool overlaps DHCP or DHCP is invalid")
        if any(ds <= x <= de for x in [gw, *servers]):
            raise AllocationError("DHCP pool overlaps gateway or DNS")
    return dict(net=net, start=str(first), end=str(last), gateway=str(gw),
                dns=",".join(map(str, servers)), domain=domain, dhcp=bool(dhcp_start))


def request(base, method, path, data=None, *, user, password):
    url = base.rstrip("/") + "/" + path.lstrip("/")
    headers = {
        "Accept": "application/json", "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
    }
    body = None if data is None else json.dumps(data).encode()
    try:
        with OPENER.open(urllib.request.Request(url, headers=headers, data=body,
                                               method=method), timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise AllocationError(f"IPAM {method} failed: HTTP {exc.code}") from None
    except OSError:
        raise AllocationError(f"IPAM {method} failed: network or TLS error") from None
    except (ValueError, TypeError):
        raise AllocationError("Invalid JSON response from IPAM") from None


def infoblox_preflight(names):
    version = WAPI_VERSION.fullmatch(required("INFOBLOX_WAPI_VERSION"))
    if not version:
        raise AllocationError("INFOBLOX_WAPI_VERSION must look like 2.12 or v2.12")
    origin = required("IPAM_URL").rstrip("/")
    parts = urllib.parse.urlsplit(origin)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path or any(c.isspace() for c in origin)):
        raise AllocationError("IPAM_URL must be an HTTPS origin without path or credentials")
    base = origin + "/wapi/v" + version.group(1)
    user, password = required("INFOBLOX_USERNAME"), required("INFOBLOX_PASSWORD")
    view, network_view = required("INFOBLOX_DNS_VIEW"), required("INFOBLOX_NETWORK_VIEW")
    if any(not s.isprintable() or s != s.strip() for s in (view, network_view)) or "," in network_view:
        raise AllocationError("Invalid Infoblox view name")
    settings = network_settings(
        required("INFOBLOX_NETWORK"), required("GATEWAY"), required("DNS_SERVERS"),
        required("DOMAIN"), required("STATIC_START"), required("STATIC_END"),
        os.environ.get("DHCP_START", ""), os.environ.get("DHCP_END", ""))
    disabled = os.environ.get("DHCP_DISABLED", "false")
    if disabled not in ("true", "false") or settings["dhcp"] and disabled == "true":
        raise AllocationError("DHCP_DISABLED must be true or false and must not conflict with a DHCP range")
    if not settings["dhcp"] and disabled != "true":
        raise AllocationError("Set DHCP_START and DHCP_END, or explicitly set DHCP_DISABLED=true")
    settings["network_view"] = network_view

    def call(method, path, body=None):
        return request(base, method, path, body, user=user, password=password)

    for name in names:
        hostname = name + "." + settings["domain"]
        if len(hostname) > 253:
            raise AllocationError("VM FQDN is too long")
        query = urllib.parse.urlencode({"name": hostname, "view": view})
        found = call("GET", "record:host?" + query)
        if not isinstance(found, list):
            raise AllocationError("Unexpected Infoblox host search response shape")
        if found:
            raise AllocationError(f"Host {hostname} already exists in Infoblox")
    return call, settings, view


def infoblox_allocate(call, settings, view, name, mac):
    fqdn = name + "." + settings["domain"]
    network_view = settings["network_view"]
    # Reserve through one server-side host insertion. No client-side free-IP scan.
    ref = call("POST", "record:host", {
        "name": fqdn, "view": view, "network_view": network_view,
        "configure_for_dns": True,
        "ipv4addrs": [{
            "ipv4addr": f'func:nextavailableip:{settings["start"]}-{settings["end"]},{network_view}'
        }],
    })
    if not isinstance(ref, str) or not HOST_REF.fullmatch(ref):
        raise AllocationError(f"Host {fqdn} may exist; inspect the DNS/network view before retrying")
    print(f"RESERVED host={fqdn} mac={mac} dns-view={view} network-view={network_view}", flush=True)
    obj = call("GET", ref + "?_return_fields=ipv4addrs")
    addrs = obj.get("ipv4addrs") if isinstance(obj, dict) else None
    if not isinstance(addrs, list) or len(addrs) != 1 or not isinstance(addrs[0], dict):
        raise AllocationError(f"Host {fqdn} exists but its address response is invalid; reconcile manually")
    address = ipv4(addrs[0].get("ipv4addr"))
    if not ipv4(settings["start"]) <= address <= ipv4(settings["end"]):
        raise AllocationError(f"Host {fqdn} exists but its address is outside the pool; reconcile manually")
    # DNS host record only: no DHCP fixed address or MAC registration in Infoblox.
    return f'{address}/{settings["net"].prefixlen}'


def main(argv=()):
    if list(argv) == ["check-ssh-key"]:
        validate_ssh_key(required("REQ_SSH_KEY"))
        print("SSH public key format OK")
        return
    if argv:
        raise AllocationError("Unknown command; use no arguments or check-ssh-key")
    names = Path(required("VM_NAMES_FILE")).read_text().splitlines()
    if not names or len(names) > 5 or len(set(names)) != len(names) or not all(HOST.fullmatch(x) for x in names):
        raise AllocationError("Invalid, empty or duplicate VM names")
    output = Path(required("ALLOCATIONS_FILE"))
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a second invocation from discarding a recovery receipt.
    with output.open("x", encoding="utf-8") as receipt:
        call, settings, view = infoblox_preflight(names)
        seen = set()
        for name in names:
            mac = "02:" + ":".join(f"{x:02x}" for x in secrets.token_bytes(5))
            try:
                cidr = infoblox_allocate(call, settings, view, name, mac)
                address = str(ipaddress.ip_interface(cidr).ip)
                if address in seen:
                    raise AllocationError(f"Duplicate allocated address for {name}; inspect both host records")
                seen.add(address)
                record = [name, cidr, mac, settings["gateway"], settings["dns"], settings["domain"]]
                receipt.write("|".join(record) + "\n")
                receipt.flush()
                print(f"Reserved {name} -> {cidr}", flush=True)
            except Exception as exc:
                raise AllocationError(
                    f"Request stopped at {name}; prior reservations remain. "
                    f"Inspect the receipt and Infoblox before retrying. {describe(exc)}") from None


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print("ERROR:", describe(exc), file=sys.stderr)
        sys.exit(1)
