.. _index:


================
QuantsMind-Agent
================


**QuantsMind-Agent is an AI coding-agent platform inspired by modern
coding-agent workflows.**

QuantsMind-Agent is a Windows desktop application that connects a real language
model to a local project folder. You point it at a repository, ask a coding
question, and the agent inspects your files through controlled tools before
answering. Everything it touches is visible in the interface.

Version 0.1 is a complete vertical slice:

.. code-block:: text

    Windows 11 user
          |
    QuantsMind-Agent GUI
          |
    Workspace  ->  Agent Core  ->  Model Gateway  ->  real LLM
          |            |               |
          |            v               v
          |        Tool Registry     Tool schemas
          |        + Permissions     + Observations
          v
    Tool activity + answer

.. toctree::
   :maxdepth: 2
   :caption: Start here

   getting-started
   installation
   configuration
   windows

.. toctree::
   :maxdepth: 2
   :caption: Using the agent

   user/workspace
   user/conversation
   user/tools
   user/permissions
   user/settings

.. toctree::
   :maxdepth: 2
   :caption: Architecture

   architecture/overview
   architecture/agent-core
   architecture/model-gateway
   architecture/context-engine
   architecture/tool-system
   architecture/workspace
   architecture/permissions
   architecture/session
   architecture/gui
   architecture/events
   architecture/security-boundaries
   architecture/adr/index

.. toctree::
   :maxdepth: 2
   :caption: Development

   developer/development
   developer/testing
   developer/logging
   developer/extension-points
   developer/troubleshooting

.. toctree::
   :maxdepth: 2
   :caption: Testing and quality

   testing/test-strategy
   testing/manual-acceptance
   testing/acceptance-v0.1

.. toctree::
   :maxdepth: 2
   :caption: Project

   releases/v0.1
   roadmap/roadmap
   security

Indices
=======

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
