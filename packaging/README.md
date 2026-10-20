# Packaging history

This directory holds the build support files for every package format, and the
permanent, version-specific record of every published release.

The record starts with the historical v0.7.0 baseline and becomes mandatory from
v0.8.0 onward.

## Policy

1. Every release receives its own `packaging/vX.Y.Z/` directory.
2. A release record is immutable once published. It is never overwritten or
   reused for another version.
3. MSI binaries remain GitHub Release assets. They are not committed to Git.
4. Git stores the release metadata and SHA-256 checksums; the artifact itself
   lives in the GitHub Release.
5. Previous packaging records are never modified by a later release.
6. Historical records contain only verified information. A value that cannot be
   verified is left out, not guessed.
7. Every future release creates its packaging record as part of release
   finalization.
8. A release is not fully finalized until its packaging record and the published
   GitHub artifact agree.

## Layout

```text
packaging/
  README.md
  v0.7.0/
    README.md
    BUILD-METADATA.json
    checksums.sha256
  v0.8.0/
    README.md
    BUILD-METADATA.json
    checksums.sha256
  v0.9.0/       # future
  v1.0.0/       # future
```

Each release directory contains:

- `README.md` -- what the release was, and how its facts were verified;
- `BUILD-METADATA.json` -- the same facts in structured form;
- `checksums.sha256` -- the artifact line in `sha256sum -c` format.

## Release sequence

```text
source
  -> release commit
  -> annotated tag
  -> packaging/vX.Y.Z/
  -> build artifact
  -> artifact verification
  -> GitHub Release
  -> published artifact verification
```

The annotated tag identifies the source release commit. Packaging metadata is
completed in a follow-up commit after the artifact exists, because a checksum
cannot be known before the artifact is built. The tag is never moved to that
follow-up commit.

## Historical baseline

`packaging/v0.7.0/` is a historical record reconstructed when this policy was
introduced in v0.8.0. It describes the already-published v0.7.0 release; it does
not claim that the v0.7.0 artifact was produced by this mechanism.
