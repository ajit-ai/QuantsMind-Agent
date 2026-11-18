"""Adversarial tests for the pinned transport (M3B-S1.4, S14-G10 to S14-G12).

These tests are platform-independent: the transport is pure Python and has no
Windows dependency. They are kept in their own module so they are collected and
executed on Linux, macOS and Windows instead of being skipped together with the
Windows-only sandbox boundary.

Every test is offline and deterministic: the name resolver is injected, so no
DNS query is made and no test contacts a live provider.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from quantsmind_agent.providers.pinned_transport import (
    AddressNotAllowed,
    PinnedAddressTransport,
)


class _RecordingTransport(httpx.BaseTransport):
    """A delegate that records what the pinned transport produced."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})


class TestPinnedTransport:
    """S14-G10, S14-G11, S14-G12: the address used is the one that was validated."""

    def _transport(self, resolver: Any, **kwargs: Any) -> Any:
        delegate = _RecordingTransport()
        transport = PinnedAddressTransport(
            resolver=resolver, transport_factory=lambda: delegate, **kwargs
        )
        return transport, delegate

    def test_a_public_destination_is_accepted(self) -> None:
        transport, _delegate = self._transport(lambda host: ["93.184.216.34"])
        assert transport.validate("api.example.test") == ["93.184.216.34"]

    @pytest.mark.parametrize(
        "address",
        ["10.0.0.5", "192.168.1.20", "127.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0"],
    )
    def test_a_disallowed_destination_is_rejected(self, address: str) -> None:
        transport, _delegate = self._transport(lambda host: [address])
        with pytest.raises(AddressNotAllowed):
            transport.validate("api.example.test")

    def test_a_mixed_answer_is_rejected(self) -> None:
        transport, _delegate = self._transport(lambda host: ["93.184.216.34", "10.0.0.5"])
        with pytest.raises(AddressNotAllowed):
            transport.validate("api.example.test")

    def test_a_failed_resolution_is_rejected(self) -> None:
        transport, _delegate = self._transport(lambda host: [])
        with pytest.raises(AddressNotAllowed):
            transport.validate("api.example.test")

    def test_loopback_is_allowed_only_when_authorised(self) -> None:
        strict, _a = self._transport(lambda host: ["127.0.0.1"])
        with pytest.raises(AddressNotAllowed):
            strict.validate("localhost.test")
        local, _b = self._transport(lambda host: ["127.0.0.1"], allow_loopback=True)
        assert local.validate("localhost.test") == ["127.0.0.1"]

    def test_the_connection_uses_the_validated_address(self) -> None:
        transport, delegate = self._transport(lambda host: ["93.184.216.34"])
        client = httpx.Client(transport=transport)
        response = client.get("https://api.example.test/v1/models")
        assert response.status_code == 200
        sent = delegate.requests[-1]
        assert sent.url.host == "93.184.216.34"
        assert sent.headers["host"] == "api.example.test"

    def test_the_tls_identity_is_the_original_hostname(self) -> None:
        transport, delegate = self._transport(lambda host: ["93.184.216.34"])
        client = httpx.Client(transport=transport)
        client.get("https://api.example.test/v1/models")
        sent = delegate.requests[-1]
        assert sent.extensions.get("sni_hostname") == "api.example.test"

    def test_a_changed_answer_replaces_the_connection(self) -> None:
        answers = [["93.184.216.34"], ["93.184.216.35"]]
        transport, delegate = self._transport(lambda host: answers.pop(0))
        client = httpx.Client(transport=transport)
        client.get("https://api.example.test/v1/models")
        client.get("https://api.example.test/v1/models")
        assert delegate.requests[-1].url.host == "93.184.216.35"

    def test_an_unvalidated_address_is_never_connected_to(self) -> None:
        transport, delegate = self._transport(lambda host: ["10.0.0.5"])
        client = httpx.Client(transport=transport)
        with pytest.raises(httpx.TransportError):
            client.get("https://api.example.test/v1/models")
        assert delegate.requests == []

    def test_a_secret_never_appears_in_a_transport_error(self) -> None:
        transport, _delegate = self._transport(lambda host: ["10.0.0.5"])
        client = httpx.Client(transport=transport)
        with pytest.raises(httpx.TransportError) as info:
            client.get("https://api.example.test/v1/models")
        assert "sk-" not in str(info.value)

    def test_redirects_are_not_followed(self) -> None:
        class _Redirecting(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(302, headers={"location": "https://evil.test/v1"})

        transport = PinnedAddressTransport(
            resolver=lambda host: ["93.184.216.34"],
            transport_factory=_Redirecting,
        )
        client = httpx.Client(transport=transport)
        response = client.get("https://api.example.test/v1/models")
        assert response.status_code == 302
        assert response.headers["location"] == "https://evil.test/v1"
