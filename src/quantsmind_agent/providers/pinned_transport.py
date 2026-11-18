"""The address-pinned provider transport (M3B-S1.4, Capability B).

The M3B-S1.1 gateway validates a destination *before* the request is
built. The gap this module closes is the one between that check and the
socket: a name can resolve to one address during validation and to a
different address when the client connects.

The transport therefore:

1. resolves the intended host itself, with an injectable resolver;
2. validates **every** address the name resolved to, against the same
   policy the gateway uses;
3. connects to one of the **validated** addresses, not to the name;
4. keeps the original hostname for **SNI** and for the **Host** authority,
   so the certificate is still checked against the name the operator
   configured;
5. re-validates on every request, and replaces the connection when the
   address set changes -- a pooled connection can never outlive the
   validation that authorised it;
6. refuses redirects and proxy inheritance, like the rest of the provider.

It does **not** disable TLS verification, and it does not present an
address as the TLS identity.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from quantsmind_agent.security.provider_identity import classify_address

__all__ = [
    "AddressNotAllowed",
    "PinnedAddressTransport",
    "Resolver",
    "default_resolver",
]

#: Resolver signature: a host name to a list of address strings.
Resolver = Callable[[str], list[str]]

#: The address classes a public cloud destination may use.
ALLOWED_CLASSES = frozenset({"public"})

#: The address classes an explicitly authorised local destination may use.
ALLOWED_LOCAL_CLASSES = frozenset({"public", "loopback"})


class AddressNotAllowed(httpx.TransportError):
    """The destination could not be validated, so no connection was made."""


def default_resolver(host: str) -> list[str]:
    """Resolve a host with the system resolver."""

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def _default_transport() -> httpx.BaseTransport:
    """The transport a pinned connection uses.

    TLS verification is on, redirects are refused, and the environment is not
    consulted, so a proxy variable cannot become the destination.
    """

    # A transport never follows a redirect; the *client* decides, and the
    # provider's client sets follow_redirects=False.
    return httpx.HTTPTransport(verify=True, trust_env=False)


class PinnedAddressTransport(httpx.HTTPTransport):
    """An ``httpx`` transport that connects to a validated address.

    Parameters
    ----------
    resolver:
        The name resolver. Injectable, so the tests never touch DNS.
    allow_loopback:
        Whether loopback addresses may be used. Off by default: a *public*
        cloud provider must never be reached over loopback. A local provider
        is authorised explicitly by the caller.
    """

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        allow_loopback: bool = False,
        transport_factory: Callable[[], httpx.BaseTransport] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._resolver = resolver or default_resolver
        self._allow_loopback = bool(allow_loopback)
        self._transport_factory = transport_factory or _default_transport
        #: host -> (address, transport). One pool per host, so a connection
        #: can never be shared between two names that resolve to one address.
        self._pinned: dict[str, tuple[str, httpx.BaseTransport]] = {}

    # -- policy -----------------------------------------------------------
    def _allowed_classes(self) -> frozenset[str]:
        return ALLOWED_LOCAL_CLASSES if self._allow_loopback else ALLOWED_CLASSES

    def validate(self, host: str) -> list[str]:
        """Resolve ``host`` and return the addresses that may be used.

        Every address the name resolved to must be allowed. A *mixed* answer
        -- one public and one private -- is refused, because the client
        cannot know which one it would have reached.
        """

        addresses = self._resolver(host)
        if not addresses:
            raise AddressNotAllowed(f"the destination {host!r} did not resolve")
        allowed = self._allowed_classes()
        for address in addresses:
            if classify_address(address) not in allowed:
                raise AddressNotAllowed(
                    f"the destination {host!r} resolved to a disallowed address"
                )
        return addresses

    # -- transport --------------------------------------------------------
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        host = parts.hostname or ""
        if not host:
            raise AddressNotAllowed("the request has no host")

        addresses = self.validate(host)
        address = addresses[0]
        pinned = self._pinned.get(host)
        if pinned is None or pinned[0] != address:
            # The address set changed, or this host has no pool yet: the old
            # connection (if any) is closed, so a pooled connection can never
            # outlive the validation that authorised it.
            if pinned is not None:
                pinned[1].close()
            transport = self._transport_factory()
            self._pinned[host] = (address, transport)
        else:
            transport = pinned[1]

        original_authority = request.url.netloc.decode("ascii")
        original_host = parts.hostname
        port = parts.port
        netloc = f"[{address}]" if ":" in address else address
        if port is not None:
            netloc = f"{netloc}:{port}"

        url = request.url.copy_with(host=netloc)
        headers = request.headers.copy()
        headers["Host"] = original_authority
        pinned_request = httpx.Request(
            method=request.method,
            url=url,
            headers=headers,
            content=request.content,
            extensions={**request.extensions, "sni_hostname": original_host},
        )
        return transport.handle_request(pinned_request)

    def close(self) -> None:
        for _address, transport in self._pinned.values():
            transport.close()
        self._pinned.clear()
        super().close()
