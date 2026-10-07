.. _configuration:


=============
Configuration
=============


Configuration has three parts, and they are deliberately separate:

1. **Settings** (not secret) -- ``config.toml`` in the per-user config directory.
2. **Credentials** (secret) -- the Windows Credential Manager, or
   ``secrets.json`` when a keyring is unavailable.
3. **Session state** (in memory) -- workspace and conversation, never written
   to disk.

A fourth, optional source overrides both: environment variables.

Provider settings
=================

.. code-block:: toml

    [provider]
    name = "openai_compatible"
    base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    timeout_seconds = 120.0
    max_output_tokens = 4096
    temperature = 0.2
    api_key_env = "QM_API_KEY"
    # organization = "org-..."      # optional, sent as OpenAI-Organization

=====================  ==========================================================
Field                  Meaning
=====================  ==========================================================
``name``               Provider implementation. v0.1 ships ``openai_compatible``.
``base_url``           Must be ``http(s)``. A trailing ``/`` is stripped.
``model``              Model identifier sent to the endpoint.
``timeout_seconds``    Per-request timeout (5-3600 s).
``max_output_tokens``  Upper bound on the response (128-200000).
``temperature``        Sampling temperature, 0.0-2.0.
``api_key_env``        Environment variable consulted before the secret store.
``organization``       Optional organisation header.
=====================  ==========================================================

``requires_api_key()`` returns ``False`` for ``localhost``, ``127.0.0.1``,
``0.0.0.0``, ``host.docker.internal`` and ``[::1]`` endpoints, so local servers
such as Ollama work with no credential at all.

Agent settings
==============

.. code-block:: toml

    [agent]
    max_iterations = 8
    max_context_files = 8
    max_file_bytes = 256000
    auto_approve_reads = true
    system_prompt = ""
    history_turns = 12

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Field
     - Meaning
   * - ``max_iterations``
     - Model round-trips per user turn (1-50). The last iteration offers no
       tools, forcing a written answer.
   * - ``max_context_files``
     - Reserved for the v0.2 retrieval index.
   * - ``max_file_bytes``
     - Hard cap on what ``read_file`` returns.
   * - ``auto_approve_reads``
     - ``false`` turns workspace reads into ``ASK`` decisions, which degrade to
       ``DENY`` without an approver.
   * - ``system_prompt``
     - Extra operator instructions appended to the base prompt.
   * - ``history_turns``
     - Recent user/assistant exchanges kept in the prompt.

Logging settings
================

.. code-block:: toml

    [logging]
    level = "INFO"
    file_enabled = true
    max_bytes = 2000000
    backup_count = 3

API key handling
================

Resolution order:

1. the environment variable named by ``api_key_env`` (default ``QM_API_KEY``);
2. the secret store.

Rules the implementation guarantees:

* the key is never written to ``config.toml``;
* the key is never written to the log file -- a redaction filter scrubs
  ``Authorization``/``api_key`` patterns from every record;
* the Settings dialog never renders a stored key; the field is empty on open
  and shows ``sk-******op`` as masked status text;
* ``tests/unit/test_secrets_paths.py`` asserts the masking contract, and
  ``tests/gui/test_main_window.py`` asserts no key reaches the transcript.

Clearing a key: Settings -> **Clear stored key**, or delete ``secrets.json``.

Environment variables
=====================

===================  ===========================================
Variable             Effect
===================  ===========================================
``QM_API_KEY``       API key (highest precedence)
``QM_CONFIG_DIR``    Override the configuration directory
``QM_LOG_DIR``       Override the log directory
``QT_QPA_PLATFORM``  Set to ``offscreen`` for headless GUI tests
===================  ===========================================

.. code-block:: powershell

    $env:QM_API_KEY = "sk-..."
    quantsmind-agent

Using the GUI is still the recommended way to configure a key, because it goes
through the credential store rather than the process environment.

Resetting everything
====================

.. code-block:: powershell

    Remove-Item -Recurse "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent"

Then launch the application and re-enter your settings. Nothing else is stored
on disk: there is no database and no telemetry.
