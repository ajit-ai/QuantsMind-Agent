# M3B-S1.2 — Secure Provider Settings, Credential Migration and Security Audit UI

**Date:** 2026-10-22
**Branch / base:** `release/v1.0.0` @ `6ef59776549f91c46a08ef9f1f85af94dc8ae31a`
**Source version:** `0.9.0` (unchanged) · **Release flags:** inactive (unchanged)
**Builds on:** M3B-S1.1 (uncommitted, unstaged, preserved)

> Implemented controls and proposed controls are distinguished throughout.
> Manual GUI behaviour that was not exercised by an automated test is marked
> as such and is not claimed as qualified.

---

## 1. Executive summary

M3B-S1.1 made credentials provider-bound and endpoint-authorised, and it
deliberately stopped reusing the legacy shared credential. That left the
desktop application unable to authenticate an existing installation without
an operator action, and no interface through which the action could be
taken. This milestone closes that gap.

What was added:

* a **security controller** (`gui/security_controller.py`) that holds every
  rule about providers, credentials, migration and endpoint status, with no
  Qt dependency — so the rules can be qualified headlessly and behave
  identically whichever surface asks;
* a **security panel** (`gui/security_panel.py`) that renders the provider
  catalogue, the credential status of each identity, the endpoint
  classification, the legacy-migration confirmation, and the audit trail;
* a **Security…** entry in the sidebar and a **Security** tab in Settings;
* a **migration confirmation flow** that requires the operator to name the
  provider the legacy credential belongs to, and that never assumes it;
* **67 new offline tests** (42 security, 25 headless GUI).

The security gateway remains the final authority. The interface asks it
about an endpoint and renders the answer; it never decides for itself, and
it cannot turn a denial into an approval.

---

## 2. Repository baseline

Verified at the start of the milestone and again at the end.

| Item | Expected | Observed |
| --- | --- | --- |
| Branch | `release/v1.0.0` | `release/v1.0.0` |
| HEAD | `6ef59776549f91c46a08ef9f1f85af94dc8ae31a` | identical, 0 new commits |
| Source version | `0.9.0` | `0.9.0` |
| `source_is_target` | `false` | `false` |
| `distribution_authorized` | `false` | `false` |
| Published MSI | `7482318…dde6` | unchanged |
| Internal candidate MSI | `28a83f9…3e04` | unchanged |
| Qualification evidence | 11 trees intact | intact |
| `dist-onefile/`, `dist-v02-manual/` | untracked, untouched | 2 and 915 files |

**M3B-S1.1 inventory (verified present before anything was changed):**

| File | Role |
| --- | --- |
| `src/quantsmind_agent/provider_identity.py` | provider and endpoint identity |
| `src/quantsmind_agent/credential_vault.py` | the vault and the lifecycle |
| `src/quantsmind_agent/auth_gateway.py` | endpoint authorisation |
| `src/quantsmind_agent/security_audit.py` | the audit record |
| `docs/security/M3B-S1.1_MULTI_PROVIDER_CREDENTIAL_SECURITY.md` | the M3B-S1.1 design record |
| `tests/security/test_m3b_s11_credential_security.py` | the M3B-S1.1 adversarial suite |
| `tests/security/test_m3b_s11_config_and_wiring.py` | the M3B-S1.1 wiring suite |

The seven M3B-S1.1 modified files (`app.py`, `config.py`, `paths.py`,
`providers/__init__.py`, `providers/openai_compat.py`, `secrets.py`,
`tests/integration/test_cli.py`) were all present and were extended rather
than replaced.

---

## 3. Existing UI architecture

| Element | Where | Notes |
| --- | --- | --- |
| Framework | PySide6 (`QtWidgets`) | the only GUI framework in the project; none was added |
| Main window | `gui/main_window.py` | `QMainWindow` with a `Sidebar` and a `QStackedWidget` (Chat / Task views) |
| Sidebar | `gui/sidebar.py` | workspace, model, status and session boxes; emits Qt signals |
| Settings dialog | `gui/settings_dialog.py` | a modal `QDialog` with Provider / Agent / About tabs |
| Approval dialog | `gui/approval.py` | the permission-broker prompt |
| Background work | `gui/worker.py` | the agent turn runs off the GUI thread |
| Styling | `gui/style.py` | one theme, applied through `apply_theme` |
| Headless tests | `tests/gui/conftest.py` | Qt's `offscreen` QPA, one shared `QApplication` |

