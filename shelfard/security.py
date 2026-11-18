"""
Security helpers shared by the registry, MCP server, and agent.

- ``validate_name``      — guards every user/LLM-supplied name that becomes part of a
                            file path (schema, table, consumer names) against path traversal.
- ``validate_var_name``  — enforces the documented ``{{var}}`` name format.
- ``check_env_allowed``  — restricts which environment variables an LLM-registered
                            checker may reference (prevents secret exfiltration).
- ``check_url_safe``     — rejects non-HTTP(S) URLs and hosts that resolve to private,
                            loopback, link-local, or otherwise internal addresses (SSRF).
- ``check_host_safe``    — the same host check, for non-HTTP targets (PostgreSQL).
- ``parse_pg_hosts`` / ``pin_pg_dsn`` — extract hosts from a libpq DSN and pin the
                            connection to a validated IP (``hostaddr``) to defeat DNS rebinding.
- ``query_template_error`` — rejects ``{{var}}`` templates inside SQL (injection guard).
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

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


def is_internal_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for private, loopback, link-local, reserved, multicast, or unspecified IPs."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


_is_internal_ip = is_internal_ip  # backward-compatible alias


def private_hosts_allowed() -> bool:
    """True if ``SHELFARD_MCP_ALLOW_PRIVATE_HOSTS=1`` is set."""
    return os.environ.get(MCP_ALLOW_PRIVATE_HOSTS_VAR) == "1"


def resolve_public_ips(host: str, port: int) -> tuple[list[str], Optional[str]]:
    """
    Resolve ``host`` and return (ips, error). ``error`` is set if resolution fails or
    any resolved address is internal — every address must be public.
    """
    if not host or host.startswith("/"):
        return [], f"Host {host or '(none)'!r} is a local socket, not a public network host."
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError) as e:
        return [], f"Cannot resolve host {host!r}: {e}"
    ips = list(dict.fromkeys(info[4][0].split("%", 1)[0] for info in infos))
    for ip in ips:
        if is_internal_ip(ip):
            return [], (
                f"Host {host!r} resolves to internal address {ip}. Checkers registered "
                f"through MCP may only target public hosts; set "
                f"{MCP_ALLOW_PRIVATE_HOSTS_VAR}=1 to allow private/loopback hosts."
            )
    return ips, None


def check_host_safe(host: str, port: int, *, allow_private: Optional[bool] = None) -> Optional[str]:
    """Return an error if ``host`` is not a public network host, else None."""
    if allow_private is None:
        allow_private = private_hosts_allowed()
    if allow_private:
        return None
    return resolve_public_ips(host, port)[1]


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

    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        return f"Invalid port in URL {url!r}: {e}"
    return check_host_safe(host, port, allow_private=allow_private)


# ── PostgreSQL DSN helpers ────────────────────────────────────────────────────

_KV_RE = re.compile(r"\s*([A-Za-z_]+)\s*=\s*('(?:[^'\\]|\\.)*'|\S*)")


def _parse_kv_dsn(dsn: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in _KV_RE.findall(dsn):
        if value.startswith("'") and value.endswith("'"):
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        out[key.lower()] = value
    return out


def parse_pg_hosts(dsn: str) -> list[tuple[str, int, Optional[str]]]:
    """
    Return ``[(host, port, hostaddr)]`` for a libpq DSN (URI or keyword form).

    No host means libpq's default (a local Unix socket), reported as ``("", 5432, None)``.
    """
    if dsn.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(dsn)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        params = {k.lower(): v for k, v in parse_qsl(parts.query)}
        host_specs = [h for h in netloc.split(",")] if netloc else []
        hosts: list[tuple[str, int]] = []
        for spec in host_specs:
            if spec.startswith("["):                      # [ipv6]:port
                h, _, rest = spec[1:].partition("]")
                port = rest.lstrip(":")
            else:
                h, _, port = spec.partition(":")
            hosts.append((h, int(port) if port.isdigit() else 5432))
        if "host" in params:
            hosts = [(h, int(params.get("port", 5432) or 5432)) for h in params["host"].split(",")]
        hostaddr = params.get("hostaddr")
    else:
        kv = _parse_kv_dsn(dsn)
        port = kv.get("port", "5432")
        hosts = [
            (h, int(port) if port.isdigit() else 5432)
            for h in kv.get("host", "").split(",") if h
        ]
        hostaddr = kv.get("hostaddr")
    if not hosts:
        hosts = [("", 5432)]
    return [(h, p, hostaddr) for h, p in hosts]


def check_pg_dsn_safe(dsn: str) -> tuple[Optional[str], Optional[str]]:
    """
    Validate a fully-resolved DSN for an MCP-registered checker.

    Returns ``(error, pinned_ip)``. The DSN must name exactly one public host (multi-host
    and Unix-socket DSNs are rejected). ``pinned_ip`` is the validated address to pass as
    ``hostaddr`` so libpq does not resolve the name again (DNS-rebinding guard); it is
    None when the DSN already sets ``hostaddr``.
    """
    try:
        hosts = parse_pg_hosts(dsn)
    except ValueError as e:
        return f"Cannot parse DSN: {e}", None
    if len(hosts) != 1:
        return "Multi-host DSNs are not allowed for checkers registered through MCP.", None
    host, port, hostaddr = hosts[0]
    if "$" in host or "{{" in host:
        return "The DSN host must be resolved (no unresolved $VAR or {{var}}).", None
    if hostaddr:
        try:
            internal = is_internal_ip(hostaddr)
        except ValueError:
            return f"Invalid hostaddr {hostaddr!r}.", None
        if internal:
            return f"hostaddr {hostaddr} is an internal address.", None
        return None, None
    ips, err = resolve_public_ips(host, port)
    if err:
        return err, None
    return None, ips[0]


def pin_pg_dsn(dsn: str, ip: str) -> str:
    """Append ``hostaddr=<ip>`` to a DSN so libpq connects to that IP (keeps host for TLS)."""
    if dsn.startswith(("postgresql://", "postgres://")):
        return dsn + ("&" if "?" in dsn else "?") + f"hostaddr={ip}"
    return f"{dsn} hostaddr={ip}"


# ── SQL templating guard ──────────────────────────────────────────────────────

def query_template_error(query: Optional[str]) -> Optional[str]:
    """Return an error if a SQL query contains ``{{var}}`` templates (SQL-injection guard)."""
    if query and "{{" in query:
        return (
            "Template variables ({{var}}) are not allowed in SQL queries — a stored "
            "variable could inject SQL. Write the query literally; use {{var}} only in the DSN."
        )
    return None
