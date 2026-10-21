.. _index:


================
QuantsMind-Agent
================


**QuantsMind-Agent is an AI coding-agent platform inspired by modern
coding-agent workflows.**

QuantsMind-Agent is a desktop application that connects a real language model to
a local project folder. You point it at a repository, ask a coding question, and
the agent inspects your files through controlled tools before answering.
Everything it touches is visible in the interface. Windows 11 is the supported
desktop target; Linux is supported headless.

Version 0.9.0 is a controlled coding agent. It can read and navigate a project,
write, edit, delete and undo files, run approved commands, and inspect Git state.
Planning starts from a bounded workspace snapshot, and a task is planned,
approved, executed and verified through the application's own records. Every
change is authorised by the application rather than by the model. Command
execution is not an OS sandbox and the published Windows MSI is still a limited
beta; see :doc:`qualification/index`.

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
   architecture/v0.6-behaviour
   architecture/v0.7-behaviour
   architecture/v0.8-behaviour
   architecture/v0.9-behaviour
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
   testing/live-provider-validation-v0.5
   known-issues
   qualification/index

.. toctree::
   :maxdepth: 2
   :caption: Project

   releases/v0.1
   releases/v0.2
   releases/v0.3
   releases/v0.4
   roadmap/roadmap
   roadmap/v0.5-implementation
   security

Indices
=======

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
