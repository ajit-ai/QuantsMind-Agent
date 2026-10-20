# QuantsMind-Agent v0.9.0 -- packaging record

This directory records the v0.9.0 release. The annotated tag `v0.9.0` identifies
the source release commit that carries this directory.

The installer was built from that tagged source after the release commit existed.
The tag was not moved when this artifact metadata was added.

## Release data

| Field | Value |
| --- | --- |
| Version | `0.9.0` |
| Tag | `v0.9.0` |
| Release commit | `b4755060cfaa85f40247507c8cf68486410c2a3d` |
| Artifact | `QuantsMindAgent-0.9.0-x64.msi` |
| Platform | Windows x64 |
| Size | 43,360,256 bytes |
| SHA-256 | `74823186651280cf6c7403bfdf1e016b35fbc2a9a3146aa750e2650f04e6dde6` |
| Installer scope | per-user, no elevation |
| Code signed | no |
| Build status | built and verified |
| Release status | published |
| Released | 2026-10-20 |
| Release | https://github.com/ajit-ai/QuantsMind-Agent/releases/tag/v0.9.0 |
| MSI asset | https://github.com/ajit-ai/QuantsMind-Agent/releases/download/v0.9.0/QuantsMindAgent-0.9.0-x64.msi |

## Build

- Executable: PyInstaller 6.22.3, onedir layout.
- Installer: WiX Toolset 5.0.2+aa65968c.
- Python: 3.14.7, Windows 11 AMD64.

The executable records the source commit `b4755060cfaa85f40247507c8cf68486410c2a3d`,
which is the commit the `v0.9.0` tag peels to.

## Verification

Performed against the artifact recorded here:

- packaged executable smoke test: 14/14 checks passed;
- MSI build checks: 7/7 passed;
- MSI install verification: 23/23 passed -- install, launch, real window,
  reinstall without duplication, uninstall, and the real user profile left
  byte-identical;
- desktop GUI acceptance: 8/8 passed;
- installed window title: `QuantsMind-Agent 0.9.0`;
- published asset: downloaded from GitHub and matched the recorded byte size and
  SHA-256; GitHub reports the same digest.

The installer is unsigned, so Windows SmartScreen will warn on first run.

## Publication

The MSI is a GitHub Release asset; it is not committed to Git. See
`checksums.sha256` for the artifact line in `sha256sum -c` format.
