============================
Packaging and distribution
============================

QuantsMind-Agent ships as a desktop application for Windows, Linux and macOS.
This page describes what is built, how to build each format, how to install and
remove it, and — as importantly — which platforms have **not** been built and
verified natively yet.

Nothing here is aspirational. Where a platform is missing, it says so.

.. contents::
   :local:
   :depth: 2

The application is free
=======================

The desktop application carries **no licence fee** and requires no account with
us. It is a local application: it reads and edits files in a project folder you
open, on your machine.

Model usage is separate, and it is the part that can cost money:

* **Cloud models** are billed by your chosen provider, through *your* API key and
  *your* account. QuantsMind-Agent holds your key in your operating system's
  credential store. It embeds no key of its own, ships none, and will not
  function as a shared service.
* **Local models** avoid cloud charges entirely, if you run one and configure the
  application to use it. They need suitable hardware and are not our product.

Version 0.9.0 has no paid licence, no subscription, no telemetry and no
registration step.

Supported platforms
===================

.. list-table::
   :header-rows: 1
   :widths: 22 18 16 44

   * - Platform
     - Architecture
     - Status
     - Notes
   * - Windows 11
     - AMD64
     - **verified**
     - Executable folder and MSI, both built, installed and launched
   * - Linux (glibc)
     - x86_64
     - tooling only
     - ``.deb`` builder exists; not part of the v0.9.0 platform scope
   * - macOS
     - --
     - tooling only
     - ``.app``/``.dmg`` builders exist; not part of the v0.9.0 platform scope
   * - FreeBSD
     - --
     - tooling only
     - ``.pkg`` builder exists; not part of the v0.9.0 platform scope

**v0.9.0 ships for Windows 11 AMD64.** That is the whole of its platform scope:
the executable folder and the MSI below are built and verified. The Linux, macOS
and FreeBSD builders are in the repository because the packaging work supports
them, but they were not run for this release and nothing here claims they work.
"Verified" means built on that OS from this source, installed the way a user
would, launched, and confirmed to render a window and shut down cleanly.

Building
========

Every platform needs its own machine. PyInstaller does not cross-compile: a Linux
binary must be built on Linux and a macOS bundle on macOS. Building on the wrong
OS produces a file that will not run, or worse, one that appears to.

Common prerequisites
---------------------

* Python 3.11 or newer
* ``python -m pip install -e ".[packaging]"``

Windows: the MSI additionally needs
`WiX Toolset <https://wixtoolset.org>`_ 4 or newer. Use version 5, which is
Apache-2.0 licensed; WiX 7 requires accepting a maintenance-fee EULA, which is
not a decision a build script should make on your behalf::

    dotnet tool install --global wix --version 5.0.2

Build commands
--------------

.. code-block:: bash

    # Executable folder -- every platform
    python scripts/build_executable.py --distpath artifacts/v0.9.0/<platform>/onedir

    # Windows Installer (Windows)
    python scripts/build_msi.py --onedir artifacts/v0.9.0/windows/onedir

    # Debian package (Debian/Ubuntu)
    python scripts/build_deb.py --onedir artifacts/v0.9.0/ubuntu/onedir

    # Disk image (macOS)
    python scripts/build_dmg.py --app artifacts/v0.9.0/macos/QuantsMindAgent.app

    # FreeBSD package (FreeBSD)
    python scripts/build_freebsd_pkg.py --onedir artifacts/v0.9.0/freebsd/onedir

.. warning::

   Never pass ``--clean`` to ``build_executable.py`` in a checkout that holds
   build output you care about: it deletes ``dist/`` and ``build/`` outright.
   ``--distpath`` is the safe way to build somewhere new.

Artifacts and metadata
----------------------

Each build writes a ``BUILD-METADATA.json`` beside its output recording the
version, source commit, OS, architecture, Python, packaging tool and command.
Collect them into one manifest with checksums:

.. code-block:: bash

    python scripts/record_artifacts.py --artifacts artifacts/v0.9.0 \
        --commit "$(git rev-parse HEAD)" --version 0.9.0

That writes ``MANIFEST.json`` (the whole matrix, including platforms that were
**not** built, so an absent platform cannot be mistaken for a forgotten one) and
``SHA256SUMS``, which ``sha256sum -c`` can check.

Packaging history
=================

Every release has a permanent record in Git under ``packaging/vX.Y.Z/``:
``README.md``, ``BUILD-METADATA.json`` and ``checksums.sha256``. The MSI remains a
GitHub Release asset; Git stores the metadata and checksum that identify it.

The policy is documented in ``packaging/README.md``. ``packaging/v0.7.0/`` is the
historical baseline, reconstructed when the policy was introduced in v0.8.0.
``packaging/v0.8.0/`` is the first record produced under it, and
``packaging/v0.9.0/`` records the current release.

A release is not finalized until its packaging record and the published GitHub
artifact agree. The annotated tag identifies the source release commit and is
never moved when artifact metadata is added in a follow-up commit.