The existing conventions were followed: a widget module per surface, Qt
signals for cross-widget events, `get_logger` for logging, and
`pytest.importorskip("PySide6.QtWidgets")` plus `pytest.mark.gui` for the
tests.

### 3.1 How the interface meets the security modules

| Module | How the interface uses it | Never done |
| --- | --- | --- |
| `provider_identity.py` | the controller resolves the identity of the configured endpoint and of every catalogue entry | the interface never decides an identity from a display name |
| `credential_vault.py` | the controller performs every credential operation through the vault | the interface never reads a credential value except through `get_secret`, and never renders one |
| `auth_gateway.py` | the controller asks the gateway to classify the endpoint and nothing else | the interface never re-implements a security rule |
| `security_audit.py` | the controller records provider selections; the vault records lifecycle changes | the interface never writes an audit record by hand |
| `config.py` | the controller reads and writes the configuration | the interface never writes the configuration file directly |
| `secrets.py` | the vault owns the store; the interface only reports *whether* secure storage is available | the interface never touches the store |

---

## 4. Security integration

The controller is constructed by `ApplicationContext.security_controller()`
with a gateway that **does not resolve names**:

```python
gateway = AuthGateway(
    vault=self.credential_vault,
    audit=self.security_audit,
    check_dns=False,  # no network on the GUI thread
    trust_custom_endpoints=lambda: config.provider.trust_custom_endpoint,
    allow_remote_local_models=lambda: config.provider.allow_remote_local_models,
)
```

Two consequences worth stating plainly:

* **The interface performs no network operation.** Name resolution is the
  only part of the policy that can block, and it is the part the interface
  leaves out. The authoritative check — the one that includes resolution —
  still runs in the request path, and it is the one that decides.
* **The trust decisions are callables, not flags.** The operator's decision
  about a custom endpoint lives in the configuration and can change while
  the application runs. A boolean captured at construction time would have
  made the interface's own decision ineffective at request time; that was a
  real defect, found by the new tests, and it is fixed.

The request-time gateway in `ApplicationContext` consults the same two
configuration fields, so the status the interface shows and the decision the
request path reaches come from one source.

---

## 5. Provider settings workflow

The **Providers & credentials** tab opens with a table whose columns are the
things that must not be confused with one another:

| Column | Meaning |
| --- | --- |
| Identity | the provider identity the credential belongs to (`openrouter`, `ollama`, …) |
| Display name | presentation only; it never decides anything |
| Endpoint | the base URL of the option |
| Authentication | `API key (bearer token)` or `None (local model server)` |
| Credential | the credential status of *that* identity |
| Endpoint status | the classification of the endpoint, for the current identity |

The catalogue offers OpenRouter, OpenAI, Groq, DeepSeek, MiniMax, Ollama,
LM Studio, llama.cpp and vLLM. **Taksh is not offered**, because the current
provider architecture has no Taksh entry and this milestone does not change
that; nothing in the Taksh repository was started, installed or modified.

Choosing an identity and pressing **Use this provider** sets the base URL,
the model and `provider.provider_id`. It does **not** touch a credential:
the previous provider's credential stays where it was, under its own
identity, and the new provider starts with *not configured*.

Two new configuration fields carry the security decisions, and both round
trip through the existing `config.toml`:

```toml
[provider]
base_url = "https://openrouter.ai/api/v1"
provider_id = "openrouter"          # the identity the credential belongs to
trust_custom_endpoint = false       # an explicit trust decision
allow_remote_local_models = false   # a LAN model server needs permission
```

A configuration written before this milestone has neither field, loads
unchanged, and resolves its identity from the endpoint exactly as before.

---

## 6. Credential management workflow

The **Credential** group offers: save (or replace), disable, re-enable and
remove, plus a status line and a masked description of what is stored.

