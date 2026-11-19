"""
Hardened HTTP fetching for REST readers and probes.

``fetch(url, headers, guard_private=...)`` differs from a plain ``requests.get`` in two ways:

1. **Redirects are followed manually** (at most ``max_redirects`` hops). On a redirect to a
   different origin (scheme, host, or port), *all caller-supplied headers are dropped*,
   so secrets in custom headers (``X-API-Key: $TOKEN``) never reach a third-party host.
   ``requests`` itself only strips ``Authorization``. Redirects to non-HTTP(S) schemes
   are refused.

2. **Optional connection-time address guard** (``guard_private=True``). The peer address
   of every TCP connection is checked *after* connecting, on the actual socket. A host
   that resolved to a public IP when the probe was registered but resolves to
   ``127.0.0.1`` or ``169.254.169.254`` now (DNS rebinding) is refused, and so is every
   redirect hop. Environment proxies are ignored in this mode so the checked peer is
   the real destination.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

from .security import is_internal_ip

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_REDIRECTS = 5


class UnsafeRedirectError(requests.exceptions.RequestException):
    """A redirect pointed to a non-HTTP(S) URL."""


def _peer_allowed(ip: str) -> bool:
    """Policy hook: may a guarded connection talk to ``ip``? (Patched in tests.)"""
    return not is_internal_ip(ip)


class _GuardMixin:
    def _new_conn(self):  # type: ignore[override]
        sock = super()._new_conn()  # type: ignore[misc]
        try:
            peer_ip = sock.getpeername()[0]
        except OSError:
            peer_ip = None
        if peer_ip is None or not _peer_allowed(peer_ip):
            sock.close()
            raise NewConnectionError(
                self,  # type: ignore[arg-type]
                f"Blocked connection to internal address {peer_ip} for host {self.host!r} "  # type: ignore[attr-defined]
                f"(probes registered through MCP may only reach public hosts)",
            )
        return sock


class _GuardedHTTPConnection(_GuardMixin, HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardMixin, HTTPSConnection):
    pass


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _GuardedHTTPPool,
            "https": _GuardedHTTPSPool,
        }

    def proxy_manager_for(self, *args, **kwargs):  # never tunnel through a proxy
        raise requests.exceptions.InvalidProxyURL("Proxies are disabled for guarded requests.")


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    default = 443 if parts.scheme == "https" else 80
    return parts.scheme, (parts.hostname or "").lower(), parts.port or default


def _session(guard_private: bool) -> requests.Session:
    session = requests.Session()
    if guard_private:
        session.trust_env = False          # ignore HTTP(S)_PROXY / netrc
        adapter = _GuardedAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session


def fetch(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    guard_private: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> requests.Response:
    """GET ``url`` with safe redirect handling and an optional internal-address guard."""
    current_headers = dict(headers or {})
    with _session(guard_private) as session:
        for _ in range(max_redirects + 1):
            response = session.get(
                url, headers=current_headers, timeout=timeout, allow_redirects=False,
            )
            if not response.is_redirect:
                return response
            target = urljoin(url, response.headers["Location"])
            if urlsplit(target).scheme not in ("http", "https"):
                raise UnsafeRedirectError(f"Refusing redirect to non-HTTP(S) URL: {target}")
            if _origin(target) != _origin(url):
                current_headers = {}       # never forward caller headers cross-origin
            response.close()
            url = target
    raise requests.exceptions.TooManyRedirects(f"Exceeded {max_redirects} redirects.")
