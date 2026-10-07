.. _adr:


======================
Architecture decisions
======================


Significant technical decisions, recorded as short ADRs. Trivial implementation
detail is deliberately *not* recorded here; it belongs in the code and in
:doc:`../../developer/development`.

.. toctree::
   :maxdepth: 1

   0001-python-pyside6
   0002-openai-compatible-provider
   0003-tool-calling-over-plugins
   0004-workspace-isolation
   0005-no-persistence-in-v01
   0006-events-and-threading

.. list-table::
   :header-rows: 1
   :widths: 12 46 42

   * - ADR
     - Decision
     - Consequence in one line
   * - :doc:`0001-python-pyside6`
     - Python + PySide6-Essentials
     - One language, offscreen GUI tests, PyInstaller packaging
   * - :doc:`0002-openai-compatible-provider`
     - One OpenAI-compatible contract
     - One client serves many vendors; natives can be added behind the gateway
   * - :doc:`0003-tool-calling-over-plugins`
     - Native function calling, not MCP
     - Typed tools in one request; MCP fits later behind the same registry
   * - :doc:`0004-workspace-isolation`
     - Containment in a Workspace class
     - Path escapes are impossible for read-only tools
   * - :doc:`0005-no-persistence-in-v01`
     - In-memory sessions
     - No database, verifiable privacy, history lost on restart
   * - :doc:`0006-events-and-threading`
     - Event bus + Qt worker bridge
     - Observable, testable, responsive GUI; three threading rules documented

Template
========

New ADRs live in ``docs/source/architecture/adr/`` and use this shape:

.. code-block:: rst

    ADR-NNNN: Title
    ===============

    Status
        Accepted | Superseded by ADR-MMMM

    Context
        What forces are relevant, and to whom.

    Options considered
        Each option with its strengths and its cost. Include "do nothing".

    Decision
        What was chosen, stated plainly.

    Consequences
        What becomes easy, what becomes hard, what is now a limitation.

Adding an ADR means adding it to the table above and to the toctree in
:doc:`../overview`.
