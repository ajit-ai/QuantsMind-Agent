.. _configuration:


=============
Configuration
=============


Configuration has four parts, and they are deliberately separate:

1. **Settings** (not secret) -- ``config.toml`` in the per-user config directory.
2. **Credentials** (secret) -- the operating-system credential store. A
   ``secrets.json`` left by an earlier version remains readable for an explicit
   migration, but new keys are never written there when no credential service is
   available.
3. **Stored data** -- conversations and task records in the per-user data
   directory. Persistence is on by default and can be disabled.
4. **Live session state** -- workspace and conversation context in memory for
   the current run.

Environment variables are an optional source, consulted only for the provider
that owns them.

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

=====================  =======================================================================
Field                  Meaning
=====================  =======================================================================
``name``               Provider implementation. The current build ships ``openai_compatible``.
``base_url``           Must be ``http(s)``. A trailing ``/`` is stripped.
``model``              Model identifier sent to the endpoint.
``timeout_seconds``    Per-request timeout (5-3600 s).
``max_output_tokens``  Upper bound on the response (128-200000).
``temperature``        Sampling temperature, 0.0-2.0.
``api_key_env``        Environment variable consulted after the secret store, and
                       only for the provider that owns that name.
``organization``       Optional organisation header.
=====================  =======================================================================

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
    auto_approve_writes = false
    command_timeout_seconds = 120.0
    max_command_output_chars = 20000
    persist_conversations = true
    resume_last_conversation = true
    max_stored_conversations = 50
    git_enabled = true
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
     - Reserved for a future retrieval index.
   * - ``max_file_bytes``
     - Hard cap on what ``read_file`` returns, and the ceiling for
       ``write_file``/``edit_file`` content.
   * - ``auto_approve_reads``
     - ``false`` turns workspace reads into ``ASK`` decisions, which degrade to
       ``DENY`` without an approver.
   * - ``auto_approve_writes``
     - Approves ``write_file`` and ``edit_file`` without prompting. It cannot
       widen ``delete_file`` or ``run_command``, which always ask.
   * - ``command_timeout_seconds``
     - Default time limit for ``run_command`` when the model does not ask for
       one. Clamped to 1-600s.
   * - ``max_command_output_chars``
     - Per-stream cap on captured command output. The head and the tail are both
       kept, because the failing part of a build or test run is at the end.
   * - ``persist_conversations``
     - Store conversations on disk so they survive a restart. ``false`` keeps them
       in memory only, the older in-memory-only behaviour.
   * - ``resume_last_conversation``
     - Reopen the most recent conversation when the application starts. The
       transcript comes back; approval grants and undo history never do.
   * - ``max_stored_conversations``
     - How many conversations to keep. The oldest are pruned beyond the cap.
   * - ``max_stored_messages``
     - Messages kept per conversation. The oldest go and the newest stay; the
       opening request always survives the trim.
   * - ``max_stored_tool_results``
     - Tool results kept per conversation, oldest pruned first. ``0`` keeps none.
   * - ``max_stored_item_chars``
     - Length cap on any single stored value. Long tool output is shortened in
       place with a note; the model still sees the whole value in the session.
   * - ``git_enabled``
     - Registers the read-only Git tools. ``false`` removes them entirely
       rather than denying them, so a model cannot even name them.
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

1. the provider-bound credential in the secure store
   (``provider:<provider_id>:default:api_key``), or a legacy credential an
   operator has explicitly bound to that provider;
2. that provider's own environment variable (``OPENROUTER_API_KEY``,
   ``OPENAI_API_KEY``, ``GROQ_API_KEY`` and so on);
3. ``api_key_env``, but only when the configured provider owns that generic
   name. ``QM_API_KEY`` belongs to OpenAI, the default provider, and is never
   read for another service.

A credential is bound to one endpoint identity. Editing the base URL to point at
a different service clears the binding in Settings; the previous provider's key
is not carried over, and a request is refused rather than sent with it.

Rules the implementation guarantees:

* the key is never written to ``config.toml``;
* the key is never written to the log file -- a redaction filter scrubs
  ``Authorization``/``api_key`` patterns from every record;
* if no OS credential service is usable, saving a key fails visibly and writes
  nothing to disk. An existing ``secrets.json`` still authenticates and is
  reported as plaintext (degraded);
* ``secrets.json`` is never overwritten or deleted automatically; moving a legacy
  value to its provider-bound slot is an explicit, read-back-verified action;
* the Settings dialog never renders a stored key; the field is empty on open
  and shows ``sk-******op`` as masked status text;
* readiness, the Settings hint, the security panel and the request path all use
  the same credential decision, so they cannot promise a key the request would
  refuse.

Clearing a key: Settings -> **Clear stored key**. The provider's own slot is
removed; a shared legacy slot is left alone because it may belong to another
provider.

Environment variables
=====================

======================  ===========================================
Variable                Effect
======================  ===========================================
``OPENAI_API_KEY``      Key for OpenAI
``OPENROUTER_API_KEY``  Key for OpenRouter
``GROQ_API_KEY``        Key for Groq
``DEEPSEEK_API_KEY``    Key for DeepSeek
``MINIMAX_API_KEY``     Key for MiniMax
``TOGETHER_API_KEY``    Key for Together
``FIREWORKS_API_KEY``   Key for Fireworks
``QM_API_KEY``          Key for the default provider (OpenAI) only
``QM_CONFIG_DIR``       Override the configuration directory
``QM_LOG_DIR``          Override the log directory
``QT_QPA_PLATFORM``     Set to ``offscreen`` for headless GUI tests
======================  ===========================================

.. code-block:: powershell

    $env:OPENROUTER_API_KEY = "sk-..."
    quantsmind-agent

A provider-specific variable is only read when that provider is configured.
Using the GUI is still the recommended way to configure a key, because it goes
through the credential store rather than the process environment.

Resetting everything
====================

.. code-block:: powershell

    Remove-Item -Recurse "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent"

Then launch the application and re-enter your settings. This removes the
configuration, credentials, logs, stored conversations and task records in that
per-user profile; it does not touch your project folders. There is no telemetry.
If you only want to clear conversation history, use the conversation controls or
turn off ``persist_conversations`` instead of deleting the profile.
