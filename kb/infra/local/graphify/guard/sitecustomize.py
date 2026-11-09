"""Network guard for third-party tools run by the platform (graphify).

Loaded automatically by Python's `site` module when this directory is on PYTHONPATH of the child process.
Every non-loopback socket connection or DNS lookup is refused and recorded (one JSON line each) in the file
named by KB_NETGUARD_LOG, so the parent can prove the tool made no outbound attempt (CLAUDE.md rule 2).
"""

import ipaddress
import json
import os
import socket
import time

_LOG = os.environ.get("KB_NETGUARD_LOG")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", None, ""}


def _record(kind, target):
    if _LOG:
        try:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "kind": kind, "target": str(target)}) + "\n")
        except OSError:
            pass


def _is_loopback(host):
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _host_of(address):
    return address[0] if isinstance(address, tuple) else address


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host_of(address)):
        _record("connect", address)
        raise ConnectionRefusedError(f"kb netguard: outbound connection to {address!r} blocked")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(_host_of(address)):
        _record("connect", address)
        return 111  # ECONNREFUSED
    return _real_connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        _record("dns", host)
        raise socket.gaierror(f"kb netguard: DNS lookup for {host!r} blocked")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
os.environ["KB_NETGUARD_ACTIVE"] = "1"
