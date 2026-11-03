# M3B-S1.1 — Multi-Provider Credential Vault and Secure Authentication Gateway

**Date:** 2026-10-22
**Branch / base:** `release/v1.0.0` @ `6ef59776549f91c46a08ef9f1f85af94dc8ae31a`
**Source version:** `0.9.0` (unchanged) · **Release flags:** inactive (unchanged)

> This document distinguishes **implemented controls** from **proposed future
> controls** throughout. Anything marked *proposed* is not in the code and
> must not be relied on.

---

## 1. Executive summary

Before this milestone the application had exactly one credential slot for
every provider it could talk to. The slot was named after `provider.name`,
and `provider.name` selects the *protocol factory*, which is the constant
`openai_compatible` for OpenAI, OpenRouter, Groq, DeepSeek and every custom
endpoint alike. One slot shared by every service is one key away from being
sent to the wrong service: changing the base URL in Settings was enough to
point an OpenRouter key at `api.openai.com`.

This milestone replaces that with a **provider identity**, a **provider-bound
credential identifier**, a **credential vault** with an explicit lifecycle,
and an **authentication gateway** that decides whether a credential may travel
to a particular endpoint before the credential is read at all.

Implemented:

* a canonical endpoint identity that refuses ambiguous URLs instead of
  guessing;
* a provider identity derived from the endpoint, never from a display name;
* a credential vault over the Windows Credential Manager, refusing to write
  to a plaintext store;
* an authentication gateway with a per-trust-class endpoint policy, a DNS
  check, redirect refusal, and no proxy inheritance for credential-bearing
  routes;
* a structured security audit log that cannot record a secret;
* 103 offline adversarial tests, covering all twenty mandatory scenarios.

Not claimed: protection against malware running as the same Windows user,
protection against a TLS-intercepting proxy the operator installed on
purpose, and complete protection against DNS rebinding, which needs a
transport redesign (M3B-S1.4).

---

## 2. Existing security architecture (as found)

| Area | Finding before this milestone | Evidence |
| --- | --- | --- |
| Credential slot | `provider:{provider.name}:api_key` — one slot for every provider | `config.py` (pre-change `api_key_secret_key`) |
| Credential resolution | environment variable first, then the secret store | `config.py` (pre-change `api_key_value`) |
| Keyring | already used when `keyring` is importable; the file store is the fallback | `secrets.py:154` |
| File store | plaintext JSON in the user config directory, chmod-restricted | `secrets.py:96` |
| Endpoint check | substring match on the base URL for local markers | `config.py` (pre-change `requires_api_key`) |
| Redirects | `httpx.Client(follow_redirects=True)` | `openai_compat.py:159` (pre-change) |
| TLS | `httpx` defaults, so verification was on | `openai_compat.py:156` |
| Proxy | `httpx` default `trust_env=True`, so proxy variables applied | `openai_compat.py:156` |
| Fallbacks | every fallback endpoint received the **primary** endpoint's credential callable | `app.py:428` (pre-change) |
| Command environment | credential-named variables removed from the subprocess environment | `command_tools.py:117` |
| Logging | a redacting filter over the logger | `logging_setup.py:78` |

### 2.1 The credential-flow diagram (as found)

```
Settings dialog --set_api_key--> SecretStore
                                    key = "provider:openai_compatible:api_key"  <-- ONE SLOT
                                         ^            (Windows Credential Manager,
                                         |             or plaintext secrets.json)
                                         |
ConfigManager.api_key_value()
    1. os.environ[provider.api_key_env]   <-- GLOBAL, not bound to a provider
    2. SecretStore.get(one shared slot)
                                         |
                                         v
build_provider(..., api_key=api_key_value)   <-- the same callable for the
                                         |        primary AND every fallback
                                         v
OpenAICompatProvider._headers()
    "Authorization": f"Bearer {key}"          <-- attached to whatever host
                                         |        the base URL names
                                         v
httpx.Client(follow_redirects=True, trust_env=True)  <-- redirects followed,
                                         |           proxy variables honoured
                                         v
                              any host the user typed, over http, to a private
                              address, with a redirect able to move the key
```

### 2.2 The credential-flow diagram (now)

