"""Provider identity and endpoint identity (M3B-S1.1).

Why this module exists
----------------------
The application used to derive the credential slot from ``provider.name``,
which selects the *protocol factory* and is therefore the constant
``openai_compatible`` for every hosted service. One slot was shared by
OpenAI, OpenRouter, Groq and every custom endpoint, so a credential stored
for one service would be sent to another the moment the base URL changed.

This module separates three things that used to be one string:

``ProviderIdentity``
    *Which service the credential belongs to.* Derived from the endpoint, from
    an explicit configuration binding, or from a stable endpoint hash -- never
    from a user-editable display name alone.

``EndpointIdentity``
    *Where the request physically goes.* Canonicalised once, so that
    ``https://API.OpenAI.com:443/v1/`` and ``https://api.openai.com/v1`` are
    the same endpoint, and two different spellings of one host are not
    confused.

The rule the whole module exists to enforce: **a credential is bound to an
endpoint, not to a name.** A base URL that merely *looks* like a known
provider is not that provider.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

__all__ = [
    "CLOUD_PROVIDER_IDS",
    "EndpointIdentity",
    "EndpointPolicyError",
    "ProviderIdentity",
    "ProviderTrust",
    "canonicalize_endpoint",
    "is_local_hostname",
    "rebind_explicit_provider_id",
    "resolve_provider_identity",
]


class EndpointPolicyError(ValueError):
    """An endpoint could not be canonicalised, so it cannot be authorised.

    Raising rather than returning a best guess is deliberate: an endpoint the
    application cannot parse is an endpoint the application must not send a
    credential to.
    """


class ProviderTrust(StrEnum):
    """How far a provider may be trusted with a credential and a destination."""

    CLOUD = "cloud"
    """A hosted service that holds a paid account and a real API key."""

    LOCAL = "local"
    """A model server on this machine, or an explicitly allowed LAN host."""

    CUSTOM = "custom"
    """A third-party endpoint the operator configured explicitly."""


#: Hostnames that always mean "this machine".
_LOCAL_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)

#: A hostname is a DNS name: letters, digits, hyphen and dots only. Anything
#: else is refused rather than normalised, because two spellings of one host
#: are exactly the ambiguity this module exists to remove.
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)

#: Cloud metadata endpoints. These hand out credentials to anything that asks.
_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, DigitalOcean
        "169.254.170.2",  # AWS ECS task metadata
        "fd00:ec2::254",  # AWS IMDSv6
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
    }
)


def is_local_hostname(host: str) -> bool:
    """Whether ``host`` is a name that always resolves to this machine."""

    text = (host or "").strip().lower().rstrip(".")
    if not text:
        return False
    if text in _LOCAL_HOSTNAMES:
        return True
    return text.endswith(".localhost")


def _classify_ip(address: str) -> str:
    """Classify a literal address: loopback, private, link-local or public."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "invalid"
    if parsed.is_loopback or parsed.is_unspecified:
        return "loopback"
    if parsed.is_link_local:
        return "link_local"
    if parsed.is_private or parsed.is_reserved or parsed.is_multicast:
        return "private"
    return "public"


def classify_address(address: str) -> str:
    """Classify a literal address: ``public``, ``loopback``, ``private``,
    ``link_local`` or ``invalid``.

    The public entry point, so the transport and the gateway agree on what
    "public" means instead of each having their own idea.
    """

    return _classify_ip(address)


