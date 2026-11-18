.. _getting-started:


===============
Getting started
===============


Requirements
============

* Windows 11 (Linux and macOS work for development and headless testing).
* Python 3.11 or newer.
* An OpenAI-compatible API endpoint and an API key.
  QuantsMind-Agent talks to the ``/chat/completions`` contract, so this includes
  OpenAI, OpenRouter, Groq, Together, vLLM, Ollama, LM Studio and llama.cpp.
  A local server (for example ``http://localhost:11434/v1``) needs no API key.

Five-minute tour
================

#. Install and launch::

       git clone https://github.com/ajit-ai/QuantsMind-Agent.git
       cd QuantsMind-Agent
       python -m venv .venv
       .venv\Scripts\activate
       pip install -e ".[dev,docs]"

   See :ref:`installation` for the full matrix.

#. Start the application::

       quantsmind-agent

   On first launch the transcript says *Open a project folder first*.

#. Open a project. Click **Open project…** in the sidebar and select a folder
   on disk. The folder path appears in the sidebar and the status bar, and the
   agent registers its workspace tools for that folder.

#. Configure the model. Click **Settings…** (``Ctrl+K``), pick a preset such as
   **OpenAI**, confirm the model name, and paste your API key. The key is
   stored in the Windows Credential Manager and is never displayed again.

#. Ask a question. For example::

       Explain the structure of this project.

   The agent calls ``project_structure``, shows each tool call in the
   *Tool activity* panel, and streams the answer into the transcript.

#. Try a tool-driven question::

       Read the README and summarize the project.

   You will see ``read_file`` called with ``README.md`` before the answer
   appears. That is the difference between a chatbot and a coding agent: it
   read the file, it did not guess.

.. _getting-started-shortcuts:

Keyboard shortcuts
==================

==============  ========================
Shortcut        Action
==============  ========================
``Ctrl+Enter``  Send the current message
``Ctrl+O``      Open a project folder
``Ctrl+K``      Open Settings
``Ctrl+N``      Start a new conversation
==============  ========================

The **Stop** button appears while a request is running. Cancellation is
cooperative: it takes effect at the next agent iteration or as soon as a
streamed response yields. An HTTP request already in flight cannot be aborted,
but a late answer is discarded rather than shown.

What v0.9.0 does and does not do
================================

Does
----

* Reads and navigates a local project through eight read-only tools, including
  ``project_structure``, ``read_file``, ``code_outline`` and
  ``project_context``.
* Writes, edits, deletes and undoes files, with approval before every change.
* Runs an approved command with a timeout and bounded output; the command policy
  refuses destructive operations.
* Reads Git state through ``git_status``, ``git_diff`` and ``git_log``.
* Plans against a bounded workspace snapshot and keeps that snapshot with the
  task as evidence.
* Streams the final answer, shows every tool call, and persists conversations,
  task history, checkpoints and recovery state.
* Enforces a workspace boundary on every path, including direct symlink and
  junction escapes.

Does not (yet)
--------------

* No OS-level sandbox: approved commands run with your privileges.
* No built-in web-fetch or arbitrary network tool. An approved command may use
  the network; the policy blocks common transfer tools but is not a sandbox.
* No Git mutations: commit, push, checkout and reset are deliberately blocked.
* No MCP, VS Code extension or multi-agent orchestration.
* No signed Windows release. The published v0.9.0 MSI is unsigned and is a
  limited beta; real-model, clean-machine and human approval evidence is still
  outstanding.

See :doc:`architecture/v0.9-behaviour` for the release behaviour and
:doc:`qualification/index` for what has actually been verified.

Next steps
==========

* :doc:`installation` -- install, run, and build a package.
* :doc:`configuration` -- every setting and where it lives.
* :doc:`user/tools` -- what the agent can do and how.
* :doc:`qualification/manual-beta-acceptance` -- the current v0.9.0 manual
  qualification plan.