```
ConfigManager / gateway
    |
    v
AuthGateway.authorize(identity, endpoint)      <-- decides BEFORE any read
    |  scheme? destination class? bound endpoint? DNS?
    |  denied --> AuthenticationGatewayError, no secret touched
    v
CredentialVault.get_secret(identity.credential_id)
    |  provider:openrouter:default:api_key  (Windows Credential Manager)
    |  state must be usable
    v
OpenAICompatProvider._headers()
    "Authorization": f"Bearer {value}"
    |
    v
httpx.Client(follow_redirects=False, verify=True, trust_env=False)
    |
    v
the authorised endpoint, and no other
```

---

## 3. Threat model

| # | Threat | Before | Now |
| --- | --- | --- | --- |
| T1 | Key of provider A sent to provider B (shared slot) | possible by changing one URL field | impossible: slots are provider-bound, and the gateway binds a credential to one endpoint |
| T2 | Global environment variable leaks across providers | possible | impossible: variables are provider-specific; the generic one belongs to the default provider |
| T3 | Custom base URL inherits a cloud key | possible | refused: a custom endpoint is not trusted by default and needs an explicit decision |
| T4 | Display-name spoofing (`name = "OpenRouter"` pointing at OpenAI) | the name was cosmetic | the name is never used for ownership; the endpoint decides |
| T5 | Credential sent to a loopback/private/metadata address | possible | refused by destination class for every cloud identity |
| T6 | Scheme downgrade to `http://` | possible | refused for every credential-bearing route |
| T7 | Redirect carries the key to another host | followed | refused and reported; the target is never contacted |
| T8 | Cloud key sent to a local model server | possible | impossible: local identities own different slots, and the local route attaches no cloud credential |
| T9 | Credential in logs, errors, tool results, prompts | mostly controlled | the vault never logs; the audit log refuses secret-bearing fields; tools never receive the vault |
| T10 | Credential-bearing proxy environment | honoured | ignored for credential-bearing routes unless explicitly allowed |
| T11 | Secret written to a plaintext file | possible (fallback store) | the vault refuses to write to an insecure store |
| T12 | DNS rebinding between the check and the connect | possible | **not closed** — see §16 |

---

## 4. Provider identity model

Implemented in `src/quantsmind_agent/provider_identity.py`.

```text
openrouter/default      provider:openrouter:default:api_key
openai/default          provider:openai:default:api_key
groq/default            provider:groq:default:api_key
deepseek/default        provider:deepseek:default:api_key
minimax/default         provider:minimax:default:api_key
ollama/default          provider:ollama:default:api_key
custom/<endpoint-hash>  provider:custom-<12 hex>:default:api_key
```

Resolution order (`resolve_provider_identity`):

1. an **explicit** `provider_id` in the configuration — the only way to bind a
   credential to a service that is not in the known list;
2. the known hosted service whose **canonical endpoint matches exactly**
   (`https://api.openai.com/v1`, not a prefix and not a look-alike host);
3. a known local model server, matched on `host:port`;
4. any other loopback endpoint → a local identity with a hashed name;
5. anything else → a **custom** identity with a hashed name, which needs an
   explicit trust decision.

The display name is carried for the interface and is never consulted for
ownership. A configuration that claims to be OpenRouter while pointing at
OpenAI resolves to the OpenAI identity, and therefore to the OpenAI slot.

### 4.1 Endpoint canonicalisation

`canonicalize_endpoint` refuses, rather than guesses:

| Refused | Why |
| --- | --- |
| a scheme other than `http`/`https` | not a supported transport |
| user information in the URL (`https://user:pass@host`) | a credential in a URL is a credential in a log |
| a percent-encoded host | two spellings of one host are ambiguity |
| an out-of-range port | not a port |
| a host that is neither a strict DNS name nor a valid address | not a host |
| a parent-directory segment in the path | not a path |

The canonical form is stable: `https://API.OpenAI.com:443/v1/` and
`https://api.openai.com/v1` are the same endpoint, and the canonical string
is what appears in audit records.

---

## 5. Endpoint-binding rules

Implemented in `src/quantsmind_agent/auth_gateway.py`.

| Trust class | Scheme | Destination | Extra rule |
| --- | --- | --- | --- |
| `cloud` | `https` only | public host only | the endpoint must be the one the identity is bound to; the name must resolve to public addresses |
| `local` | `http` allowed | loopback only | a LAN model server needs an explicit opt-in |
| `custom` | `https` only | public host only | refused entirely unless custom endpoints are explicitly trusted |

