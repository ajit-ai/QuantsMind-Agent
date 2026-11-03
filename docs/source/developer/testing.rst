.. _developer-testing:


=======
Testing
=======


The suite is offline by construction. Nothing in it performs network I-O, and
no test needs an API key -- which means it can run on a plane and in CI without
credentials.

Layout
======

.. code-block:: text

    tests/
      conftest.py         isolation fixtures (paths, env, logging)
      fakes.py            scripted provider, httpx mock transport
      harness.py          agent wiring used by integration tests
      unit/               one module per subsystem
      integration/        the full agent loop and the composition root
      gui/                widgets and the main window (offscreen Qt)
      fixtures/           repository-level fixtures

Commands
========

.. code-block:: powershell

    pytest                                   # everything
    pytest tests/unit -q                     # unit only
    pytest tests/integration -q              # agent loop and composition
    pytest tests/gui -q                      # GUI (offscreen)
    pytest -m "not gui" -q                   # headless subset
    pytest -m integration -q                 # agent loop
    pytest --cov=quantsmind_agent --cov-report=term-missing
    pytest tests/unit/test_workspace.py -v   # one module

Headless Qt
===========

GUI tests need a platform plugin that works without a desktop:

* Windows: ``QT_QPA_PLATFORM=offscreen`` (set automatically by the fixture);
* Linux CI: install ``xvfb`` and run under ``xvfb-run -a pytest``.

If neither is available the GUI tests are skipped rather than failing, so the
headless suite still passes.

Isolation
=========

``tests/conftest.py`` guarantees three things for every test:

* **No ambient credentials.** ``QM_API_KEY`` and every provider-specific
  variable (``OPENAI_API_KEY``, ``OPENROUTER_API_KEY``, and the rest) are
  removed.
* **No real profile.** ``AppPaths`` points at ``tmp_path`` and the configuration
  manager uses an in-memory secure store, so tests never touch
  ``%LOCALAPPDATA%\QuantsMind-Agent`` or the operator's real keyring.
* **No log noise.** The package logger is pinned to ``CRITICAL`` and handlers
  are torn down afterwards.

.. code-block:: text

    environment: no ambient API keys        <- appears in the pytest header

What is covered
===============

==========================  ======================================================
Area                        Where
==========================  ======================================================
Configuration and TOML      ``unit/test_config.py``
Secrets, masking, paths     ``unit/test_secrets_paths.py``
Workspace and path escape   ``unit/test_workspace.py``
Tools and registry          ``unit/test_tools.py``
Workspace tools             ``unit/test_workspace_tools.py``
Permissions                 ``unit/test_permissions.py``
Provider (offline HTTP)     ``unit/test_provider.py``, ``test_provider_stream.py``
Gateway                     ``unit/test_llm_gateway.py``
Context engine              ``unit/test_context.py``
Session, events             ``unit/test_session_events.py``
Errors, redaction, logging  ``unit/test_errors_logging.py``
Agent loop and recovery     ``integration/test_agent_loop.py``
Streaming and composition   ``integration/test_streaming_and_app.py``
Widgets                     ``gui/test_widgets.py``
Main window                 ``gui/test_main_window.py``
Credential resolution       ``security/test_phase08_credential_authority.py``
Provider/endpoint identity  ``security/test_m3b_s11_credential_security.py``
Credential migration        ``security/test_m3b_s12_credential_migration.py``
Security wiring             ``security/test_m3b_s11_config_and_wiring.py``
Sandbox/transport           ``security/test_m3b_s14_sandbox_and_transport.py``,
                            ``security/test_m3b_s14_pinned_transport.py``
Security settings GUI       ``gui/test_m3b_s12_security_settings.py``
==========================  ======================================================

.. _developer-testing-fakes:

Test doubles
============

``FakeModelProvider`` implements the full ``ModelProvider`` contract from a
scripted response list:

.. code-block:: python

    from tests.fakes import FakeModelProvider, text_response, tool_response

    provider = FakeModelProvider()
    provider.script(
        tool_response([("read_file", {"path": "README.md"})]),
        text_response("The README describes a sample project."),
    )

It records every request, so tests can assert on what the agent actually sent:

.. code-block:: python

    assert [m.role.value for m in provider.requests[1].messages[-2:]] == [
        "assistant", "tool"
    ]

For provider-level tests, ``canned_body`` / ``sse_body`` plus ``mock_transport``
serve ``httpx`` responses, including errors and malformed payloads:

.. code-block:: python

    body = httpx.Response(status_code=429, json={"error": {"message": "slow down"}})
    provider = _provider(mock_transport({"/chat/completions": body}))
    with pytest.raises(ModelUnavailableError):
        provider.complete(request)

Continuous integration
======================

``.github/workflows/ci.yml`` runs on pushes to ``main``, ``develop`` and
``release/**``, on version tags (``v*``), and on pull requests targeting those
branches. The test job matrix covers Ubuntu (Python 3.11 and 3.12), Windows 3.12
and macOS 3.12. ``tests/security`` runs as its own step on every matrix entry,
so credential, identity and audit invariants are exercised on the Windows
target as well as on Linux.

Coverage is measured on Ubuntu with Python 3.12 over the whole suite and
uploaded as an XML artifact. Coverage is currently reported and uploaded; no
threshold is enforced, so a coverage drop is visible but does not fail the
build. The measured baseline at Phase 0.8 was 88% (2111 passed, 3 skipped on
Windows).

A local run is not CI evidence. A green CI result may only be claimed for an
exact commit that the hosted workflow actually tested.

.. _developer-testing-what-is-not-covered:

What is NOT covered
===================

Honest limits of the automated suite:

* **No real LLM call.** No test contacts a provider, so model behaviour --
  quality of tool arguments, refusal behaviour, prompt sensitivity -- is not
  verified automatically. That requires the manual acceptance procedure in
  :doc:`../testing/manual-acceptance`.
* **No interactive desktop.** Widgets are exercised offscreen: no window
  dragging, DPI scaling, native dialogs, taskbar integration or focus behaviour.
* **No visual regression.** There are no screenshot assertions.
* **No packaging test in the ordinary suite.** ``python -m build`` validates
  metadata and imports. PyInstaller, MSI install and uninstall checks have
  dedicated scripts, but their results are separate evidence and are not
  inferred from a pytest run.
* **No concurrency stress.** The threading tests are single-turn and
  deterministic.

Never mark any of these as passing on the strength of the automated suite.

Adding tests
============

* One behaviour per test, named after the behaviour:
  ``test_escaping_path_is_refused_not_raised``.
* Use the fixtures in ``tests/conftest.py``: ``workspace``, ``sample_project``,
  ``config_manager``, ``app_paths``, ``escape_target``.
* Assert on user-visible outcomes (returned data, rendered text, event
  sequences), not on private attributes.
* A bug fix ships with the test that would have caught it. Several v0.1 bugs
  were found exactly this way, and each has a regression test today:

  .. code-block:: text

      stale AgentCore reference after a workspace rebuild
      cross-thread signal-to-signal events silently dropped
      final answer discarded when a provider could not stream
      cancel issued immediately after Send being cleared
      credential-shaped errors re-wrapped as network errors

Related
=======

* :doc:`../testing/test-strategy`
* :doc:`../testing/manual-acceptance`
* :doc:`../testing/acceptance-v0.1`