| Requirement | How it is met |
| --- | --- |
| The secret is never displayed | there is no reveal control, and the status line carries a status word, not a value |
| The input is masked | `QLineEdit.EchoMode.Password` |
| The input is never pre-filled | the field is only ever written by the operator; `load` never populates it |
| No clipboard use | the panel never calls `QApplication.clipboard()` |
| The input is not logged | the panel logs nothing about the field, and the application logger redacts credential-shaped text |
| The input is cleared | after a save, after a failure, and when the panel or dialog closes |
| The vault is the store | every write goes through `CredentialVault`, which refuses a plaintext backend |

The status vocabulary is the one the milestone asked for:

| Status | Meaning |
| --- | --- |
| Not configured | no credential is stored for this identity |
| Configured | a credential is stored, but for a provider that is not the current one |
| Active | a credential is stored for the current provider identity and is usable |
| Disabled | a credential is stored but is deliberately unusable |
| Migration required | a legacy credential exists and this provider does not have one yet |
| Secure storage unavailable | the system has no OS credential store, so nothing can be stored |

**A credential is never labelled "validated".** Nothing in this milestone
performs a live authentication, so the interface does not claim that a
credential works; it says what the vault knows.

**Failure behaviour.** When the OS credential store is unavailable, the
vault refuses to write, the status reads *Secure storage unavailable*, and
the existing credential is left exactly where it was. The application does
not fall back to plaintext storage, and it does not create a partially
configured provider.

---

## 7. Legacy migration confirmation

This is the operational heart of the milestone. The flow is:

```
the interface detects a credential in provider:openai_compatible:api_key
        |
        v
the "Legacy credential" group appears, explaining what was found
        |
        v
the operator CHOOSES the provider it belongs to   (nothing is preselected)
        |
        v
the operator presses "Confirm migration" and answers the confirmation
        |
        v
the vault copies the value, reads it back, compares it, and only then
removes the source
        |
        v
the status becomes "Active"; a sanitized audit event is recorded
```

| Restriction | How it is enforced |
| --- | --- |
| Never assume the legacy key belongs to OpenRouter | the provider combo starts with no selection; the confirm button is disabled until one is chosen |
| No migration on startup | the controller only *detects*; it never migrates without `confirm_migration` being called |
| No migration because a provider was selected | `apply_provider` touches the configuration only; the test `test_nothing_is_migrated_without_an_explicit_action` pins this |
| No migration when the user cancels | cancelling means the call is never made; the preview is read-only |
| The source is not deleted before verification | the vault writes, reads back, compares, and only then removes the source |
| A failure preserves the source | the vault removes the half-written destination and leaves the source; `test_a_failed_migration_preserves_the_source` pins this |
| An existing destination is not overwritten | when the destination already holds a credential there is no candidate at all, so nothing can be overwritten |
| No live request during migration | the flow touches only the local store; the test suite refuses every socket connection |

The dialog text states that the credential has *not been used, moved or
deleted* before the operator acts, and the panel never mentions the key.

**The owner's real credential was not migrated.** No migration was executed
against the stored OpenRouter credential during this milestone, and its
value was never read into any output. The manual guide in §15 describes how
the owner performs the action.

---

## 8. Endpoint authorization behaviour

The status is produced by the production gateway and mapped to a label. The
mapping adds no rules of its own; it only names what the gateway decided.

| Label | Gateway reason |
| --- | --- |
| Approved cloud endpoint | the identity is bound to this endpoint and the destination is public |
| Approved loopback endpoint | the identity is a local model server on this machine |
| Custom endpoint requiring authorization | `custom_endpoint_not_trusted` |
| Blocked insecure endpoint | `scheme_downgrade` |
| Blocked private-network destination | `private_destination`, `loopback_destination`, `link_local_destination`, `metadata_endpoint`, `remote_local_endpoint`, `dns_resolves_to_non_public`, `dns_unresolved` |
| Invalid endpoint configuration | the URL could not be canonicalised |

A **Trust this custom endpoint** control appears only for a custom endpoint,
and it asks first, showing the canonical endpoint identity, the HTTPS
requirement, the credential identity that would be used and the
implications. The decision is written to `provider.trust_custom_endpoint`,
so it survives a restart and is visible in the configuration file.

