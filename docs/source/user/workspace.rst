.. _user-workspace:


=========
Workspace
=========


A *workspace* is the single project folder QuantsMind-Agent may access through
its built-in tools. The user picks one folder; every file tool and every Git
tool resolves paths inside it and nowhere else. An approved command starts in
the workspace but is a separate, less contained capability; see
:doc:`../architecture/command-execution`.

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
    QuantsMind-Agent can only access files inside the project folder you opened.

Project discovery does not follow symbolic links, directory junctions or other
filesystem reparse points. They are omitted from listings and scans, and the
project snapshot says how many paths were not followed. A direct request that
resolves outside the workspace is still refused.

Changing the ignore list
========================

.. code-block:: python

    from quantsmind_agent.workspace import Workspace

    space = Workspace(r"C:\code\project", ignored_directories={"fixtures"})

The GUI always uses the default list; the parameter exists for embedders.

Recommended project shapes
==========================

The workspace tools work best on projects where the answer lives in text files:
Python, JavaScript/TypeScript, Java, Go, Rust, C#, C/C++, shell, Markdown,
YAML/JSON/TOML. Monorepos work, but opening the sub-project a question is about
keeps discovery and search focused. The project snapshot reports roots,
declared checks and an explicit sample, so a large tree is not mistaken for a
complete inventory.

Related
=======

* :doc:`tools` -- the read, navigation, mutation, command and Git tools.
* :doc:`permissions` -- why reads are allowed and writes ask.
* :doc:`../architecture/workspace` -- the implementation contract.
