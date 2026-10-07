.. _architecture-workspace:


=========
Workspace
=========


:class:`~quantsmind_agent.workspace.Workspace` is the only object permitted to
touch user files. It resolves the root once and validates every path against it.

.. code-block:: python

    space = Workspace(r"C:\code\my-project")

    space.root                    # canonical absolute Path
    space.resolve("src/app.py")   # -> C:\code\my-project\src\app.py
    space.relative(absolute)      # -> src/app.py
    space.list_dir("src")         # -> [FileEntry, ...]
    space.walk(".", max_entries=5000)
    space.read_text("README.md", max_bytes=200_000)
    space.exists("docs"), space.is_dir("src"), space.is_file("pyproject.toml")
    space.count_children("src")

Construction
============

:meth:`Workspace.resolve` requires an existing directory. A missing path or a
file raises :class:`~quantsmind_agent.errors.InvalidWorkspaceError` with a
message the GUI can display directly.

Path validation
===============

:meth:`Workspace.resolve` rejects, in order:

1. empty paths and paths containing a null byte;
2. absolute POSIX paths (``/etc/passwd``);
3. Windows drive letters, UNC paths and roots (``C:\\x``, ``\\\\server\\share``);
4. home-directory references (``~``, ``~/notes``);
5. any ``..`` segment;
6. anything that resolves -- after following links -- outside the root.

Both separators are accepted, and surrounding quotes are stripped, because model
output uses both habits:

.. code-block:: python

    space.resolve(r"src\app\main.py") == space.resolve("src/app/main.py")
    space.resolve('"README.md"')       == space.root / "README.md"

.. _architecture-workspace-symlinks:

Symlinks and junctions
----------------------

Containment is checked on the *resolved* path, so a link inside the project that
points elsewhere is an escape:

.. code-block:: python

    project/link -> C:\Users\someone
    space.resolve("link/secret.txt")   # InvalidPathError

This is stricter than the user might expect when a repository legitimately
contains links, and it is the right default: the workspace is a security
boundary, not a convenience. Opening the parent directory is the workaround
when a real cross-directory reference is intended.

Traversal
=========

:meth:`Workspace.walk` skips ignored directory *names* and does not follow links:

.. code-block:: python

    for entry in space.walk(".", max_entries=5000):
        print(entry.path, entry.size, entry.modified)

Ignores are by name, not path, so ``node_modules`` is skipped at any depth.

Metadata
========

``FileEntry`` carries ``path``, ``name``, ``is_dir``, ``size``, ``modified`` (UTC
ISO-8601) and a ``kind`` property. Entries sort directories first, then by name,
so listings are stable and scannable.

Reads
=====

:meth:`Workspace.read_text` refuses directories and known binary suffixes,
encodes leniently (undecodable bytes become replacement characters rather than an
exception) and honours ``max_bytes``.

Reading a binary file returns:

.. code-block:: text

    'logo.png' looks like a binary file and cannot be read as text.

Not in v0.1
===========

There is no write, delete, rename, move or permission-changing API. Adding one
means adding it here -- so it inherits containment for free -- *and* declaring
the right :class:`~quantsmind_agent.permissions.Permission`, *and* an
interactive approver. See :doc:`../roadmap/roadmap`.

Related
=======

* :doc:`../user/workspace`
* :doc:`adr/0004-workspace-isolation`
* :doc:`security-boundaries`