During this milestone the gateway's *reason codes* were improved so that the
interface can say something specific: a custom endpoint on a private address
is now reported as a private destination rather than as merely untrusted.
The decision itself did not change — it was denied before and it is denied
now.

---

## 9. Audit viewer

The **Security audit** tab renders the records the existing audit
infrastructure produces.

| Column | Source |
| --- | --- |
| Timestamp | the record's UTC timestamp |
| Category | the event family (`credential`, `provider`, `endpoint`, `authentication`, `security`) |
| Provider | the provider identity |
| Event | the full event name |
| Result | the decision, or the reason code |

Filters are provided for provider, event and result. The viewer is a table
of sanitised records: the audit record has no field for a secret, and
`SecurityAuditLog.record` refuses any keyword that names one, so the viewer
cannot show a credential even if a caller tried to put one there.

The tab is labelled **session-only**: the records live in memory for the
run and are not written to disk. The log is **not** encrypted and **not**
tamper-evident, and the interface says so in the same tab. Encrypted,
tamper-evident audit storage belongs to M3B-S1.6.

The viewer needs no internet connection, and it introduced no external
database dependency: it reads the list the audit log already keeps.

---

## 10. Error handling

| Situation | What the operator is told | What is not shown |
| --- | --- | --- |
| No credential for the provider | "Not configured", and the status line names the identity that is missing | the value |
| Credential disabled | "Disabled"; the masked description is empty | the value |
| Legacy credential present | "Migration required", with the explanation and the confirm control | the value |
| Secure storage unavailable | "Secure storage unavailable", with the reason, and the write is refused | any partial state |
| Endpoint refused by the policy | the endpoint label and the gateway's reason code, e.g. `private_destination` | — |
| Invalid model configuration | the configuration validator's own message, as before | — |
| Unsupported authentication method | the interface offers only bearer tokens and no-auth local servers; nothing else is selectable | — |
| Provider rejected the credential | the provider's own error text, as before | the header, the value |

No error message, tooltip, status line or activity entry contains a
credential value. The existing redaction filter in the logger still applies
to everything the application writes.

**Settings unrelated to provider security are preserved.** The Agent tab is
untouched, and the Provider tab still edits the model, the timeout, the
token cap and the temperature.

**The active model and provider are never changed silently.** Choosing an
identity in the security panel writes the configuration; it does not switch
the running session's provider. The session is rebuilt when the owner saves
the Settings dialog, which is the existing behaviour.

**Session behaviour when a credential changes.** A credential operation
does not disturb a turn in progress: the credential is consulted when a
request is built, so disabling a credential takes effect from the next
request. A turn already in flight keeps the value it was authorised with.
This is deliberate and is reported as a limitation rather than as a
feature.

**Nothing blocks the GUI thread.** The credential store operations are local
calls to the Windows Credential Manager, and the endpoint status check
performs no name resolution (§4).

---

## 11. GUI test coverage

`tests/gui/test_m3b_s12_security_settings.py` — **25 headless GUI tests**,
run on Qt's `offscreen` platform, so they need no visible desktop and no
display server. They instantiate the real widgets.

