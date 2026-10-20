# QuantsMind-Agent v0.8.0 -- packaging record

This directory records the v0.8.0 release. The annotated tag `v0.8.0` identifies
the source release commit that carries this directory.

The installer was built from that tagged source after the release commit existed.
The tag was not moved when this artifact metadata was added.

## Release data

| Field | Value |
| --- | --- |
| Version | `0.8.0` |
| Tag | `v0.8.0` |
| Release commit | `74c061b9cf963ec7c791304ba45379727a94d313` |
| Artifact | `QuantsMindAgent-0.8.0-x64.msi` |
| Platform | Windows x64 |
| Size | 43,335,680 bytes |
| SHA-256 | `5c1c3b4e967c19ca70b6455cce11af7b18d14a83ae545b890df5c428ac31a438` |
| Installer scope | per-user, no elevation |
| Code signed | no |
| Build status | built and verified |
| Release status | pending publication |

## Build

- Executable: PyInstaller 6.22.3, onedir layout.
- Installer: WiX Toolset 5.0.2+aa65968c.
- Python: 3.14.7, Windows 11 AMD64.

The executable records the source commit `74c061b9cf963ec7c791304ba45379727a94d313`,
which is the commit the `v0.8.0` tag peels to.

## Verification

Performed against the artifact recorded here:

- packaged executable smoke test: 14/14 checks passed;
- MSI build checks: 7/7 passed;
- MSI install verification: 23/23 passed -- install, launch, real window,
  reinstall without duplication, uninstall, and the real user profile left
  byte-identical;
- desktop GUI acceptance: 8/8 passed;
- installed window title: `QuantsMind-Agent 0.8.0`.

The installer is unsigned, so Windows SmartScreen will warn on first run.

## Publication

The GitHub Release and asset URLs are recorded after publication. The MSI remains
a GitHub Release asset; it is not committed to Git. See `checksums.sha256` for the
artifact line in `sha256sum -c` format.
