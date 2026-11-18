"""The secure authentication gateway (M3B-S1.1).

This is the place where a credential stops being "a string the provider will
use" and becomes "a string this application is willing to put on the wire to
this endpoint, for this provider, right now".

The sequence the gateway implements::

    provider identity resolution
        -> model authorization
        -> endpoint authorization
        -> credential binding validation
        -> secure credential retrieval
        -> HTTP request construction
        -> authorised TLS connection
        -> response sanitisation
        -> agent receives model output

Only the middle of that sequence lives here; the rest is the provider
adapter's job. What matters is the order: **nothing is retrieved and nothing
is attached until the endpoint has been authorised.**

Policy summary
--------------
``CLOUD`` providers
    HTTPS only, a public host only, and only the exact endpoint the provider
    identity is bound to. A cloud credential can never be sent to a loopback,
    private, link-local or metadata address, and never over plain HTTP.

``LOCAL`` providers
    Loopback only unless the operator explicitly allows a remote model
    server. No cloud credential is ever attached to a local endpoint: the
    credential identifiers are provider-bound, and the gateway refuses a
    credential that does not belong to the endpoint's own identity.

``CUSTOM`` endpoints
    Refused unless the operator has explicitly trusted custom endpoints *and*
    the destination is a public host over HTTPS. Speaking the OpenAI
    protocol is not a trust decision.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.credential_resolution import (
    CredentialResolution,
    CredentialResolutionService,
    CredentialSource,
    CredentialStatus,
)
from quantsmind_agent.security.credential_vault import CredentialVault
from quantsmind_agent.security.provider_identity import (
    CLOUD_PROVIDER_IDS,
    EndpointIdentity,
    ProviderIdentity,
    ProviderTrust,
    resolve_provider_identity,
)

__all__ = [
    "AuthGateway",
    "AuthenticationGatewayError",
    "AuthorizationDecision",
    "EndpointAuthorization",
]


class AuthenticationGatewayError(QuantsMindError):
    """An authentication or authorisation step refused to continue."""

    code = "authentication_gateway_error"


class AuthorizationDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class EndpointAuthorization:
    """The outcome of authorising one endpoint for one provider identity."""

    decision: AuthorizationDecision
    reason_code: str
    detail: str = ""
    provider_id: str = ""
    endpoint_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is AuthorizationDecision.ALLOWED

    def __bool__(self) -> bool:
        return self.allowed


#: Resolver signature: a host name to a list of address strings.
Resolver = Callable[[str], list[str]]


def _system_resolver(host: str) -> list[str]:
    """Resolve ``host`` with the system resolver, returning address strings."""

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def _embedded_ipv4(address: str) -> str | None:
    """The IPv4 address inside an IPv6 transition address, if there is one.

    ``::ffff:0:0/96`` (IPv4-mapped), ``64:ff9b::/96`` (NAT64) and
    ``2002::/16`` (6to4) all carry a public IPv4 address inside an IPv6 one.
    Python classifies the first two as *private*, which would make the DNS
    check refuse an ordinary public host that happens to publish an AAAA
    record -- so the embedded address is what is classified instead.
    """

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if parsed.version != 6:
        return None
    packed = parsed.packed
    if parsed in ipaddress.ip_network("::ffff:0:0/96"):
        return str(ipaddress.IPv4Address(packed[12:16]))
    if parsed in ipaddress.ip_network("64:ff9b::/96"):
        return str(ipaddress.IPv4Address(packed[12:16]))
    if parsed in ipaddress.ip_network("2002::/16"):
        return str(ipaddress.IPv4Address(packed[2:6]))
    return None


def _is_public_address(address: str) -> bool:
    """Whether an address is a routable public address."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        return _is_public_address(embedded)
    if parsed.is_loopback or parsed.is_unspecified:
        return False
    # ``is_global`` covers shared address space such as 100.64.0.0/10, which
    # ``is_private`` alone does not.
    return bool(parsed.is_global)