| # | Scenario | Test |
| --- | --- | --- |
| 1 | The table lists every provider identity | `test_the_table_lists_every_provider_identity` |
| 2 | Identity and display name are distinct columns | `test_the_table_distinguishes_identity_from_display_name` |
| 3 | A local provider is shown as needing no credential | `test_a_local_provider_is_shown_as_needing_no_credential` |
| 4 | The credential status column reflects the vault | `test_the_credential_status_column_reflects_the_vault` |
| 5 | No widget shows a credential value | `test_no_widget_ever_shows_a_credential_value` |
| 6 | The input is masked | `test_the_input_is_masked` |
| 7 | The input is never pre-filled | `test_the_input_is_never_prefilled` |
| 8 | The input is cleared after a save | `test_the_input_is_cleared_after_a_save` |
| 9 | The input is cleared when the panel closes | `test_the_input_is_cleared_when_the_panel_closes` |
| 10 | There is no reveal control | `test_there_is_no_reveal_control` |
| 11 | The status line shows a masked description | `test_the_status_line_shows_a_masked_description` |
| 12 | The banner is hidden when there is nothing to migrate | `test_the_banner_is_hidden_when_there_is_nothing_to_migrate` |
| 13 | The banner explains the legacy identity | `test_the_banner_explains_the_legacy_identity` |
| 14 | The confirm button needs a provider choice | `test_the_confirm_button_needs_a_provider_choice` |
| 15 | The banner never mentions the key | `test_the_banner_never_mentions_the_key` |
| 16 | An approved endpoint is shown as approved | `test_an_approved_endpoint_is_shown_as_approved` |
| 17 | A blocked endpoint is shown as blocked | `test_a_blocked_endpoint_is_shown_as_blocked` |
| 18 | The trust control only appears for a custom endpoint | `test_the_trust_control_only_appears_for_a_custom_endpoint` |
| 19 | The audit table starts empty | `test_the_audit_table_is_empty_at_the_start` |
| 20 | Events are rendered, without a secret | `test_events_are_rendered` |
| 21 | The viewer is labelled session-only | `test_the_audit_viewer_is_labelled_session_only` |
| 22 | The provider filter narrows the table | `test_the_filter_by_provider_narrows_the_table` |
| 23 | Cancelling the dialog clears the input | `test_the_dialog_clears_the_input_when_it_is_cancelled` |
| 24 | The dialog builds and closes | `test_the_dialog_can_be_built_and_closed` |

**What the automated GUI tests do *not* cover.** They exercise the widgets'
data layer and their handlers, not the painted result. Layout, spacing,
fonts, the appearance of the confirmation dialogs and the behaviour of the
real `QMessageBox` interactions are **not** verified automatically; the
modal dialogs are exercised by calling the handlers directly. A visual
inspection on a real desktop is required before the interface is considered
finished — see §15.

---

## 12. Security test coverage

`tests/security/test_m3b_s12_credential_migration.py` — **42 tests**. Every
credential is a fake canary, the secret store is in memory, and a socket
guard refuses every connection, so a test that accidentally reached the
network would fail rather than pass quietly.

| # | Scenario | Test |
| --- | --- | --- |
| 1 | Provider identity selection | `test_choosing_a_provider_sets_its_identity` |
| 2 | A display name never decides the identity | `test_the_display_name_never_decides_the_identity` |
| 3 | Cloud-to-cloud credential isolation | `test_a_cloud_credential_is_never_visible_to_another_cloud_provider` |
| 4 | Cloud-to-local credential isolation | `test_a_cloud_credential_is_never_visible_to_a_local_provider` |
| 5 | Secure key entry goes to the bound slot | `test_a_key_is_stored_under_the_provider_bound_slot` |
| 6 | Input clearing | `test_the_input_is_cleared_after_a_save` (GUI file) |
| 7 | Credential replacement | `test_replacing_a_credential_replaces_only_that_one` |
| 8 | Credential disabling | `test_a_disabled_credential_cannot_be_used` |
| 9 | Credential re-enabling | `test_a_disabled_credential_can_be_re_enabled` |
| 10 | Credential deletion | `test_removing_a_credential_leaves_the_others_alone` |
| 11 | Legacy migration confirmation | `test_a_confirmed_migration_moves_the_credential` |
| 12 | Migration cancellation | `test_a_cancelled_migration_changes_nothing` |
| 13 | Migration failure recovery | `test_a_failed_migration_preserves_the_source` |
| 14 | No migration without explicit consent | `test_nothing_is_migrated_without_an_explicit_action` |
| 15 | Existing destination preservation | `test_an_existing_destination_is_never_overwritten` |
| 16 | Custom endpoint refusal | `test_a_custom_endpoint_is_refused_by_default` |
| 17 | Blocked endpoint display | `test_a_blocked_endpoint_is_shown_as_blocked` |
| 18 | The interface cannot bypass the gateway | `test_the_gui_cannot_bypass_the_gateway` |
| 19 | Audit-event rendering | `test_the_audit_trail_is_renderable` |
| 20 | Audit secret redaction | `test_the_audit_trail_never_carries_a_secret` |
| 21 | Session-only audit status | `test_the_audit_trail_is_session_only` |
| 22 | Restart and settings reload | `test_the_state_survives_a_restart` |
| 23 | Existing provider settings compatibility | `test_an_existing_configuration_still_loads` |
| 24 | Existing agent tool-permission behaviour | `test_the_permission_broker_is_unchanged` |
| 25 | No live HTTP requests during tests | the autouse `_no_network` fixture, which fails any connection attempt |

