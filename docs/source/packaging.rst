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

Version 0.2.0 has no paid licence, no subscription, no telemetry and no
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
     - verified
     - Executable folder and MSI, both built and launched
   * - Linux (glibc)
     - x86_64
     - unverified
     - Build machinery exists; never run on Linux
   * - macOS
     - not built
     - not built
     - No native build machine available
   * - FreeBSD
     - not built
     - not built
     - No native build machine available

"Verified" means: built from source on a machine of that OS, installed the way a
user would, launched, and confirmed to render a window and shut down cleanly.
"Unverified" means the build machinery exists but nobody has run the result on
that OS, so no claim is made about it.

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
    python scripts/build_executable.py --distpath artifacts/v0.2.0/<platform>/onedir

    # Windows Installer (Windows)
    python scripts/build_msi.py --onedir artifacts/v0.2.0/windows/onedir

    # Debian package (Debian/Ubuntu)
    python scripts/build_deb.py --onedir artifacts/v0.2.0/ubuntu/onedir

    # Disk image (macOS)
    python scripts/build_dmg.py --app artifacts/v0.2.0/macos/QuantsMindAgent.app

    # FreeBSD package (FreeBSD)
    python scripts/build_freebsd_pkg.py --onedir artifacts/v0.2.0/freebsd/onedir

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

    python scripts/record_artifacts.py --artifacts artifacts/v0.2.0 \
        --commit "$(git rev-parse HEAD)" --version 0.2.0

That writes ``MANIFEST.json`` (the whole matrix, including platforms that were
**not** built, so an absent platform cannot be mistaken for a forgotten one) and
``SHA256SUMS``, which ``sha256sum -c`` can check.

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

    msiexec /i QuantsMindAgent-0.2.0-x64.msi

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

    sudo dpkg -i quantsmind-agent_0.2.0_amd64.deb

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

    sudo pkg install -y ./quantsmind-agent-0.2.0.pkg
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
    python scripts/smoke_test_executable.py --distpath artifacts/v0.2.0/windows/onedir

    # Launches the real executable on a real display and checks for a window
    python scripts/gui_acceptance.py --distpath artifacts/v0.2.0/windows/onedir

    # Installs, launches, reinstalls and uninstalls the MSI, and proves that the
    # real user profile is byte-identical afterwards
    powershell -File scripts/verify_msi_install.ps1 -Msi artifacts/v0.2.0/windows/msi/QuantsMindAgent-0.2.0-x64.msi

Known issues and unverified platforms
=====================================

* **Linux: built but not launched.** The Windows bundle is smoke-tested, launched
  and screenshotted here. No Linux build has been run on a Linux machine, so this
  documentation makes no claim about it beyond "the build machinery exists".
* **macOS: not built.** No macOS machine was available. The ``.app`` bundle is
  produced by the existing spec and the ``.dmg`` builder is implemented, but
  neither has been executed.
* **FreeBSD: not built.** PyInstaller does not publish a FreeBSD bootloader, so
  a FreeBSD bundle needs a route that is verified on FreeBSD rather than a
  cross-compiled Linux build. No claim is made that the Linux artifact runs there.
* **Nothing is code-signed.** No certificate is available on the build machine, so
  Windows SmartScreen and macOS Gatekeeper will both warn on first run. Signing
  needs a real certificate; an invented signature would be worse than none.
* **No CI evidence.** The packaging workflow exists in
  ``.github/workflows/package.yml`` for Windows, Linux and both macOS
  architectures, but it has not been run for this milestone, so nothing here is
  claimed on its authority.