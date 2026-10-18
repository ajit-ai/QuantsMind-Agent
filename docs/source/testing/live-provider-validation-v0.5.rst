.. _live-provider-validation-v0.5:

===============================
Live provider validation v0.5.0
===============================

One genuine request against a real provider, recorded so the release decision rests
on evidence rather than on the simulated-provider suite passing.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Field
     - Value
   * - Date and time
     - 2026-10-18, 13:20-13:26 +05:30
   * - Release branch
     - ``release/v0.5.0``
   * - Tested commit
     - ``f53fe48`` (all application code; see "Commit tested" below)
   * - Provider
     - OpenRouter, via the ``openai_compatible`` adapter
   * - Endpoint
     - ``https://openrouter.ai/api/v1``
   * - Model
     - ``openai/gpt-4o-mini``
   * - Credential source
     - OS secret store, resolved through ``ConfigManager.api_key_value()``
   * - Declared capabilities
     - ``tools=yes; streaming=yes; structured_output=yes; cancellation=no``
   * - Outcome
     - **Passed** -- all four checks succeeded

Method
======

The requests were issued through the application's own interfaces, not through an
independent HTTP client: ``ConfigManager`` resolved the credential, ``providers.
build_provider`` constructed the adapter, and ``ModelGateway`` issued the request.
That is deliberate -- a smoke test against a hand-rolled client would prove the
provider is reachable, not that the shipped adapter can talk to it.

Credentials were never printed, logged, passed on a command line, or written to a
file. The script held only a callable, so the key was read by the provider at
request time. Prompts were a single fixed word ("acknowledged", "streamed") and
requests were bounded to 16-64 output tokens with a 45-60 second timeout.

Results
=======

Plain completion
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Check
     - Result
   * - Endpoint reachable
     - Yes
   * - Authentication accepted
     - Yes
   * - Model accepted the request
     - Yes
   * - Genuine response returned
     - ``acknowledged`` -- the exact word requested
   * - Adapter parsed the response
     - Yes; ``finish_reason=stop``, ``response_model`` matched the request
   * - Usage metadata captured
     - ``prompt_tokens=14``, ``completion_tokens=3``, ``total_tokens=17``
   * - Latency
     - 3.07 s

Tool calling
------------

One genuine tool-calling round trip: the model was offered the real ``read_file``
schema from the application registry, in a throwaway directory, and asked to read
a single known file.

* The model returned a genuine tool call -- ``read_file`` with
  ``{"path": "sample.txt"}`` -- and the adapter parsed it into the shared
  ``ToolCall`` shape with ``finish_reason=tool_calls``.
* Usage metadata: ``prompt_tokens=146``, ``completion_tokens=15``.

The parsed call was then executed twice through the real
:class:`~quantsmind_agent.permissions.PermissionBroker`, which is the point of the
exercise -- a tool call that is generated but never authorised must not run:

.. list-table::
   :header-rows: 1
   :widths: 40 20 40

   * - Policy configured
     - Decision
     - Outcome
   * - No rule (fail-closed default)
     - ``DENY``
     - ``PermissionDeniedError``; the tool did **not** run
   * - Explicit ``READ_WORKSPACE`` allow rule for ``read_file``
     - ``ALLOW``
     - Executed read-only: ``read sample.txt (1 lines)``

Both branches behaved correctly against a live model. No shell command was run, no
file outside the sandbox was read, and nothing was written.

Streaming
---------

A genuine streamed response, parsed by the adapter's SSE handling:

* 4 chunks received.
* Assembled text: ``streamed`` -- the exact word requested.
* ``finish_reason`` of ``stop`` observed on the terminal chunks.
* Latency: 2.33 s.

Not tested
==========

* **Cancellation.** Declared ``cancellation=no`` for this adapter; the application
  does not abort an in-flight HTTP request, and no release claim depends on it.
* **``structured_output``.** Declared supported, but no runtime path consumes it
  yet. Its declaration is unverified against this endpoint, and it is listed as a
  known limitation in the changelog.
* **Other providers and models.** Only OpenRouter with one model was exercised.
  Every other supported endpoint remains validated by the simulated contract suite
  alone.

Commit tested
=============

The three commits after ``f53fe48`` (``docs``, ``docs``, ``docs``) and this report
change no application code: documentation, changelog and this file. The packaged
artifacts were built from ``102aef1``, which is the version-metadata commit before
them. The tested application code is therefore identical to the shipped artifacts'
code.

Recommendation
==============

**Release eligible.** Authentication, request handling, response parsing, usage
capture, tool calling and streaming are all confirmed against a real endpoint
through the shipped adapter. The single unverified claim -- whether the declared
``structured_output`` capability behaves as declared on this endpoint -- is not
relied upon by any code path, and is documented as a limitation rather than a
capability.