Plus: migration idempotency, the wrong-provider choice, the read-back
mismatch, the audit redaction of the migration itself, the empty-key
refusal, and the fail-closed behaviour when secure storage is missing.

---

## 13. Regression results

| Check | M3B-S1.1 reported | M3B-S1.2 actual |
| --- | --- | --- |
| Full offline suite | 1,948 passed, 3 skipped | **2,015 passed, 3 skipped** |
| New tests | 104 security | **146 security** (104 + 42) and **25 headless GUI** |
| Ruff | clean | clean |
| Mypy strict | clean (173 files) | clean (177 files) |
| `git diff --check` | clean | clean |

The three skips are the pre-existing symlink tests, which cannot run on
this platform.

Nothing was weakened to make a test pass. The M3B-S1.1 adversarial suite is
unchanged and still passes in full.

---

## 14. Known limitations

| # | Limitation | Why | Owner |
| --- | --- | --- | --- |
| L1 | The modal confirmation dialogs are not verified automatically | the `QMessageBox` interactions are exercised by calling the handlers directly; a painted result needs a desktop | manual verification, §15 |
| L2 | The audit trail is session-only | the audit log keeps records in memory; persistence was not part of this milestone | M3B-S1.6 |
| L3 | A credential change takes effect from the next request | a turn already in flight keeps the value it was authorised with | by design |
| L4 | The endpoint status does not include name resolution | the interface must not block on DNS; the request path does the resolving check | by design |
| L5 | The generic `QM_API_KEY` belongs to the default provider only | inherited from M3B-S1.1 | M3B-S1.1 |
| L6 | The same-user privilege limitation is unchanged | an approved command can still read the plaintext fallback store | M3B-S1.4 |
| L7 | Taksh is not offered as a provider | the provider architecture has no Taksh entry, and this milestone does not add one | future milestone |
| L8 | The identity of a custom endpoint is a hash of its canonical URL | two spellings of one endpoint are the same endpoint, so they share an identity | by design |

---

## 15. Manual Windows verification steps

These steps are for the owner, on a real desktop, with the real
application. They are the part of this milestone that automation cannot
cover. **They never ask you to print, export or paste the stored key, and
you should not do so.**

### 15.1 Confirming the existing OpenRouter credential

The credential is stored under the shared legacy identity
`provider:openai_compatible:api_key`. The application will not use it until
you say which provider it belongs to.

1. Start the application.
2. Open **Security…** in the left sidebar (or **Settings…** → the
   **Security** tab).
3. The **Legacy credential** group appears at the top of *Providers &
   credentials*. Read the explanation: it says the credential was found
   under the shared identity and has not been used, moved or deleted.
4. In **Belongs to**, choose **OpenRouter**. Nothing is preselected, and
   **Confirm migration** stays disabled until you choose.
5. Press **Confirm migration** and answer **Yes**.
6. The dialog reports that the credential is now stored under the provider
   identity and has been read back successfully.
7. The **Credential** column for the `openrouter` row now reads **Active**,
   and the legacy group disappears.

If you press **No**, or close the panel, nothing changes: the credential
stays in the legacy slot and the row still reads *Migration required*. That
is the correct outcome for a cancelled confirmation.

If the migration fails, the original credential is left untouched and the
panel says so. Nothing is lost either way.

### 15.2 Checking the rest of the panel

8. The provider table lists every identity with its endpoint, its
   authentication type and its credential status. No key is visible
   anywhere, and there is no control that reveals one.
9. Choose **Ollama (local)** and press **Use this provider**. The endpoint
   line reads *Approved loopback endpoint*, and the Ollama row shows
   *None (local model server)*. The OpenRouter credential is still listed
   under OpenRouter; it was not moved.
10. Choose OpenRouter again and press **Use this provider**. The credential
    status for `openrouter` is unchanged — choosing a provider never
    touches a credential.
11. Press **Disable** on the OpenRouter credential. The status becomes
    *Disabled* and the stored description disappears. **Enable** puts it
    back.
