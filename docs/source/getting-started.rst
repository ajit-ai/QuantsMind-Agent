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
   agent registers its read-only tools for that folder.

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

What v0.1 does and does not do
==============================

Does
----

* Reads a local project through four tools: ``project_structure``,
  ``list_directory``, ``read_file``, ``search_content``.
* Streams the final answer when the provider supports streaming.
* Shows every tool call, with its state, while it happens.
* Keeps the conversation for as long as the application stays open.
* Enforces a workspace boundary on every path, including symlink escapes.

Does not (yet)
--------------

* No file writes, no command execution, no git, no network tools.
* No persistent memory or conversation history across restarts.
* No multi-agent orchestration, no MCP, no VS Code extension.

These are deliberate v0.1 boundaries, documented in
:doc:`releases/v0.1` and planned in :doc:`roadmap/roadmap`.

Next steps
==========

* :doc:`installation` -- install, run, and build a package.
* :doc:`configuration` -- every setting and where it lives.
* :doc:`user/tools` -- what the agent can read and how.
* :doc:`testing/manual-acceptance` -- verify v0.1 on your machine.