Installing
==========

Windows -- executable folder
-----------------------------

Copy the whole ``QuantsMindAgent`` folder. The ``.exe`` alone is not the
distribution; it needs the ``_internal`` directory beside it.

Windows -- MSI
--------------

Double-click ``QuantsMindAgent-<version>-x64.msi``, or:

.. code-block:: bat

    msiexec /i QuantsMindAgent-0.9.0-x64.msi

The default package is **per-user**: it installs to
``%LOCALAPPDATA%\Programs\QuantsMindAgent`` and needs **no elevation**. Build a
per-machine package instead with:

.. code-block:: bat

    python scripts\build_msi.py --onedir dist --scope perMachine

which installs to ``Program Files`` and **does** require elevation.

A per-machine install, an upgrade and an uninstall all leave your configuration,
credentials, logs and projects alone: they live in ``%LOCALAPPDATA%\QuantsMind-
Agent``, which no package writes to.

Linux -- Debian package
------------------------

.. code-block:: bash

    sudo dpkg -i quantsmind-agent_0.9.0_amd64.deb

Removes with ``sudo dpkg -r quantsmind-agent``, or ``dpkg -P`` to purge package
files. Either way your ``$XDG_CONFIG_HOME`` and ``$XDG_DATA_HOME`` are untouched;
delete them yourself if you want them gone.

The ``.deb`` is built and tested on one specific Ubuntu release. It declares
dependencies on the usual Qt runtime libraries (``libegl1``, ``libxkbcommon0``,
``libdbus-1-3``, ``libfontconfig1``, ``libglib2.0-0``), but a package that is
untested on a release is not a package that is verified on it.

macOS -- application and disk image
------------------------------------

Drag ``QuantsMindAgent.app`` into ``/Applications``, or open the ``.dmg`` and
drag. Remove by dragging the ``.app`` out of ``/Applications``; nothing outside
it is touched.

The image is unsigned and not notarised, so Gatekeeper will refuse to open it
until the user allows it explicitly (right-click, Open). Signing and notarisation
need an Apple Developer identity and credentials, and are not performed.

FreeBSD
-------

.. code-block:: sh

    sudo pkg install -y ./quantsmind-agent-0.9.0.pkg
    sudo pkg remove -y quantsmind-agent

Installing into ``/usr/local``, with a menu entry in
``/usr/local/share/applications``. User configuration stays in ``~/.config`` and
``~/.local/share``.

Upgrading and uninstalling
===========================

* **Upgrades replace, they do not stack.** Every package format carries a stable
  product identity (the MSI's ``UpgradeCode``, the Debian package name, the
  FreeBSD package name), so installing a newer build over an older one replaces
  the application files instead of leaving two copies.
* **Uninstalling removes the application, not your data.** Configuration,
  credentials, logs, conversation history and projects belong to the user in every
  format. Remove them deliberately, or copy them before uninstalling if you are
  unsure.
* **No package embeds an API key.** This is checked at build time: packaging fails
  if a configuration or credential file is found in the bundle.

Verification
============

The build system ships the checks that were used, so they can be re-run:

.. code-block:: bash

    # Starts the bundle headlessly and exercises configuration, paths and tools
    python scripts/smoke_test_executable.py --distpath artifacts/v0.9.0/windows/onedir

    # Launches the real executable on a real display and checks for a window
    python scripts/gui_acceptance.py --distpath artifacts/v0.9.0/windows/onedir

    # Installs, launches, reinstalls and uninstalls the MSI, and proves that the
    # real user profile is byte-identical afterwards
    powershell -File scripts/verify_msi_install.ps1 -Msi artifacts/v0.9.0/windows/msi/QuantsMindAgent-0.9.0-x64.msi

Known issues and unverified platforms
=====================================

* **Windows is what this release ships.** Both Windows artifacts are built from
  this source and were installed, launched and uninstalled on Windows 11.
* **Linux, macOS and FreeBSD are tooling only.** ``build_deb.py``,
  ``build_dmg.py`` and ``build_freebsd_pkg.py`` are implemented and refuse to run
  on the wrong operating system, but no artifact was produced from them for
  v0.9.0 and no claim is made about them. They are outside this release's
  platform scope, not a defect in it.
* **Nothing is code-signed.** No certificate is available on the build machine,
  so Windows SmartScreen will warn on first run of the MSI or the executable.
  Signing needs a real certificate; an invented signature would be worse than
  none. macOS notarization, were that in scope, is likewise not performed.
  The manual Windows signing and fail-closed verification procedure for a
  future v1.0.0 release is in ``packaging/WINDOWS-SIGNING.md``.
* **No successful CI evidence.** ``.github/workflows/package.yml`` is configured
  for Windows, Linux and both macOS architectures, but no run on the release
  commit completed successfully for this release. Its macOS Intel job also still
  names a retired runner. Nothing here is claimed on the workflow's authority.