12. **Remove** asks for confirmation and deletes the stored value. It does
    not revoke the key at the provider.

### 15.3 The audit trail

13. Open the **Security audit** tab. The events from the steps above are
    listed with a timestamp, a category, the provider, the event and the
    result. Use the three filters to narrow the list.
14. Confirm the note at the bottom: the trail is session-only, and it is
    neither encrypted nor tamper-evident. Restart the application and the
    list is empty again — that is expected, not a fault.

### 15.4 A custom endpoint

15. Set the base URL to `https://api.example.com/v1` in the **Provider**
    tab. The security panel shows *Custom endpoint requiring authorization*.
16. Press **Trust this custom endpoint**. The dialog names the canonical
    endpoint, states that the configured API key will be sent to it, and
    asks. Declining leaves the endpoint blocked. Accepting records
    `trust_custom_endpoint = true` in `config.toml`, where you can see it.

### 15.5 What to record

Record the outcome of steps 1–16 in the milestone report: what the status
column read, what the dialogs said, and whether the migration succeeded.
**Do not record the key, a partial key, a screenshot of the key field, or
anything copied out of the credential store.**

---

## 16. Final qualification gates

| Gate | Requirement | Verdict | Evidence |
| --- | --- | --- | --- |
| S12-G1 | Repository and artifacts preserved | **PASS** | §2; HEAD, version, flags, MSI hashes and all 11 evidence trees unchanged |
| S12-G2 | Existing security gateway reused | **PASS** | the controller only calls `AuthGateway.authorize`; the gateway's own rules decide |
| S12-G3 | Provider identity selection | **PASS** | `test_choosing_a_provider_sets_its_identity`, and the table test |
| S12-G4 | Credential management UI | **PASS** | the status, masking, clearing, replacement, disable, enable and remove tests |
| S12-G5 | Explicit legacy migration confirmation | **PASS** | `test_a_confirmed_migration_moves_the_credential` and the GUI banner tests |
| S12-G6 | Safe migration failure recovery | **PASS** | `test_a_failed_migration_preserves_the_source` |
| S12-G7 | Endpoint security status | **PASS** | the six endpoint-status tests |
| S12-G8 | Custom endpoint restrictions | **PASS** | `test_a_custom_endpoint_is_refused_by_default` and the trust test |
| S12-G9 | Security audit viewer | **PASS** | the audit rendering, filtering, redaction and session-only tests |
| S12-G10 | Credential-safe error handling | **PASS** | `test_no_widget_ever_shows_a_credential_value`, the redaction tests |
| S12-G11 | GUI and security tests | **PASS** | 25 headless GUI + 42 security tests, all passing |
| S12-G12 | Provider and agent regressions | **PASS** | 2,015 passed, 3 skipped; the M3B-S1.1 suite is unchanged |
| S12-G13 | Static analysis | **PASS** | ruff clean; mypy strict clean over 177 files |
| S12-G14 | Documentation and manual test guide | **PASS** | this document, §15 |
| S12-G15 | Final repository preservation | **PASS** | nothing committed, staged, tagged or released |

**One qualification is explicitly limited.** The *painted* behaviour of the
interface — layout, spacing and the appearance of the modal dialogs — is
**not** qualified by automation. The automated GUI tests cover the data
layer and the handlers. The visual result is **INSUFFICIENT_EVIDENCE**
until a human has followed §15 on a desktop.

---

## 17. Recommended next milestone

**M3B-S1.4 — OS-enforced execution isolation and a pinned transport.**

The two residual risks that M3B-S1.1 identified are the ones this milestone
cannot close from the interface: an approved command still runs with the
same privileges as the credential store (R1/R2), and the name-resolution
check still leaves a window between the check and the connection (R3).
Both need work below the application.

After that, **M3B-S1.6** for encrypted, tamper-evident audit storage and
the enterprise authentication seam, and **M3B-S1.3** for the
authentication-strategy interface that the gateway is already shaped for.

The live coding evaluation (M3B-2) may now proceed on top of this
foundation: the credential that the live benchmark needs is the one the
owner confirms in §15, and the gateway will authorise every request that
carries it.
