.. _adr-0002:

=================================================
ADR-0002: One OpenAI-compatible provider contract
=================================================

Status
    Accepted (v0.1)

Context
-------

The agent must reach a real model. Vendors disagree on request shape, streaming
format, tool calling and authentication, so "support many providers" usually
means a growing pile of adapters.

Options considered
------------------

* **Native SDK per vendor** (OpenAI, Anthropic, Bedrock, Vertex, Mistral, ...)
  Best fidelity, but several message models, several streaming formats and
  several error taxonomies.
* **LiteLLM-style proxy** Richest support, but adds a service to operate and a
  new failure mode.
* **One OpenAI-compatible contract** One client, one message model, one tool
  format; most vendors already expose it.

Decision
--------

Implement a single :class:`~quantsmind_agent.llm.ModelProvider` and ship one
implementation, ``OpenAICompatProvider``, speaking ``POST /chat/completions``
(streaming and not) plus ``GET /models``.

The coverage is broad because the contract is an industry standard:

.. code-block:: text

    OpenAI, OpenRouter, Groq, Together, Fireworks, DeepSeek,
    vLLM, Ollama, LM Studio, llama.cpp, Azure OpenAI (compatible deployments)

A provider is constructed through one factory:

.. code-block:: python

    provider = build_provider(settings.provider, api_key=resolve_key)

so registering a native protocol later is a dictionary entry, not a change to the
Agent Core.

Consequences
------------

* The Agent Core is provider-independent: it sees ``ChatRequest`` and
  ``ChatResponse`` only.
* Adding a native provider (for example Anthropic Messages) means implementing
  ``complete``/``stream``/``list_models`` and translating to the same DTOs. The
  translation cost is paid once per vendor, not once per call site.
* Tool-calling fidelity is limited by the least capable compatible endpoint.
  Providers without tool support will not work for agentic use; this is a
  documented requirement rather than a silent failure.
* Some vendors are OpenAI-*shaped* rather than OpenAI-*compatible*: Azure needs
  a deployment-specific URL and an ``api-version`` query parameter, and
  Mistral-style endpoints rename some fields. The adapter surface is small
  enough to extend when a real need appears; nothing is pre-built speculatively.

Rejected alternative, restated
------------------------------

Wrapping every vendor natively in v0.1 would have doubled the provider code and
the test matrix before a single vertical slice worked. One contract first;
abstractions are in place for the rest.