Reason codes are specific and are what the audit log records:
`scheme_downgrade`, `loopback_destination`, `private_destination`,
`link_local_destination`, `metadata_endpoint`, `endpoint_not_bound`,
`provider_not_recognised`, `dns_unresolved`, `dns_resolves_to_non_public`,
`custom_endpoint_not_trusted`, `remote_local_endpoint`.

### 5.1 Ordering guarantee

The endpoint is authorised **before** the credential is read. A refused
endpoint never causes a secret to be loaded into memory, let alone placed on
the wire (`AuthGateway.credential_for`).

---

## 6. Windows secret storage

* The existing `KeyringSecretStore` (Windows Credential Manager through
  `keyring`) is the production backend. `SecretStore.is_secure` now states
  whether a backend is an OS credential store.
* `CredentialVault` **refuses to construct** against an insecure store when
  `require_secure=True`, and refuses to *write* to one even when it was
  constructed with `require_secure=False` (`allow_insecure_writes=False`).
  The application degrades to a read-only vault when no OS credential
  service is present, and says so.
* Credential **metadata** (identifier, provider, endpoint, state) lives in
  `credentials.json` in the user config directory. It contains no values.
* The legacy `secrets.json` file store is still *readable*, so an existing
  installation keeps working, but the vault writes nothing new to it.
* No custom cryptography was introduced. No API key is written to JSON, YAML,
  TOML, SQLite, the log or the application preferences.
* Deletion removes the value and leaves a `revoked` record, so the history
  of what existed is not silently lost.

### 6.1 Credential lifecycle

```
UNCONFIGURED -> CONFIGURED -> ACTIVE -> DISABLED -> (CONFIGURED)
                                |           |
                                |           +--> usable again
                                +--> ROTATED (a new value replaced this one)
                                +--> REVOKED (the value was deleted)
```

| State | The value | May be used |
| --- | --- | --- |
| `unconfigured` | absent | no |
| `configured` | present | yes |
| `active` | present | yes (an explicit marker for "in use") |
| `disabled` | present | **no** — `get_secret` returns `None` |
| `rotated` | present | no, until the rotation completes |
| `revoked` | absent | no |

Disabling a credential therefore makes it unusable at the point of use, not
merely hidden from the Settings screen.

---

## 7. Legacy credential migration

The old shared slot `provider:openai_compatible:api_key` cannot be
*proved* to belong to any one provider: it was written by whichever provider
was active at the time. The design therefore separates three things.

1. **Detection.** `CredentialVault.detect_legacy_credential` reports the
   candidate, the provider it would be bound to, and whether that binding is
   unambiguous. It never reads the value into a log or an error message.
2. **Confirmation.** The migration needs an explicit `confirm` callback. With
   no callback the attempt returns `confirmation_required` and changes
   nothing. The candidate is marked `requires_confirmation` unconditionally,
   because a shared slot is never self-evidently owned.
3. **Execution, in a safe order.**
   * write the value to the provider-bound slot;
   * read it back and compare it with the original;
   * only then remove the legacy slot.

Any failure before the last step leaves the original credential exactly where
it was, and removes the half-written new slot. The original is preserved
until a successful migration is confirmed.

### 7.1 The non-destructive path

`CredentialVault.confirm_legacy_ownership` records that a legacy credential
belongs to a provider identity **without moving or deleting anything**. The
value stays in the old slot and is read through the recorded binding. This
is what an installation needs in order to keep working after the milestone
without an interactive dialog, and it is the only migration step performed
here.

### 7.2 The user-confirmation step is **BLOCKED**

The Settings dialog has no confirmation flow for this decision, and adding
one is a GUI change beyond the scope of this milestone. The foundation is in
place — a `confirm` callback, a `confirmation_required` result, and an audit
record — but the dialog that would ask the question is not. The owner must
confirm a legacy binding out of band (through the vault API) until the GUI
step is built. **This is reported as a blocked item, not as a completed
control.**

### 7.3 The real credential was not touched

No migration was performed against the stored OpenRouter credential during
this milestone. It was neither moved, nor deleted, nor overwritten, and its
value was never read into any output.

---

## 8. Local LLM security

