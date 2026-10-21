"""Explicit host, origin, trusted-peer and CSRF checks for the private API."""

from dataclasses import dataclass
import ipaddress
from urllib.parse import urlsplit

from fastapi import Request


@dataclass(frozen=True)
class RequestSecurity:
    public_origin: str | None = None
    trusted_proxy_cidrs: tuple[str, ...] = ('127.0.0.1/32', '::1/128')

    def __post_init__(self):
        networks = tuple(ipaddress.ip_network(value, strict=False) for value in self.trusted_proxy_cidrs)
        object.__setattr__(self, '_networks', networks)
        if self.public_origin:
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path not in ('', '/')
                    or parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise ValueError('PUBLIC_ORIGIN')

    def trusted_peer(self, request: Request) -> bool:
        peer = request.client.host if request.client else ''
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(address in network for network in self._networks)

    def expected_origin(self, request: Request) -> str | None:
        if self.public_origin:
            parsed = urlsplit(self.public_origin)
            expected_host = parsed.netloc.lower()
            return self.public_origin.rstrip('/') if request.headers.get('host', '').lower() == expected_host else None
        host = request.headers.get('host', '')
        try:
            hostname = request.url.hostname or ''
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if hostname.lower() != 'localhost':
                return None
        else:
            if not address.is_loopback:
                return None
        return f'{request.url.scheme}://{host}'.rstrip('/')

    def allows(self, request: Request) -> bool:
        if not self.trusted_peer(request):
            return False
        expected = self.expected_origin(request)
        if expected is None:
            return False
        origin = request.headers.get('origin')
        if origin is not None and origin.rstrip('/') != expected:
            return False
        if request.method.upper() in {'POST', 'PUT', 'PATCH', 'DELETE'} and origin is None:
            return False
        return True
