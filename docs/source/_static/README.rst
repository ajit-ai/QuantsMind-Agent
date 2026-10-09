This directory is declared in ``docs/source/conf.py`` as ``html_static_path``.

Git cannot track an empty directory, so without a tracked file here a fresh
checkout has no ``_static`` at all and Sphinx emits ``WARNING:
html_static_path entry '_static' does not exist``. The documentation build
treats warnings as errors, so the job fails on any machine that does not
already have the directory lying around from a previous build.

Add custom CSS or images here; they are copied into the HTML output as-is.