| Requirement | Status |
| --- | --- |
| Ollama, LM Studio, Taksh, llama.cpp, vLLM recognised as local servers | implemented (`LOCAL_PROVIDERS`, matched on `host:port`) |
| Loopback-only default | implemented: a local identity is authorised on loopback only |
| No cloud API key attached to a local request | implemented: local identities own different slots, and the gateway attaches only the identity's own credential |
| No implicit network exposure | implemented: a LAN model server is refused unless `allow_remote_local_models` is set |
| Remote LAN model servers require explicit authorisation | implemented (`allow_remote_local_models`, plus an explicit provider binding) |
| Local models use the existing production permission broker | unchanged: the broker is the tool registry's enforcement point, independent of the provider |
| No automatic fallback from local to a paid cloud model | implemented by construction: a fallback endpoint resolves its own credential, and a local identity has no cloud slot |
| No Taksh startup, installation, training or repository modification | honoured: nothing outside this repository was touched |
| A local model is not treated as trustworthy because it is local | honoured: the local policy is a *destination* policy; it grants no extra tool privilege |

**Residual risk.** A local model server is a program the user chose to run.
It can see the prompts sent to it, and it is not sandboxed by this
application.

---

## 9. Authentication gateway behaviour

The gateway is the only component that may hand a credential to a provider.

```python
decision = gateway.authorize(identity, endpoint)  # 1. decide
if not decision.allowed:
    raise AuthenticationGatewayError(...)  # 2. refuse, touching nothing
value = gateway.credential_for(identity, endpoint)  # 3. read, then attach
```

`AuthGateway.client_options` also constrains the transport:

| Option | Value | Why |
| --- | --- | --- |
| `follow_redirects` | `False` | a redirect would move the prompt and the `Authorization` header to a host the policy never authorised |
| `verify` | `True` | certificate validation is never disabled |
| `trust_env` | `False` for a credential-bearing route | a proxy variable in the environment must not silently become the destination of a request that carries an API key |

The provider adapter reports a redirect as a protocol error rather than
following it (`_refuse_redirect`), so the redirect target is never contacted.

---

## 10. Tool and credential isolation

| Control | Status |
| --- | --- |
| The vault is never exposed as a model tool | implemented: the tool registry is built from the production tool set; the vault is not one of them |
| No tool receives the vault or the secret store | implemented: tools are constructed with a workspace and settings only |
| No credential in tool arguments | implemented: no tool schema has a credential field |
| No credential in agent prompts or tool-result messages | implemented: the credential is read inside the provider's header construction, after the model has answered |
| No credential in the subprocess environment | already implemented: `sanitized_environment` removes the known credential variables; the gateway adds no new ones |
| No credential in generated patches, debug logs or error messages | implemented: the audit log refuses secret-bearing fields; the redacting logger filter is unchanged |
| A model cannot retrieve a secret through a supported interface | implemented and tested: `test_no_tool_schema_or_tool_result_exposes_the_vault` |

### 10.1 The residual risk, stated plainly

**The command tool runs with the same Windows user privileges as the
credential store.** A model that can run `type` or `cat` against the user's
configuration directory can read the *file* fallback store, and it can reach
the Windows Credential Manager through the same user session. The application
does **not** claim secret isolation from a command it has approved.

Closing this needs OS-enforced execution isolation — a low-privilege helper
process, a job object, or a container — and belongs to **M3B-S1.4**. No
unverified sandbox was introduced and labelled secure.

---

## 11. Audit event schema

Implemented in `src/quantsmind_agent/security_audit.py`.

| Event | Meaning |
| --- | --- |
| `credential.created` | a credential was stored, or re-enabled |
| `credential.migration_requested` | a migration or an ownership confirmation was proposed |
| `credential.migration_completed` | the migration finished and the value read back correctly |
| `credential.migration_failed` | the migration failed; the original is untouched |
| `credential.rotated` | a value was replaced |
| `credential.disabled` | a credential was made unusable |
| `credential.deleted` | a value was removed |
| `provider.selected` | an endpoint was chosen |
| `endpoint.authorized` | an endpoint passed the policy |
| `endpoint.denied` | an endpoint failed the policy |
| `authentication.failed` | the provider rejected the credential |
| `security.policy_denied` | a request was refused by policy |

Each record carries: a UTC timestamp, a correlation identifier, the provider
identity, the model where relevant, the **credential identifier** (never the
value), the endpoint identity, the decision and a non-sensitive reason code.

