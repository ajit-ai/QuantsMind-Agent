.. _user-settings:


========
Settings
========


Open **Settings…** from the sidebar or with ``Ctrl+K``.

Provider tab
============

=================  ==========================================================
Field              Notes
=================  ==========================================================
Provider preset    Fills the base URL and model for OpenAI, OpenRouter,
                     Groq and local servers (Ollama, LM Studio, llama.cpp).
Model              Free text, or pick from ``Available on endpoint`` when the
                     endpoint exposes ``GET /models``.
Base URL           Must be ``http://`` or ``https://``.
API key            Password field, never rendered after saving.
Clear stored key   Deletes the credential from the store.
Key from env var   Name of the environment variable to consult, e.g.
                     ``QM_API_KEY``.
Request timeout    Seconds per request (5-3600).
Max output tokens  Upper bound on the response length.
Temperature        0.0-2.0, shown as tenths.
=================  ==========================================================

Presets
-------

==========  ========================================
Preset      Endpoint
==========  ========================================
OpenAI      ``https://api.openai.com/v1``
OpenRouter  ``https://openrouter.ai/api/v1``
Groq        ``https://api.groq.com/openai/v1``
Local       ``http://localhost:11434/v1`` (Ollama)
Local       ``http://localhost:1234/v1`` (LM Studio)
Local       ``http://localhost:8080/v1`` (llama.cpp)
Custom      Editable starting point
==========  ========================================

For local servers, the hint under the form changes to *"This endpoint looks like
a local server, so no API key is required."*

Agent tab
=========

========================  ===================================
Setting                   Meaning
========================  ===================================
Max agent iterations      Model round-trips per turn (1-50)
Read tool file cap        Maximum bytes ``read_file`` returns
Conversation turns kept   History size in the prompt
Allow workspace reads     ``false`` makes reads fail closed
Extra agent instructions  Appended to the system prompt
Developer mode            Verbose logging
========================  ===================================

Raising *Max agent iterations* lets the agent chain more tool calls per request
at the cost of latency and tokens. The last iteration always withholds tools, so
the model must produce a written answer.

About tab
=========

Shows the version and the exact locations of the configuration file, the log
file and the credential store.

Applying changes
================

**Save** writes ``config.toml`` and rebuilds the live stack -- provider, tools,
context engine and agent -- without restarting the application. The activity
panel logs ``configuration updated``.

**Cancel** discards everything, including a key typed into the field.

Troubleshooting
===============

*The model returns 401/403*
    The key was rejected. Confirm the key and the base URL belong together: an
    OpenAI key against ``openrouter.ai`` will be refused.

*The model returns 404*
    Wrong model name, or the base URL is missing the ``/v1`` suffix. Use a
    preset and then change only the model.

*The agent answers immediately with an error about a missing key*
    The endpoint requires a credential but none is configured. Either paste one
    in Settings or export ``QM_API_KEY``.

*The agent ignores my project*
    No project is open. The transcript will say *Open a project folder first.*

Related
=======

* :doc:`../configuration`
* :doc:`../developer/troubleshooting`