@dataclass(frozen=True, slots=True)
class EndpointIdentity:
    """A canonical, comparable description of one destination."""

    scheme: str
    host: str
    port: int | None
    path: str

    @property
    def url_host(self) -> str:
        """The host as it must appear in a URL (IPv6 literals bracketed)."""

        return f"[{self.host}]" if ":" in self.host else self.host

    @property
    def endpoint_id(self) -> str:
        """A stable, canonical string for this endpoint.

        The same endpoint always produces the same id, whatever spelling the
        configuration used, so it can be used as a binding key and logged
        without disclosing anything.
        """

        default = 443 if self.scheme == "https" else 80
        port = "" if self.port in (None, default) else f":{self.port}"
        path = self.path if self.path != "/" else ""
        return f"{self.scheme}://{self.url_host}{port}{path}"

    @property
    def host_port(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return f"{self.url_host}:{self.port or default}"

    @property
    def is_loopback(self) -> bool:
        if is_local_hostname(self.host):
            return True
        return _classify_ip(self.host) == "loopback"

    @property
    def is_private(self) -> bool:
        return _classify_ip(self.host) == "private"

    @property
    def is_link_local(self) -> bool:
        return _classify_ip(self.host) == "link_local"

    @property
    def is_metadata(self) -> bool:
        if self.host in _METADATA_ADDRESSES:
            return True
        return _classify_ip(self.host) == "link_local"

    @property
    def is_public_host(self) -> bool:
        return not (self.is_loopback or self.is_private or self.is_link_local)

    def describe(self) -> str:
        """A log-safe description. Never includes credentials."""

        return self.endpoint_id


def canonicalize_endpoint(base_url: str) -> EndpointIdentity:
    """Parse and canonicalise ``base_url``, or refuse it.

    The checks are the ones that stop a credential from being sent somewhere
    the operator did not mean: user-info in the URL, a non-HTTP scheme, an
    invalid port, an ambiguous host, or a host that is neither a hostname nor
    a valid address.
    """

    raw = (base_url or "").strip()
    if not raw:
        raise EndpointPolicyError("the endpoint is empty")
    if "://" not in raw:
        raise EndpointPolicyError(
            f"the endpoint {raw!r} has no scheme; it must start with http:// or https://"
        )

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise EndpointPolicyError(f"the endpoint {raw!r} could not be parsed: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise EndpointPolicyError(
            f"the endpoint scheme {scheme!r} is not supported; only http and https are"
        )

    netloc = parts.netloc
    if "@" in netloc:
        raise EndpointPolicyError(
            "the endpoint carries user information; credentials belong in the credential "
            "store, not in a URL"
        )
    if "%" in netloc:
        raise EndpointPolicyError(
            "the endpoint carries a percent-encoded host; that spelling is ambiguous"
        )

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EndpointPolicyError("the endpoint has no host")

    try:
        port = parts.port
    except ValueError as exc:
        raise EndpointPolicyError(
            f"the endpoint port {parts.netloc!r} is not a valid port number"
        ) from exc
    if port is not None and not (1 <= port <= 65535):
        raise EndpointPolicyError(f"the endpoint port {port} is out of range")

    # A host is either a valid IP literal or a strict DNS name. Not both, and
    # not something in between.
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        if not _HOSTNAME_RE.match(host):
            raise EndpointPolicyError(
                f"the endpoint host {host!r} is not a valid hostname or IP address"
            ) from None

    if parts.query or parts.fragment:
        raise EndpointPolicyError(
            "the endpoint carries a query string or fragment; credentials and other "
            "sensitive parameters must not travel in the base URL"
        )

    path = parts.path or "/"
    if not path.startswith("/"):
        raise EndpointPolicyError(f"the endpoint path {path!r} is not absolute")
    if ".." in path.split("/"):
        raise EndpointPolicyError(f"the endpoint path {path!r} contains a parent-directory segment")
    # One spelling for one path: ``/v1/`` and ``/v1`` are the same endpoint.
    # The root stays ``/`` so that the endpoint id stays well-formed.
    if path != "/":
        path = path.rstrip("/") or "/"

    return EndpointIdentity(scheme=scheme, host=host, port=port, path=path)


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Which service a credential belongs to, and how far it may be trusted.

    ``requested_provider_id`` records what the configuration asked for, and
    ``identity_mismatch`` says whether that request was refused because it did
    not belong to the canonical endpoint. A mismatched identity keeps an
    endpoint-derived id so a credential cannot be addressed through the old
    name; the gateway refuses it outright.
    """

    provider_id: str
    profile: str = "default"
    trust: ProviderTrust = ProviderTrust.CLOUD
    display_name: str = ""
    endpoint_id: str = ""
    requested_provider_id: str = ""
    identity_mismatch: bool = False

    @property
    def credential_id(self) -> str:
        """The credential-store key for this provider identity.

        Provider-bound by construction: two services never share a slot, and a
        local server never shares one with a cloud service.
        """

        return f"provider:{self.provider_id}:{self.profile}:api_key"

    @property
    def is_local(self) -> bool:
        return self.trust is ProviderTrust.LOCAL

    def describe(self) -> str:
        """A log-safe description: identities, never values."""

        return f"{self.provider_id}/{self.profile}"


#: Known hosted services, keyed by their canonical endpoint. The key is the
#: canonical endpoint, so a configuration that merely *resembles* one of these
#: (a different port, a different path, a look-alike host) does not match.
KNOWN_PROVIDERS: dict[str, tuple[str, str]] = {
    "https://api.openai.com/v1": ("openai", "OpenAI"),
    "https://openrouter.ai/api/v1": ("openrouter", "OpenRouter"),
    "https://api.groq.com/openai/v1": ("groq", "Groq"),
    "https://api.deepseek.com/v1": ("deepseek", "DeepSeek"),
    "https://api.minimax.chat/v1": ("minimax", "MiniMax"),
    "https://api.together.xyz/v1": ("together", "Together"),
    "https://api.fireworks.ai/inference/v1": ("fireworks", "Fireworks"),
}

#: The provider ids that only ever mean a hosted service. They must never be
#: reused for a local or custom endpoint, because doing so would address a
#: cloud credential slot.
CLOUD_PROVIDER_IDS = frozenset(provider_id for provider_id, _label in KNOWN_PROVIDERS.values())

#: Local model servers, keyed by ``host:port``. Each is unauthenticated by
#: default and must never receive a cloud credential.
LOCAL_PROVIDERS: dict[str, str] = {
    "localhost:11434": "ollama",
    "127.0.0.1:11434": "ollama",
    "localhost:1234": "lmstudio",
    "127.0.0.1:1234": "lmstudio",
    "localhost:8080": "llamacpp",
    "127.0.0.1:8080": "llamacpp",
    "localhost:8000": "taksh",
    "127.0.0.1:8000": "taksh",
    "localhost:8081": "vllm",
    "127.0.0.1:8081": "vllm",
}


def _endpoint_hash(endpoint: EndpointIdentity) -> str:
    """A short, stable identifier for an endpoint that is not a known one."""

    digest = hashlib.sha256(endpoint.endpoint_id.encode("utf-8")).hexdigest()
    return digest[:12]


def resolve_provider_identity(
    endpoint: EndpointIdentity,
    *,
    explicit_provider_id: str | None = None,
    display_name: str = "",
) -> ProviderIdentity:
    """Work out which service ``endpoint`` belongs to.

    Resolution order:

    1. an explicit ``provider_id`` from the configuration, which is the only
       way an operator can bind a credential to a service that is not in the
       known list;
    2. the known hosted service whose canonical endpoint matches exactly;
    3. a known local model server, matched on ``host:port``;
    4. any other loopback endpoint, which is local but unnamed;
    5. anything else, which is a *custom* endpoint and therefore needs an
       explicit trust decision before a credential may be sent to it.

    The display name is never used to decide ownership. It is carried along
    for the user interface and nothing else.

    An explicit ``provider_id`` is validated against the canonical endpoint
    before it can name a credential slot:

    * a hosted provider id must point at exactly that provider's endpoint;
    * a public, non-known endpoint is always addressed by an id derived from
      that endpoint, never by a caller-supplied name;
    * a loopback or LAN endpoint may keep an explicit local name, but never a
      hosted provider's id.

    A refused binding is returned as ``identity_mismatch=True`` with a safe,
    endpoint-derived id; the gateway then denies the request rather than
    reading a credential under the requested name.
    """

    known = KNOWN_PROVIDERS.get(endpoint.endpoint_id)

    if explicit_provider_id:
        if known is not None:
            canonical_id, label = known
            if explicit_provider_id == canonical_id:
                return ProviderIdentity(
                    provider_id=canonical_id,
                    trust=ProviderTrust.CLOUD,
                    display_name=display_name or label,
                    endpoint_id=endpoint.endpoint_id,
                    requested_provider_id=explicit_provider_id,
                )
            # The operator bound a provider id to a different provider's
            # endpoint. Address the credential by the endpoint's real owner and
            # mark the binding invalid so it cannot be used.
            return ProviderIdentity(
                provider_id=canonical_id,
                trust=ProviderTrust.CLOUD,
                display_name=display_name or label,
                endpoint_id=endpoint.endpoint_id,
                requested_provider_id=explicit_provider_id,
                identity_mismatch=True,
            )

        if endpoint.is_loopback or endpoint.is_private:
            # An explicitly bound model server on this machine or the local
            # network. It is still local: a local model server does not become
            # a cloud service because it was given a name. A hosted provider's
            # id is not a local name, though.
            if explicit_provider_id in CLOUD_PROVIDER_IDS:
                return ProviderIdentity(
                    provider_id=f"local-{_endpoint_hash(endpoint)}",
                    trust=ProviderTrust.LOCAL,
                    display_name=display_name or "Local model server",
                    endpoint_id=endpoint.endpoint_id,
                    requested_provider_id=explicit_provider_id,
                    identity_mismatch=True,
                )
            return ProviderIdentity(
                provider_id=explicit_provider_id,
                trust=ProviderTrust.LOCAL,
                display_name=display_name,
                endpoint_id=endpoint.endpoint_id,
                requested_provider_id=explicit_provider_id,
            )

        # A public endpoint that is not a known service. Its credential slot
        # must be derived from the endpoint itself, so two custom endpoints can
        # never share one.
        derived = f"custom-{_endpoint_hash(endpoint)}"
        return ProviderIdentity(
            provider_id=derived,
            trust=ProviderTrust.CUSTOM,
            display_name=display_name or "Custom endpoint",
            endpoint_id=endpoint.endpoint_id,
            requested_provider_id=explicit_provider_id,
            identity_mismatch=explicit_provider_id != derived,
        )

    if known is not None:
        provider_id, label = known
        return ProviderIdentity(
            provider_id=provider_id,
            trust=ProviderTrust.CLOUD,
            display_name=display_name or label,
            endpoint_id=endpoint.endpoint_id,
        )

    local_name = LOCAL_PROVIDERS.get(endpoint.host_port)
    if local_name is not None:
        return ProviderIdentity(
            provider_id=local_name,
            trust=ProviderTrust.LOCAL,
            display_name=display_name or local_name,
            endpoint_id=endpoint.endpoint_id,
        )

    if endpoint.is_loopback:
        return ProviderIdentity(
            provider_id=f"local-{_endpoint_hash(endpoint)}",
            trust=ProviderTrust.LOCAL,
            display_name=display_name or "Local model server",
            endpoint_id=endpoint.endpoint_id,
        )

    return ProviderIdentity(
        provider_id=f"custom-{_endpoint_hash(endpoint)}",
        trust=ProviderTrust.CUSTOM,
        display_name=display_name or "Custom endpoint",
        endpoint_id=endpoint.endpoint_id,
    )


def rebind_explicit_provider_id(endpoint: EndpointIdentity, provider_id: str | None) -> str | None:
    """The explicit binding that is still valid for ``endpoint``, if any.

    Editing a base URL must not carry the old provider id with it: that is how
    a key for one service becomes a key for another. A known provider binding
    survives only while the endpoint is exactly that provider; a local name
    survives on a loopback or LAN endpoint; a public custom endpoint keeps no
    caller-supplied name, because its identity is derived from the endpoint.
    """

    if not provider_id:
        return None
    known = KNOWN_PROVIDERS.get(endpoint.endpoint_id)
    if known is not None:
        return provider_id if provider_id == known[0] else None
    if endpoint.is_loopback or endpoint.is_private:
        return None if provider_id in CLOUD_PROVIDER_IDS else provider_id
    return None