The record has **no field for a secret**, and `record()` refuses any keyword
that names one (`secret`, `value`, `token`, `api_key`, `password`,
`authorization`, `bearer`, …). Free text is passed through the application
redactor.

The log is plain: immutable records in memory plus an optional sink. It is
**not** encrypted and **not** tamper-evident, and this milestone does not
claim otherwise. Encrypted, tamper-evident audit storage belongs to
**M3B-S1.6**.

---

## 12. Security test evidence

`tests/security/test_m3b_s11_credential_security.py` and
`tests/security/test_m3b_s11_config_and_wiring.py` — **103 tests, all
passing, all offline**.

Every credential in these tests is a fake canary
(`sk-canary-openrouter-0000000000` and similar). HTTP is served by an
in-process `httpx` mock transport. Name resolution is injected, so no DNS
query is ever issued. No live provider API is contacted.

| # | Mandatory scenario | Test |
| --- | --- | --- |
| 1 | An OpenRouter key cannot be used for OpenAI | `test_an_openrouter_key_cannot_be_used_for_openai` |
| 2 | An OpenAI key cannot be used for OpenRouter | `test_an_openai_key_cannot_be_used_for_openrouter` |
| 3 | A custom endpoint cannot inherit another provider's key | `test_a_custom_endpoint_cannot_inherit_another_providers_key` |
| 4 | Provider display-name spoofing fails | `test_a_display_name_spoof_does_not_win_the_credential` |
| 5 | A credential-bearing cross-host redirect is rejected | `test_a_cross_host_redirect_is_refused` |
| 6 | An HTTP downgrade is rejected | `test_an_http_downgrade_is_refused` |
| 7 | An unauthorised localhost destination is rejected | `test_a_localhost_destination_is_refused_for_a_cloud_credential` |
| 8 | A private-network destination is rejected under cloud policy | `test_a_private_destination_is_refused_under_cloud_policy` |
| 9 | Metadata endpoint access is rejected | `test_a_metadata_endpoint_is_refused` |
| 10 | Invalid URL normalisation fails closed | `test_malformed_endpoints_are_refused` |
| 11 | A local no-auth provider receives no cloud key | `test_a_local_provider_receives_no_cloud_key` |
| 12 | Credential deletion affects only the intended provider | `test_deletion_affects_only_the_intended_provider` |
| 13 | Rotation does not corrupt other credentials | `test_rotation_does_not_corrupt_other_credentials` |
| 14 | Legacy migration preserves the original on failure | `test_a_failed_migration_preserves_the_original_credential` |
| 15 | Logs and exceptions redact fake secret canaries | `test_an_audit_record_refuses_to_carry_a_secret`, `test_an_exception_does_not_disclose_the_credential` |
| 16 | Tool results cannot expose vault contents | `test_no_tool_schema_or_tool_result_exposes_the_vault` |
| 17 | Provider fallback cannot bypass credential policy | `test_a_fallback_endpoint_cannot_borrow_the_primary_credential` |
| 18 | Missing credentials produce safe errors | `test_a_missing_credential_produces_a_safe_actionable_error` |
| 19 | Existing provider presets still resolve correctly | `test_the_provider_presets_still_resolve` |
| 20 | Existing permission-broker behaviour is unchanged | `test_the_permission_broker_still_asks_before_a_write` |

Additional coverage: redirect refusal at the transport, DNS classification
(including the NAT64 case), a disabled credential being unusable, an
unconfirmed migration doing nothing, the vault refusing a plaintext store,
the generic environment variable not crossing providers, and the M3B-1B
OpenRouter tool-call and `DENY` regression.

### 12.1 A finding the tests produced

The first version of the DNS check classified every resolved address with
`ipaddress`. Python classifies `64:ff9b::/96` (NAT64) and `::ffff:0:0/96`
(IPv4-mapped) as **private**, so on this machine — where `api.openai.com`
and `openrouter.ai` publish AAAA records — the gateway refused every
provider with `dns_resolves_to_non_public`. The check now extracts the
embedded IPv4 address from an IPv6 transition address and judges that
instead (`_embedded_ipv4`). The regression test
`test_a_nat64_address_is_judged_by_the_ipv4_it_carries` pins it.

---

## 13. Compatibility findings

