# Windows code signing (manual release procedure)

This guide defines the Windows signing prerequisite for a public v1.0.0
release. It is a setup and verification procedure, **not** evidence that any
current artifact is signed. Signing is performed manually on a trusted release
machine; no CI signing is configured or planned.

## Current status (2026-11-18)

| Item | Result |
| --- | --- |
| Published v0.9.0 MSI and executable | unsigned (`NotSigned`) |
| QF-001 / G25 (signing and trusted publisher) | **open — release blocker** |
| Signing tooling in this repository | present (`scripts/signing.py`, `scripts/verify_windows_signature.ps1`) |
| CI signing workflow | none; manual signing only |
| Production code-signing certificate | not available to the repository |
| Self-signed certificate | not acceptable release evidence |

A SHA-256 published over a trusted channel detects accidental substitution; it
does **not** authenticate the publisher and is not a substitute for a code
signature. SmartScreen and security-software warnings are expected for unsigned
builds.

## Scope

Windows-only v1.0.0. macOS artifacts are not part of the v1.0.0 release and no
notarization is performed here.

## What must be signed, and in what order

| Artifact | When | Why |
| --- | --- | --- |
| `QuantsMindAgent.exe` | after PyInstaller, before building the MSI | gives the launched application a publisher identity |
| `QuantsMindAgent-<version>-x64.msi` | after WiX builds the MSI | gives the distributable a publisher identity |
| first-party helper `.exe`/`.dll` files, if added later | before packaging | only files owned by this project |
| bundled Python, Qt, OpenSSL and VC runtime binaries | normally not | vendor binaries; do not re-sign them for a green checklist |

The order matters:

```text
build onedir
  -> sign QuantsMindAgent.exe
  -> build MSI (it contains the signed executable)
  -> sign MSI
  -> verify both signatures
  -> calculate SHA-256 of the signed MSI
  -> write the packaging record
  -> publish
```

Signing the MSI after the executable is embedded is required: signing the
executable afterwards would leave an unsigned binary inside the signed package
or change the binary without updating the package.

## Certificate and tool prerequisites

- A **publicly trusted** code-signing certificate with a non-exportable private
  key (HSM, USB token or cloud signing service). A self-signed certificate is
  for local testing only and is never release evidence.
- The certificate's 40-hex SHA-1 thumbprint.
- An RFC 3161 timestamp URL from the CA (HTTPS, e.g. `http://timestamp.digicert.com`
  where the CA permits HTTP — the repository tooling requires an absolute HTTPS
  URL for `QM_SIGNING_TIMESTAMP_URL`; use the HTTPS endpoint supplied by the CA).
- `signtool.exe` from the Windows SDK, or `QM_SIGNTOOL` pointing to it.

Never place a private key, `.pfx`, `.p12`, password, token or signing code in
the repository, an issue, a pull request or a log. The certificate subject is
public; the key is not.

## Using the repository tooling

The tooling refuses to select a certificate implicitly:

```powershell
$env:QM_SIGNING_CERT_SHA1       = '<40-hex thumbprint>'
$env:QM_SIGNING_TIMESTAMP_URL   = 'https://<ca-timestamp-endpoint>'
# optional: $env:QM_SIGNTOOL = 'C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe'

python scripts/signing.py check                  # validate configuration
python scripts/signing.py sign <artifact.exe>    # sign with SHA-256 + RFC 3161 timestamp
python scripts/signing.py verify <artifact.exe>  # signtool verify /pa /all /v
```

`signtool sign` is invoked with `/fd SHA256 /tr <url> /td SHA256 /sha1 <thumbprint>`.
The sign command exits non-zero if the thumbprint is absent or malformed, the
timestamp URL is not an absolute HTTPS URL, or `signtool` cannot be found.

Manual equivalent, if the release engineer signs outside the helper:

```bat
signtool sign /fd SHA256 /tr https://<ca-timestamp-endpoint> /td SHA256 /sha1 <THUMBPRINT> <FILE>
```

For a machine certificate store add `/sm`. For a token or HSM, follow the
provider's instructions; the private key must remain in the hardware.

## Fail-closed verification

Signing command success is not proof of a valid signature. After signing both
artifacts, verify them:

```powershell
Get-AuthenticodeSignature -LiteralPath <FILE> |
  Format-List Status, StatusMessage, SignerCertificate, TimeStamperCertificate

signtool verify /pa /all /v <FILE>

powershell -NoProfile -File scripts/verify_windows_signature.ps1 `
  -Path <FILE> -ExpectedThumbprint $env:QM_SIGNING_CERT_SHA1 -RequireTimestamp
```

The verifier is read-only. It checks `Status == Valid`, the signer thumbprint,
the signer subject and the presence of a trusted timestamp, and exits non-zero
on any failure. Accept an artifact only when **all** of these hold:

- `Get-AuthenticodeSignature` reports `Valid`;
- `signtool verify /pa /all` exits 0;
- the signer thumbprint matches the intended certificate;
- the timestamp is present and trusted;
- both the executable and the MSI are signed;
- the SHA-256 in the packaging record was computed **after** signing.

Stop the release on a failed verification. Do not upload, hash for publication,
or create a GitHub Release after a verification failure.

## Release integrity

- Do **not** re-sign or replace the published v0.9.0 MSI or reuse its tag.
  A signed build is a new version with a new tag and artifact.
- Record the signer subject, thumbprint, timestamp service and verification
  output in `packaging/v1.0.0/` as part of release finalization.
- Keep the publisher identity consistent across the certificate, MSI
  manufacturer, executable metadata and release notes.

## Owner actions required

1. Supply a publicly trusted certificate and its thumbprint, plus the CA's
   HTTPS RFC 3161 timestamp URL.
2. Sign the executable, build the MSI, then sign the MSI on the release machine.
3. Run the fail-closed verification on both artifacts and keep the output.
4. Compute and record SHA-256 after signing; complete `packaging/v1.0.0/`.
5. Only then tag and publish. If no trusted signing path is available, stop
   before production publication; do not publish an unsigned artifact as signed.
