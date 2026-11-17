"""
Security helpers shared by the registry, MCP server, and agent.

- ``validate_name``      — guards every user/LLM-supplied name that becomes part of a
                            file path (schema, table, consumer names) against path traversal.
- ``validate_var_name``  — enforces the documented ``{{var}}`` name format.
- ``check_env_allowed``  — restricts which environment variables an LLM-registered
                            checker may reference (prevents secret exfiltration).
- ``check_url_safe``     — rejects non-HTTP(S) URLs and hosts that resolve to private,
                            loopback, link-local, or otherwise internal addresses (SSRF).
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

# Names that become file names: letters, digits, '_', '-', '.', must not start with
# '.' or '-', no path separators, no '..' sequences, bounded length.
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,127}$")
_VAR_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_ENV_REF_RE = re.compile(r"\$([A-Z_][A-Z0-9_]*)")

#: Env vars an LLM-registered checker may always reference.
MCP_ENV_PREFIX = "SHELFARD_"
#: Comma-separated extra env var names an LLM-registered checker may reference.
MCP_ENV_ALLOWLIST_VAR = "SHELFARD_MCP_ENV_ALLOWLIST"
#: Set to "1" to let LLM-registered checkers target private/loopback hosts.
MCP_ALLOW_PRIVATE_HOSTS_VAR = "SHELFARD_MCP_ALLOW_PRIVATE_HOSTS"


def validate_name(value: str, kind: str = "name") -> Optional[str]:
    """Return an error message if ``value`` is not safe to use as a file name, else None."""
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value) or ".." in value:
        return (
            f"Invalid {kind} {value!r}: use 1-128 characters from [A-Za-z0-9_.-], "
            f"not starting with '.' or '-', with no path separators or '..'."
        )
    return None


def validate_var_name(value: str) -> Optional[str]:
    """Return an error message if ``value`` is not a valid template variable name."""
    if not isinstance(value, str) or not _VAR_NAME_RE.fullmatch(value):
        return f"Invalid template variable name {value!r}: must match [a-zA-Z_][a-zA-Z0-9_]*."
    return None


def ensure_within(root: Path, path: Path) -> Path:
    """Raise ValueError unless ``path`` resolves to a location inside ``root``."""
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"Refusing to access path outside the registry: {path}")
    return path


def extract_env_refs(*templates: str) -> set[str]:
    """Return every ``$VAR_NAME`` referenced in the given strings."""
    refs: set[str] = set()
    for t in templates:
        if t:
            refs.update(_ENV_REF_RE.findall(t))
    return refs


def check_env_allowed(env_names: list[str]) -> Optional[str]:
    """
    Return an error if any env var is not allowed for LLM-registered checkers.

    Allowed: names prefixed with ``SHELFARD_`` or listed in ``SHELFARD_MCP_ENV_ALLOWLIST``.
    """
    allowlist = {
        n.strip() for n in os.environ.get(MCP_ENV_ALLOWLIST_VAR, "").split(",") if n.strip()
    }
    denied = sorted(
        n for n in env_names
        if not (isinstance(n, str) and (n.startswith(MCP_ENV_PREFIX) or n in allowlist))
    )
    if denied:
        return (
            f"Env vars not allowed for checkers registered through MCP: {', '.join(denied)}. "
            f"Use names prefixed with '{MCP_ENV_PREFIX}', add them to {MCP_ENV_ALLOWLIST_VAR}, "
            f"or register the checker yourself with 'shelfard checker register'."
        )
    return None


def _is_internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def check_url_safe(url: str, *, allow_private: Optional[bool] = None) -> Optional[str]:
    """
    Return an error if ``url`` is not an http(s) URL to a public host, else None.

    ``allow_private`` defaults to the ``SHELFARD_MCP_ALLOW_PRIVATE_HOSTS`` env var.
    Every address the host resolves to must be public.
    """
    try:
        parts = urlsplit(url)
    except ValueError as e:
        return f"Invalid URL {url!r}: {e}"
    if parts.scheme not in ("http", "https"):
        return f"URL scheme must be http or https, got {parts.scheme or 'none'!r}."
    host = parts.hostname
    if not host:
        return f"URL has no host: {url!r}"

    if allow_private is None:
        allow_private = os.environ.get(MCP_ALLOW_PRIVATE_HOSTS_VAR) == "1"
    if allow_private:
        return None

    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        return f"Cannot resolve host {host!r}: {e}"

    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if _is_internal_ip(ip):
            return (
                f"Host {host!r} resolves to internal address {ip}. Checkers registered "
                f"through MCP may only target public hosts; set "
                f"{MCP_ALLOW_PRIVATE_HOSTS_VAR}=1 to allow private/loopback hosts."
            )
    return None
