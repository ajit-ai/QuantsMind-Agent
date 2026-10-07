"""Model providers shipped with QuantsMind-Agent.

v0.1 registers exactly one: :class:`OpenAICompatProvider`. The registry keeps
provider construction behind a single factory so that adding a native
Anthropic or Azure provider later needs no Agent Core change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from quantsmind_agent.config import ProviderSettings
from quantsmind_agent.errors import LLMConfigurationError
from quantsmind_agent.llm import ModelProvider
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "OPENAI_COMPAT",
    "OpenAICompatProvider",
    "build_provider",
    "registered_provider_names",
]

logger = get_logger(__name__)

OPENAI_COMPAT = "openai_compatible"

#: name -> factory. Extend this mapping to add providers.
_FACTORIES: dict[str, Callable[..., ModelProvider]] = {
    OPENAI_COMPAT: OpenAICompatProvider,
}


def registered_provider_names() -> list[str]:
    """Names accepted by :func:`build_provider`."""

    return sorted(_FACTORIES)


def build_provider(
    settings: ProviderSettings,
    *,
    api_key: str | Callable[[], str | None] | None = None,
    transport: Any | None = None,
) -> ModelProvider:
    """Instantiate the provider described by ``settings``.

    Parameters
    ----------
    api_key:
        The credential, or a callable returning it. A callable is preferred so
        the key is read at request time and never cached on the provider.
    transport:
        Optional ``httpx`` transport; used by tests to stub the network.
    """

    name = settings.name.strip() or OPENAI_COMPAT
    factory = _FACTORIES.get(name)
    if factory is None:
        raise LLMConfigurationError(
            f"Unknown provider: {name!r}",
            user_message=(
                f"Provider '{name}' is not available in v0.1. "
                f"Available providers: {', '.join(registered_provider_names())}."
            ),
            details={"provider": name, "available": registered_provider_names()},
        )

    return factory(
        settings.base_url,
        api_key=api_key,
        organization=settings.organization,
        timeout_seconds=settings.timeout_seconds,
        requires_api_key=settings.requires_api_key(),
        transport=transport,
    )
