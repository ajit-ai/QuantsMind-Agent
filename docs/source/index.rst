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

Version 0.2 is a controlled coding agent: it can read, write, edit, delete, run
approved commands and inspect Git state, and every change is authorised by the
application rather than by the model.

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
          |        + Command policy
          v
    Approval prompt + tool activity + answer

.. toctree::
   :maxdepth: 2
   :caption: Start here

   getting-started
   installation
   packaging
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
   architecture/task-lifecycle
   architecture/model-gateway
   architecture/context-engine
   architecture/tool-system
   architecture/command-execution
   architecture/workspace
   architecture/permissions
   architecture/session
   architecture/gui
   architecture/events
   architecture/security-boundaries
   architecture/persistence-and-recovery
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
   testing/acceptance-v0.2
   testing/platform-acceptance-matrix
   testing/native-gui-acceptance
   known-issues

.. toctree::
   :maxdepth: 2
   :caption: Project

   releases/v0.1
   releases/v0.2
   releases/v0.3
   releases/v0.4
   roadmap/roadmap
   security

Indices
=======

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