class AuthGateway:
    """Authorises endpoints and attaches credentials to them.

    Parameters
    ----------
    vault:
        Where credentials come from. The gateway is the only caller.
    audit:
        Optional audit log; every decision is recorded in it.
    resolver:
        Name resolution used for the DNS check. Injectable so the security
        tests never touch the network.
    trust_custom_endpoints:
        Whether a configured custom endpoint may receive a credential at all.
        Off by default: protocol compatibility is not a trust decision.
    allow_remote_local_models:
        Whether a model server on the LAN may be used. Off by default.
    check_dns:
        Whether the destination is resolved and checked before a credential is
        attached. On by default, and a resolution failure is a denial.
    """

    def __init__(
        self,
        *,
        vault: CredentialVault,
        audit: SecurityAuditLog | None = None,
        resolver: Resolver | None = None,
        trust_custom_endpoints: bool | Callable[[], bool] = False,
        allow_remote_local_models: bool | Callable[[], bool] = False,
        check_dns: bool = True,
        credential_service: CredentialResolutionService | None = None,
    ) -> None:
        self._vault = vault
        self._audit = audit
        self._resolver = resolver or _system_resolver
        self._trust_custom_endpoints = trust_custom_endpoints
        self._allow_remote_local_models = allow_remote_local_models
        self._check_dns = bool(check_dns)
        self._credentials = credential_service or CredentialResolutionService(vault=vault)

    def _custom_endpoints_trusted(self) -> bool:
        """Whether custom endpoints are trusted *right now*.

        The decision is a callable because it lives in the configuration, and
        the configuration can change while the application is running. A
        boolean would freeze the decision at construction time, and the
        operator's choice in the settings would then have no effect on the
        request path.
        """

        source = self._trust_custom_endpoints
        if callable(source):
            return bool(source())
        return bool(source)

    def _remote_local_models_allowed(self) -> bool:
        source = self._allow_remote_local_models
        if callable(source):
            return bool(source())
        return bool(source)

    # -- decisions --------------------------------------------------------
    def _deny(
        self,
        reason_code: str,
        detail: str,
        identity: ProviderIdentity,
        endpoint: EndpointIdentity,
        *,
        record: bool = True,
    ) -> EndpointAuthorization:
        decision = EndpointAuthorization(
            decision=AuthorizationDecision.DENIED,
            reason_code=reason_code,
            detail=detail,
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
        )
        if self._audit is not None and record:
            self._audit.record(
                AuditEventType.ENDPOINT_DENIED,
                provider_id=identity.provider_id,
                endpoint_id=endpoint.endpoint_id,
                decision="denied",
                reason_code=reason_code,
                detail=detail,
            )
            self._audit.record(
                AuditEventType.SECURITY_POLICY_DENIED,
                provider_id=identity.provider_id,
                endpoint_id=endpoint.endpoint_id,
                decision="denied",
                reason_code=reason_code,
            )
        return decision

    def _allow(
        self,
        reason_code: str,
        detail: str,
        identity: ProviderIdentity,
        endpoint: EndpointIdentity,
        *,
        record: bool = True,
    ) -> EndpointAuthorization:
        decision = EndpointAuthorization(
            decision=AuthorizationDecision.ALLOWED,
            reason_code=reason_code,
            detail=detail,
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
        )
        if self._audit is not None and record:
            self._audit.record(
                AuditEventType.ENDPOINT_AUTHORIZED,
                provider_id=identity.provider_id,
                endpoint_id=endpoint.endpoint_id,
                decision="allowed",
                reason_code=reason_code,
                detail=detail,
            )
        return decision

    def _destination_is_public(self, endpoint: EndpointIdentity) -> tuple[bool, str]:
        """Whether the destination is a public host, and why not if it is not."""

        if endpoint.is_metadata:
            return False, "metadata_endpoint"
        if endpoint.is_loopback:
            return False, "loopback_destination"
        if endpoint.is_link_local:
            return False, "link_local_destination"
        if endpoint.is_private:
            return False, "private_destination"
        return True, ""

    def _dns_is_public(self, endpoint: EndpointIdentity) -> tuple[bool, str]:
        """Resolve the host and check every address it resolves to."""

        if not self._check_dns:
            return True, ""
        addresses = self._resolver(endpoint.host)
        if not addresses:
            return False, "dns_unresolved"
        for address in addresses:
            if not _is_public_address(address):
                return False, "dns_resolves_to_non_public"
        return True, ""

    def authorize(
        self,
        identity: ProviderIdentity,
        endpoint: EndpointIdentity,
        *,
        credential_bearing: bool = True,
        record: bool = True,
    ) -> EndpointAuthorization:
        """Decide whether this provider identity may use this endpoint.

        The decision is about the *pair*. A cloud identity may not be pointed
        at a local address, and a local identity may not be pointed at a
        public one, no matter how the configuration was written.

        ``record=False`` asks for the decision without writing an audit
        record. The settings screen uses it to *show* the classification,
        which is not a request and must not fill the audit trail with a
        decision per repaint.
        """

        # A rejected binding is refused before any endpoint policy: the
        # configuration asked for a provider that does not own this endpoint,
        # and the credential slot it names must never be read.
        if identity.identity_mismatch:
            if self._audit is not None and record:
                self._audit.record(
                    AuditEventType.PROVIDER_IDENTITY_MISMATCH,
                    provider_id=identity.provider_id,
                    endpoint_id=endpoint.endpoint_id,
                    decision="denied",
                    reason_code="provider_endpoint_mismatch",
                    detail=(
                        "the requested provider identity "
                        f"{identity.requested_provider_id!r} does not own this endpoint"
                    ),
                )
            return self._deny(
                "provider_endpoint_mismatch",
                "the configured provider identity does not belong to this endpoint",
                identity,
                endpoint,
                record=record,
            )

        if identity.trust is ProviderTrust.CLOUD:
            if endpoint.scheme != "https":
                return self._deny(
                    "scheme_downgrade",
                    "a cloud credential may only be sent over https",
                    identity,
                    endpoint,
                    record=record,
                )
            public, reason = self._destination_is_public(endpoint)
            if not public:
                return self._deny(
                    reason,
                    "the destination is not a public host",
                    identity,
                    endpoint,
                    record=record,
                )
            known = self._known_endpoint(identity.provider_id)
            if known is not None and endpoint.endpoint_id != known:
                return self._deny(
                    "endpoint_not_bound",
                    "the endpoint is not the one this provider identity is bound to",
                    identity,
                    endpoint,
                    record=record,
                )
            if known is None:
                return self._deny(
                    "provider_not_recognised",
                    "this provider identity is not a known hosted service",
                    identity,
                    endpoint,
                    record=record,
                )
            dns_ok, dns_reason = self._dns_is_public(endpoint)
            if not dns_ok:
                return self._deny(
                    dns_reason,
                    "the destination did not resolve to a public address",
                    identity,
                    endpoint,
                    record=record,
                )
            return self._allow(
                "cloud_endpoint_authorized",
                "the endpoint is bound to this provider",
                identity,
                endpoint,
                record=record,
            )

        if identity.trust is ProviderTrust.LOCAL:
            if identity.provider_id in CLOUD_PROVIDER_IDS:
                return self._deny(
                    "identity_endpoint_mismatch",
                    "a hosted provider identity cannot be used for a local endpoint",
                    identity,
                    endpoint,
                    record=record,
                )
            if identity.endpoint_id and identity.endpoint_id != endpoint.endpoint_id:
                return self._deny(
                    "endpoint_identity_changed",
                    "the identity was resolved for a different endpoint",
                    identity,
                    endpoint,
                    record=record,
                )
            if endpoint.is_loopback:
                return self._allow(
                    "local_endpoint_authorized",
                    "the model server is on this machine",
                    identity,
                    endpoint,
                    record=record,
                )
            if self._remote_local_models_allowed() and endpoint.is_private:
                return self._allow(
                    "remote_local_endpoint_authorized",
                    "the remote model server was explicitly authorised",
                    identity,
                    endpoint,
                    record=record,
                )
            return self._deny(
                "remote_local_endpoint",
                "a local model server must be on this machine unless it is explicitly authorised",
                identity,
                endpoint,
                record=record,
            )

        # CUSTOM
        # The scheme and the destination class are checked first, so the
        # reason code names the most fundamental problem: a private address
        # is refused as a private address, not merely as an untrusted one.
        if endpoint.scheme != "https":
            return self._deny(
                "scheme_downgrade",
                "a custom endpoint credential may only be sent over https",
                identity,
                endpoint,
                record=record,
            )
        public, reason = self._destination_is_public(endpoint)
        if not public:
            return self._deny(
                reason,
                "the destination is not a public host",
                identity,
                endpoint,
                record=record,
            )
        expected = resolve_provider_identity(endpoint).provider_id
        if identity.provider_id != expected:
            return self._deny(
                "custom_identity_not_endpoint_bound",
                "a custom endpoint must use the identity derived from that endpoint",
                identity,
                endpoint,
                record=record,
            )
        if identity.endpoint_id and identity.endpoint_id != endpoint.endpoint_id:
            return self._deny(
                "endpoint_identity_changed",
                "the identity was resolved for a different endpoint",
                identity,
                endpoint,
                record=record,
            )
        if not self._custom_endpoints_trusted():
            return self._deny(
                "custom_endpoint_not_trusted",
                "a custom endpoint needs an explicit trust decision before it may be used",
                identity,
                endpoint,
                record=record,
            )
        dns_ok, dns_reason = self._dns_is_public(endpoint)
        if not dns_ok:
            return self._deny(
                dns_reason,
                "the destination did not resolve to a public address",
                identity,
                endpoint,
                record=record,
            )
        return self._allow(
            "custom_endpoint_authorized",
            "the custom endpoint was explicitly trusted",
            identity,
            endpoint,
            record=record,
        )

    @staticmethod
    def _known_endpoint(provider_id: str) -> str | None:
        """The canonical endpoint a known hosted service is bound to."""

        from quantsmind_agent.security.provider_identity import KNOWN_PROVIDERS

        for endpoint_id, (known_id, _label) in KNOWN_PROVIDERS.items():
            if known_id == provider_id:
                return endpoint_id
        return None

    # -- credential attachment --------------------------------------------
    def resolve_credential(
        self,
        identity: ProviderIdentity,
        endpoint: EndpointIdentity,
        *,
        required: bool = True,
        configured_env_name: str = "QM_API_KEY",
        record: bool = True,
        authorize: bool = True,
    ) -> CredentialResolution:
        """The one authoritative decision for this identity and endpoint.

        Authorisation happens before the credential is read, so a denied
        endpoint never causes a secret to be loaded. ``record=False`` and
        ``authorize=False`` are for diagnostics that must not write audit
        records or perform a network name resolution.
        """

        authorization_status = "not_checked"
        if authorize:
            decision = self.authorize(identity, endpoint, record=record)
            authorization_status = "allowed" if decision.allowed else "denied"
            if not decision.allowed:
                resolution = CredentialResolution(
                    provider_id=identity.provider_id,
                    credential_id=identity.credential_id,
                    status=CredentialStatus.REJECTED,
                    storage_status=self._credentials.storage_status,
                    reason_code=decision.reason_code,
                    detail=decision.detail,
                    authorization_status=authorization_status,
                    required=required,
                )
                if record:
                    self._record_authentication(resolution)
                return resolution

        resolution = self._credentials.resolve(
            identity,
            required=required,
            configured_env_name=configured_env_name,
            authorization_status=authorization_status,
        )
        if record:
            self._record_authentication(resolution)
        return resolution

    def _record_authentication(self, resolution: CredentialResolution) -> None:
        if self._audit is None:
            return
        succeeded = resolution.usable and resolution.status is not CredentialStatus.NOT_REQUIRED
        event = (
            AuditEventType.AUTHENTICATION_SUCCEEDED
            if succeeded
            else AuditEventType.AUTHENTICATION_FAILED
        )
        reason = resolution.reason_code
        if succeeded and resolution.source is not CredentialSource.NONE:
            reason = f"{resolution.reason_code}:{resolution.source.value}"
        self._audit.record(
            event,
            provider_id=resolution.provider_id,
            credential_id=resolution.credential_id,
            decision="allowed" if succeeded else "denied",
            reason_code=reason,
            detail=resolution.detail,
        )

    @staticmethod
    def _credential_user_message(resolution: CredentialResolution) -> str:
        if resolution.reason_code == "provider_endpoint_mismatch":
            return (
                "The provider identity in Settings does not belong to the configured "
                "model address, so no API key was sent. Choose the provider again or "
                "correct the address."
            )
        if resolution.reason_code == "credential_disabled":
            return (
                "The API key for this provider is disabled. Open Settings and enable it, "
                "or enter a new key."
            )
        if resolution.reason_code == "credential_revoked":
            return "The API key for this provider was removed. Open Settings and enter a new key."
        if resolution.status is CredentialStatus.UNAVAILABLE:
            return (
                "The credential store could not be read, so no API key was sent. Check the "
                "OS credential service and try again."
            )
        if resolution.status is CredentialStatus.REJECTED:
            return (
                "The configured model endpoint was refused by the security policy, so no "
                "API key was sent. Check the provider address in Settings."
            )
        return (
            "No API key is stored for this provider. Open Settings and enter the key for "
            "this provider."
        )

    def credential_for(
        self,
        identity: ProviderIdentity,
        endpoint: EndpointIdentity,
        *,
        required: bool = True,
        configured_env_name: str = "QM_API_KEY",
    ) -> str | None:
        """The credential to attach to this endpoint, or ``None``.

        The endpoint is authorised *before* the credential is read, so a
        denied endpoint never causes a secret to be loaded into memory, let
        alone placed on the wire. The value comes only from the authoritative
        resolution; the provider never learns why a key is absent beyond the
        safe reason code.
        """

        resolution = self.resolve_credential(
            identity,
            endpoint,
            required=required,
            configured_env_name=configured_env_name,
        )
        if not resolution.usable:
            raise AuthenticationGatewayError(
                f"no usable credential for provider {identity.provider_id}: "
                f"{resolution.reason_code}",
                user_message=self._credential_user_message(resolution),
                details=resolution.diagnostic(),
            )
        if resolution.status is CredentialStatus.NOT_REQUIRED:
            return None
        return resolution.value

    def headers_for(
        self,
        identity: ProviderIdentity,
        endpoint: EndpointIdentity,
        *,
        required: bool = True,
    ) -> dict[str, str]:
        """The request headers, with the credential attached if there is one.

        A local endpoint with no credential gets no ``Authorization`` header
        at all, rather than an empty one.
        """

        value = self.credential_for(identity, endpoint, required=required)
        if not value:
            return {}
        return {"Authorization": f"Bearer {value}"}

    def client_options(
        self,
        endpoint: EndpointIdentity,
        *,
        credential_bearing: bool = True,
        allow_proxy: bool = False,
    ) -> dict[str, object]:
        """Transport options that constrain where a credential can travel.

        ``follow_redirects`` is off: a redirect would move the request, and
        with it the prompt and the ``Authorization`` header, to a host the
        policy never authorised. The provider adapter reports a redirect as an
        error instead of following it.

        ``verify`` stays on. Certificate validation is never disabled to make
        a test pass.

        ``trust_env`` is off for a credential-bearing route, so a proxy
        variable in the environment cannot silently become the destination of
        a request that carries an API key. A user who needs a proxy has to
        say so explicitly.
        """

        return {
            "follow_redirects": False,
            "verify": True,
            "trust_env": bool(allow_proxy) and not credential_bearing,
        }
