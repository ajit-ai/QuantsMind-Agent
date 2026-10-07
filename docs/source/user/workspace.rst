.. _user-workspace:


=========
Workspace
=========


A *workspace* is the single project folder QuantsMind-Agent may inspect. The
user picks one folder; every tool resolves paths inside it and nowhere else.

Opening a project
=================

* **Open project…** in the sidebar, or ``Ctrl+O``.
* On startup: ``quantsmind-agent --workspace C:\code\my-project``.
* The most recent folder is remembered in ``config.toml`` (``last_workspace``)
  and reopened on the next launch. If it no longer exists, startup continues
  without it.

The sidebar shows the resolved absolute path. The agent receives that path in
its system prompt as *Current workspace*.

What the agent can reach
========================

Only entries under the root:

* the root itself (``"."``) and any subdirectory;
* paths using ``/`` or ``\``;
* paths wrapped in quotes (``"README.md"``) because models sometimes add them.

Ignored by default
------------------

These directory *names* are never traversed or listed, because they are huge,
generated or irrelevant to reading code:

``.git``, ``.hg``, ``.svn``, ``.idea``, ``.vscode``, ``.venv``, ``venv``,
``env``, ``node_modules``, ``__pycache__``, ``.pytest_cache``, ``.mypy_cache``,
``.ruff_cache``, ``dist``, ``build``, ``.tox``, ``.eggs``, ``.next``,
``.nuxt``, ``target``, ``vendor``, ``.gradle``, ``.terraform``,
``site-packages``.

Binary files (images, archives, executables, databases, model weights) are
skipped by search and refused by ``read_file``.

Path validation
===============

``Workspace.resolve()`` refuses, with an explanatory message:

===================================  =============================
Rejected                             Reason
===================================  =============================
``../secrets.txt``                   parent-directory traversal
``/etc/passwd``                      absolute POSIX path
``C:\Windows\System32\...``          absolute Windows path / drive
``\\server\share``                   UNC path
``~/notes.md``                       home-directory reference
``src/app\x00.py``                   null byte
a symlink/junction pointing outside  resolves outside the root
===================================  =============================

The agent sees the refusal as an observation and can correct itself::

    TOOL ERROR (read_file): 'secrets.txt' is outside the current workspace.
    QuantsMind-Agent can only read files inside the project folder you opened.

Changing the ignore list
========================

.. code-block:: python

    from quantsmind_agent.workspace import Workspace

    space = Workspace(r"C:\code\project", ignored_directories={"fixtures"})

The GUI always uses the default list; the parameter exists for embedders.

Recommended project shapes
==========================

v0.1 works best on projects where the answer lives in a handful of text files:
Python, JavaScript/TypeScript, Java, Go, Rust, C#, C/C++, shell, Markdown,
YAML/JSON/TOML. Monorepos work, but you should open the sub-project that the
question is about -- the workspace summary lists only the top level, and a very
large tree is slower to search.

Related
=======

* :doc:`tools` -- the four tools built on the workspace.
* :doc:`permissions` -- why reads are allowed and writes are denied.
* :doc:`../architecture/workspace` -- the implementation contract.