| Finding | Effect | Classification |
| --- | --- | --- |
| The credential slot is now provider-bound | an existing installation must confirm the legacy binding once, or re-enter the key | **intended behaviour change** |
| `QM_API_KEY` now belongs to the default provider only | a user who pointed the application at a non-OpenAI provider and relied on `QM_API_KEY` must use that provider's variable | **intended behaviour change** |
| A custom endpoint is refused unless explicitly trusted | the "Custom endpoint" preset needs `trust_custom_endpoint = true` | **intended behaviour change** |
| `host.docker.internal` is no longer treated as local by a substring match | it needs an explicit binding as a local model server | intended, reported |
| Redirects are no longer followed | a provider that redirects (a proxy in front of an API, for example) now fails with a clear message | intended, reported |
| `httpx` no longer inherits proxy variables for a credential-bearing route | a user behind a corporate proxy needs the explicit opt-in | intended, reported |
| `requires_api_key` is decided by the canonical endpoint | the same three local URLs behave exactly as before | verified unchanged |

**No existing provider preset stopped resolving.** The preset table, the
capability lookup by base URL (the M3B-1A change), the capability overrides
and the local-endpoint rule all still work, and are covered by
`test_the_provider_presets_still_resolve` and
`test_local_presets_still_need_no_credential`.

One existing test was **updated** rather than weakened:
`tests/integration/test_cli.py::test_output_never_contains_a_credential`
asserted that the legacy shared slot alone authenticates the configured
provider. That is precisely the behaviour this milestone removes, so the test
now asserts the opposite (the legacy slot alone does *not* resolve) and a
new test asserts that a provider-bound credential *is* reported masked. The
security property the test was written for — that the raw value never appears
in the output — is unchanged and still asserted.

---

## 14. Known residual risks

| # | Risk | Why it is not closed here | Owner |
| --- | --- | --- | --- |
| R1 | Malware running as the same Windows user can reach the credential store and the secrets the model can read from disk | the application runs as the user; isolation needs a different process identity | M3B-S1.4 |
| R2 | An approved command can read the plaintext fallback store | same as R1 | M3B-S1.4 |
| R3 | DNS rebinding between the authorisation check and the TCP connect | the check resolves the name; `httpx` resolves it again when it connects. The window is small but real. Closing it needs a transport that pins the address | M3B-S1.4 |
| R4 | A TLS-intercepting proxy the operator installed on purpose sees the traffic | the application cannot distinguish a trusted interception from a hostile one | operator decision |
| R5 | A local model server is not sandboxed | out of scope; the local server is a program the user chose | M3B-S1.4 |
| R6 | The audit log is neither encrypted nor tamper-evident | not implemented here | M3B-S1.6 |
| R7 | The Settings dialog has no confirmation flow for a legacy credential | GUI work, out of scope for this milestone | M3B-S1.2 |
| R8 | Provider-side revocation is not performed | deleting a local credential is not revoking a key at the provider | operator action, needs a provider API |
| R9 | Split-horizon DNS can resolve a public name to an internal address, which the check then refuses | the check is deliberately strict; an override would weaken it | operator decision |
| R10 | OAuth, AWS, Azure, Google Cloud identity, mTLS and enterprise SSO are not implemented | no production support existed to build on safely | M3B-S1.6 |

**The desktop credential vault does not protect against malware with full
access to the same Windows user account.** The Credential Manager stores the
key under the user's profile; any program running as that user can ask for
it. This milestone reduces the *blast radius* of a wrong endpoint and of a
mis-typed base URL. It does not reduce the blast radius of malware, and it
does not claim to.

---

## 15. Follow-up milestones

| Milestone | Scope | Why it is needed |
| --- | --- | --- |
| **M3B-S1.2** | Settings-dialog confirmation for a legacy credential; provider identity selection in the UI; audit-log viewer | the confirmation flow and the operator-facing view of the new model are missing |
| **M3B-S1.3** | Enterprise authentication interface (an abstract `AuthenticationStrategy` seam) | the gateway already has one place where a strategy would plug in |
| **M3B-S1.4** | OS-enforced execution isolation for command tools; an address-pinning transport | closes R1, R2, R3 and R5 |
| **M3B-S1.5** | Secret rotation automation and scheduled re-authentication | rotation exists as an operation; automation does not |
| **M3B-S1.6** | Encrypted, tamper-evident audit storage; OAuth, cloud identity, mTLS and SSO | closes R6 and R10 |

---

## 16. What would have to change to close DNS rebinding

