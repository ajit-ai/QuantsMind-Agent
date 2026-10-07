.. _developer-development:


===========
Development
===========


Setup
=====

.. code-block:: powershell

    git clone https://github.com/ajit-ai/QuantsMind-Agent.git
    cd QuantsMind-Agent

    python -m venv .venv
    .venv\Scripts\Activate.ps1

    pip install -e ".[dev,docs]"

    pre-commit install        # optional; hooks mirror the CI gates

Every command below is what CI runs.

Quality gates
=============

.. code-block:: powershell

    ruff check .              # lint
    ruff format --check .     # formatting
    mypy                      # strict types
    pytest                    # unit + integration + GUI
    scripts\build-docs.ps1    # documentation
    python -m build           # sdist + wheel

All must exit ``0``.

Tooling configuration
=====================

* **ruff** -- line length 100, target ``py311``, rules ``E W F I B C4 UP SIM RUF``.
  Run ``ruff check --fix .`` and ``ruff format .`` before committing.
* **mypy** -- ``strict = true`` over ``src``, ``tests`` and ``scripts``.
  Three modules are annotated through the stricter manual approach the type
  checker needs:

  .. code-block:: python

      # With __slots__, declare annotations on the class for the type checker
      # while keeping the runtime attribute list small.
      class ToolSpec:
          __slots__ = ("name", "description", ...)

          name: str
          description: str

  If a third-party Qt stub is missing, ``ignore_missing_imports`` covers it.

* **pytest** -- ``asyncio_mode = auto``, markers ``gui``, ``integration``,
  ``slow``. ``pythonpath = ["src"]`` means no editable install is required to run
  the suite.

Layout
======

.. code-block:: text

    src/quantsmind_agent/
        app.py                 composition root and CLI
        agent.py               the loop
        llm.py                 gateway DTOs and interface
        providers/             concrete providers
        tools.py               tool contract and registry
        workspace_tools.py     the v0.1 tools
        workspace.py           path containment
        permissions.py         capability policy
        context.py             prompt assembly
        session.py             conversation state
        schema.py              message dataclasses
        config.py              validated settings
        secrets.py             credential stores
        paths.py               per-user directories
        events.py              event types and bus
        errors.py              error taxonomy
        logging_setup.py       logging and redaction
        toml_writer.py         minimal TOML writer
        gui/                   PySide6 widgets and worker
    tests/
        conftest.py            isolation fixtures
        fakes.py               scripted provider and httpx transports
        harness.py             agent wiring for integration tests
        unit/ integration/ gui/ fixtures/
    docs/source/              this documentation
    scripts/                  build-docs.ps1, package.ps1, fixture generator

Adding a dependency
===================

The bar is high: a dependency must remove real complexity and must not pull in a
transitive tree the user does not need.

.. code-block:: powershell

    pip install <package>
    pip show <package> | Select-String "Requires"

    # then edit pyproject.toml, and justify it in the PR description

Rules:

* runtime dependencies are justified by the packaging decision in
  :doc:`../installation`;
* prefer the standard library for anything under ~100 lines
  (``toml_writer.py`` exists for exactly this reason);
* test-only helpers live in ``tests/fakes.py``, never in the runtime path.

Adding a tool
=============

.. code-block:: python

    from quantsmind_agent.permissions import Permission
    from quantsmind_agent.tools import ToolResult, ToolSpec

    class WordCountTool:
        spec = ToolSpec(
            name="word_count",
            description="Count words in one workspace text file.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="word count {path}",
        )

        def __init__(self, workspace):
            self._workspace = workspace

        def run(self, arguments):
            text = self._workspace.read_text(arguments["path"])
            count = len(text.split())
            return ToolResult.success(
                "word_count", f"{count} words in {arguments['path']}", {"words": count}
            )

Register it in ``ApplicationContext._rebuild`` (see
:func:`~quantsmind_agent.workspace_tools.build_default_tools`) and add tests:

* a success case asserting the returned data;
* an escaping-path case asserting refusal;
* a case asserting the declared permission is enforced.

See :doc:`extension-points` for provider, context and session extensions.

Scripts
=======

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Script
     - Purpose
   * - ``scripts\build-docs.ps1``
     - Build ``docs\build\html\index.html``; fails on any warning
   * - ``scripts\package.ps1``
     - PyInstaller one-directory build into ``dist\``
   * - ``scripts\run.ps1``
     - Launch from source with the repo on ``PYTHONPATH``
   * - ``scripts\make_acceptance_fixture.py``
     - Create the disposable project for manual acceptance

Commit and PR conventions
=========================

* One logical change per commit; the repository style is conventional commits
  (``feat:``, ``fix:``, ``docs:``, ``test:``, ``chore:``, ``refactor:``).
* A bug fix includes the test that would have caught it. Several v0.1 bugs were
  found this way, and each has a regression test.
* Update ``CHANGELOG.md`` under *Unreleased* for user-visible changes.
* Update the docs when behaviour changes. The documentation is part of the
  product, not an afterthought.

Related
=======

* :doc:`testing`
* :doc:`extension-points`
* :doc:`troubleshooting`