The gateway resolves the destination name and checks every address it
resolves to. `httpx` then resolves the name *again* when it opens the
connection. Between those two moments a hostile resolver can answer
differently, and the credential follows the second answer.

Closing this needs the transport to connect to an address the application
chose, not to a name it re-resolved: a resolver that returns the already
checked addresses, or a pinned transport that connects to a validated
address and checks the certificate against the original name. That is a
transport redesign and is out of scope here. Until then the check is a
meaningful barrier (it refuses a *statically* private destination) and is
**not** claimed to be complete.

---

## 17. File inventory

| File | Change | Purpose |
| --- | --- | --- |
| `src/quantsmind_agent/provider_identity.py` | **new** | provider identity, endpoint canonicalisation, trust classes |
| `src/quantsmind_agent/credential_vault.py` | **new** | the vault, the credential lifecycle, the legacy migration |
| `src/quantsmind_agent/auth_gateway.py` | **new** | endpoint authorisation, credential attachment, transport constraints |
| `src/quantsmind_agent/security_audit.py` | **new** | the structured, secret-free audit record |
| `src/quantsmind_agent/config.py` | modified | provider identity on the settings; provider-bound credential resolution; per-provider environment variables; endpoint-based `requires_api_key` |
| `src/quantsmind_agent/secrets.py` | modified | `SecretStore.is_secure` |
| `src/quantsmind_agent/paths.py` | modified | `credentials_file` (metadata, no values) |
| `src/quantsmind_agent/providers/__init__.py` | modified | `follow_redirects` and `trust_env` on the factory |
| `src/quantsmind_agent/providers/openai_compat.py` | modified | redirect refusal, `trust_env`, explicit `verify=True` |
| `src/quantsmind_agent/app.py` | modified | the gateway owns credential release; every endpoint resolves its own credential |
| `tests/security/test_m3b_s11_credential_security.py` | **new** | the twenty mandatory adversarial scenarios |
| `tests/security/test_m3b_s11_config_and_wiring.py` | **new** | configuration and application-wiring security |
| `tests/integration/test_cli.py` | modified | one test updated to the new legacy-slot behaviour, one added |
| `docs/security/M3B-S1.1_MULTI_PROVIDER_CREDENTIAL_SECURITY.md` | **new** | this document |

---

## 18. Final qualification disposition

| Gate | Requirement | Verdict |
| --- | --- | --- |
| S11-G1 | Baseline and protected artifacts preserved | **PASS** |
| S11-G2 | Credential-flow and threat audit complete | **PASS** |
| S11-G3 | Provider identity isolation | **PASS** |
| S11-G4 | Secure Windows credential storage | **PASS** |
| S11-G5 | Safe legacy credential migration | **PARTIAL** — the foundation and the non-destructive path are implemented; the GUI confirmation step is blocked |
| S11-G6 | Endpoint-bound authentication | **PASS** |
| S11-G7 | Redirect and network policy | **PARTIAL** — redirects, schemes, destinations and proxy inheritance are closed; DNS rebinding needs a transport redesign |
| S11-G8 | Local model authentication separation | **PASS** |
| S11-G9 | Secret-safe logs and errors | **PASS** |
| S11-G10 | Agent/tool credential boundary | **PASS** for the model-facing surface; the same-user privilege limitation is recorded as R1/R2 |
| S11-G11 | Credential lifecycle | **PASS** |
| S11-G12 | Security audit events | **PARTIAL** — the events, the schema and the redaction are implemented and tested; encryption and tamper-evidence are M3B-S1.6 |
| S11-G13 | Adversarial offline tests | **PASS** — 103 tests, all twenty scenarios, all offline |
| S11-G14 | Existing provider regressions | **PASS** — presets, capabilities and local endpoints unchanged; full suite green |
| S11-G15 | Existing permission-broker regressions | **PASS** — the broker is untouched; the M3B-1B `DENY` behaviour is re-asserted |
| S11-G16 | Documentation and final preservation | **PASS** |

**Disposition: `M3B-S1.1 QUALIFIED — CORE CREDENTIAL AND ENDPOINT SECURITY`**

The two PARTIAL gates and the one recorded limitation are stated above, are
owned by named follow-up milestones, and are not represented as implemented
controls. No live provider API was contacted, no credential was moved or
deleted, and the release authorisation was not touched.
