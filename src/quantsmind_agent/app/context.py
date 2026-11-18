"""Application composition root.

:class:`ApplicationContext` wires the subsystems together::

    Configuration -> Model Gateway -> Tool Registry -> Agent Core
                   -> Session       -> Context Engine
                   -> Event Bus     -> GUI bridge

The GUI receives this object and never constructs providers, tools or agents by
itself, which keeps provider logic out of the widget layer.

The console entry point lives in :mod:`quantsmind_agent.app.cli` and the Qt
entry point in :mod:`quantsmind_agent.app.desktop`, so this module stays
headless and importable without Qt.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quantsmind_agent.app.settings import (
    GENERIC_ENV_OWNER,
    PROVIDER_ENV_VARS,
    ConfigManager,
    Configuration,
    ProviderSettings,
)
from quantsmind_agent.code_tools import build_code_tools
from quantsmind_agent.context import ContextEngine
from quantsmind_agent.diagnostics.governance import ResourceGovernor
from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import LLMConfigurationError, QuantsMindError
from quantsmind_agent.events import AgentEvent, AgentEventType, EventBus
from quantsmind_agent.git_tools import build_git_tools
from quantsmind_agent.persistence.editor_state import EditorStateStore
from quantsmind_agent.persistence.paths import AppPaths, default_paths
from quantsmind_agent.providers import build_provider
from quantsmind_agent.providers.gateway import ModelGateway
from quantsmind_agent.runtime.agent.core import AgentCore
from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.runtime.tools.builtin.command import build_command_tools
from quantsmind_agent.runtime.tools.builtin.terminal import build_terminal_tools
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.audit_sink import RotatingAuditSink
from quantsmind_agent.security.auth_gateway import AuthGateway
from quantsmind_agent.security.credential_resolution import (
    CredentialResolution,
    CredentialResolutionService,
    CredentialStatus,
)
from quantsmind_agent.security.credential_vault import CredentialVault, CredentialVaultError
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionBroker,
    PermissionRequest,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.security.provider_identity import EndpointPolicyError, canonicalize_endpoint
from quantsmind_agent.security.workspace_trust import (
    WorkspaceTrustState,
    WorkspaceTrustStore,
)
from quantsmind_agent.services.changesets.applier import ChangeSetApplier
from quantsmind_agent.services.changesets.rollback import ReverseApplier
from quantsmind_agent.services.changesets.storage import ChangeSetStore
from quantsmind_agent.services.conversations.session import Session
from quantsmind_agent.services.conversations.store import (
    ConversationStore,
    ConversationSummary,
    StorageLimits,
    snapshot_session,
)
from quantsmind_agent.services.workspace.containment import Workspace, workspace_root_id
from quantsmind_agent.workspace_intel import WorkspaceIntelligence, WorkspaceSnapshot
from quantsmind_agent.workspace_tools import build_default_tools
from quantsmind_agent.write_tools import build_mutating_tools, build_undo_tool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quantsmind_agent.gui.security.controller import SecurityController

__all__ = ["ApplicationContext", "ReadinessCode", "ReadinessReport"]


logger = get_logger(__name__)

#: Events that end a turn. A turn is the smallest unit of durable progress.
_TURN_END_EVENTS = frozenset(
    {
        AgentEventType.AGENT_COMPLETED,
        AgentEventType.AGENT_FAILED,
        AgentEventType.AGENT_CANCELLED,
    }
)


class ReadinessCode(StrEnum):
    """Why the application is, or is not, ready to send a request."""

    READY = "ready"
    WORKSPACE_MISSING = "workspace_missing"
    ENDPOINT_INVALID = "endpoint_invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    CREDENTIAL_MISSING = "credential_missing"
    CREDENTIAL_DISABLED = "credential_disabled"
    CREDENTIAL_UNAVAILABLE = "credential_unavailable"
    CREDENTIAL_STORAGE_DEGRADED = "credential_storage_degraded"
    ENDPOINT_DENIED = "endpoint_denied"
    MODEL_MISSING = "model_missing"


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """A structured readiness answer, with the decisions behind it."""

    ready: bool
    code: ReadinessCode
    reason: str
    credential: CredentialResolution | None = None
    endpoint_reason: str = ""


class ApplicationContext:
    """Owns every long-lived subsystem and the transitions between them.

    The gateway, tool registry and agent are always present; only the workspace
    is optional, because the application can start with no project open.
    """

    def __init__(
        self,
        config_manager: ConfigManager,
        paths: AppPaths,
        *,
        security_resolver: Any | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.paths = paths
        #: Per-root trust decisions. Undecided roots are never trusted
        #: implicitly; until a human decides, every consumer treats them as
        #: Restricted Mode.
        self.trust_store = WorkspaceTrustStore(paths.trust_file)
        #: The decision that applies to the open workspace.
        self.workspace_trust = WorkspaceTrustState.UNDECIDED
        #: Open tabs, cursors and workbench layout (schema 1).
        self.editor_state = EditorStateStore(paths.editor_state_file)
        #: Resource governance (ADR-012 baseline, P-7 owner). This is the single
        #: authority expensive work asks before starting; the probe beneath it
        #: measures, this decides.
        self.resources = ResourceGovernor()
        #: Name resolution used by endpoint authorisation. Injectable so tests
        #: and offline deployments never depend on the system resolver.
        self.security_resolver = security_resolver
        self.events = EventBus()
        self.recorded_events: list[AgentEvent] = []
        #: Security audit trail (M3B-S1.1). Identifiers and decisions only.
        #: Since v0.11.0 the records also reach disk: an approval or a
        #: transaction that cannot be written is refused before it starts, and
        #: a record lost after the fact is reported instead of hidden.
        self.audit_sink = self._build_audit_sink()
        self.security_audit = SecurityAuditLog(sink=self.audit_sink)
        #: Credential vault and authentication gateway (M3B-S1.1). The gateway
        #: is the only component that may hand a credential to a provider.
        #: Whether an OS credential store is actually available (M3B-S1.2).
        self.secure_storage_available = True
        self.credential_vault = self._build_credential_vault()
        #: The one authority every consumer asks about a credential (Phase 0.8).
        self.credential_service = CredentialResolutionService(
            self.credential_vault,
            provider_env_vars=PROVIDER_ENV_VARS,
            generic_env_owner=GENERIC_ENV_OWNER,
        )
        self.config_manager.bind_credential_service(self.credential_service)
        self.auth_gateway = AuthGateway(
            vault=self.credential_vault,
            audit=self.security_audit,
            resolver=security_resolver,
            credential_service=self.credential_service,
            # The trust decisions live in the configuration and can change
            # while the application runs, so the gateway asks for them rather
            # than freezing them at construction time.
            trust_custom_endpoints=lambda: (
                self.config_manager.config.provider.trust_custom_endpoint
            ),
            allow_remote_local_models=lambda: (
                self.config_manager.config.provider.allow_remote_local_models
            ),
        )

        self.session = Session()
        self.context_engine = ContextEngine()
        self.workspace: Workspace | None = None
        #: Bounded orientation for the system prompt, replaced per workspace.
        self.context_engine.set_project_intel(None)
        self.bridge: Any | None = None
        #: GUI approval bridge, attached by the main window. While it is absent
        #: (CLI, tests, startup) every ASK resolves to DENY.
        self.approval: Any | None = None
        #: One cancellation flag shared by the agent loop and blocking tools.
        self._cancel = threading.Event()
        #: Durable conversation storage; ``None`` when persistence is switched off.
        self.conversations: ConversationStore | None = None
        #: ChangeSet store: every transaction survives here, keyed by workspace.
        self.changeset_store = ChangeSetStore.from_app_paths(paths)
        #: Transactional apply engine; built per-workspace in open_workspace.
        self.changeset_applier: ChangeSetApplier | None = None
        #: Reverse applier for rollback; built per-workspace in open_workspace.
        self.changeset_rollback: ReverseApplier | None = None
        #: Project-scoped discovery, symbol/import map and bounded workspace
        #: snapshot. Owned here rather than by a tool because the tools are
        #: rebuilt on every workspace or settings change, which is exactly when a
        #: cache would be thrown away. Session-scoped and deliberately not
        #: persisted: it is a claim about files on disk.
        self.project_intel: WorkspaceIntelligence | None = None
        #: Per-conversation storage bounds, rebuilt with the store.
        self._storage_limits = StorageLimits()
        #: Title of the conversation in progress, derived from the first request.
        self._conversation_title = ""
        #: Whether the last turn ended cleanly, as it will be recorded on disk.
        self._last_turn_finished = True
        #: A stored project that could not be reopened, for the UI to explain.
        self._unavailable_workspace = ""
        #: Set when a conversation was reopened at startup.
        self._resumed_id = ""
        #: Whether the conversation being resumed ended cleanly, as stored.
        self._resumed_finished = True
        #: Older items a resumed record no longer holds, so the UI can say so.
        self._resumed_truncated = 0
        self.events.subscribe_all(self._record_event)
        self._rebuild()

    # -- construction ------------------------------------------------------
    def _build_audit_sink(self) -> RotatingAuditSink:
        """The durable audit store, or a sink that reports it cannot write.

        Construction never raises. A data directory that is unwritable is a real
        condition the user has to be told about -- and one that must make an
        approval fail closed -- but it is not a reason the application cannot
        start with a conversation history in memory.
        """

        return RotatingAuditSink(
            self.paths.audit_log_file,
            anchor_path=self.paths.audit_anchor_file,
        )

    def _build_credential_vault(self) -> CredentialVault:
        """The credential vault, degrading to a read-only vault when needed.

        The vault refuses to write to a plaintext store. When the platform has
        no OS credential service the application still starts: the vault then
        holds no writable backend, credentials cannot be stored, and the user
        is told why.
        """

        try:
            self.secure_storage_available = True
            return CredentialVault(
                self.config_manager.secret_store,
                metadata_path=self.paths.credentials_file,
                audit=self.security_audit,
                require_secure=True,
            )
        except CredentialVaultError as exc:
            logger.warning("Secure credential storage is unavailable: %s", exc.message)
            self.secure_storage_available = False
            return CredentialVault(
                self.config_manager.secret_store,
                metadata_path=self.paths.credentials_file,
                audit=self.security_audit,
                require_secure=False,
                allow_insecure_writes=False,
            )

    def security_controller(self) -> SecurityController:
        """The controller behind the secure provider settings (M3B-S1.2).

        It is built with a gateway that does not resolve names, so the
        interface never performs a network operation on the GUI thread. The
        authoritative check -- the one that includes name resolution -- still
        runs in the request path, and it is the one that decides.
        """

        from quantsmind_agent.gui.security.controller import SecurityController

        gateway = AuthGateway(
            vault=self.credential_vault,
            audit=self.security_audit,
            resolver=self.security_resolver,
            credential_service=self.credential_service,
            check_dns=False,
            trust_custom_endpoints=lambda: (
                self.config_manager.config.provider.trust_custom_endpoint
            ),
            allow_remote_local_models=lambda: (
                self.config_manager.config.provider.allow_remote_local_models
            ),
        )
        return SecurityController(
            self.config_manager,
            vault=self.credential_vault,
            gateway=gateway,
            audit=self.security_audit,
            secure_storage=self.secure_storage_available,
        )

    @classmethod
    def create(
        cls,
        *,
        paths: AppPaths | None = None,
        config_manager: ConfigManager | None = None,
        security_resolver: Any | None = None,
    ) -> ApplicationContext:
        """Build a fully wired context, tolerating a broken config file."""

        resolved_paths = paths or (config_manager.paths if config_manager else default_paths())
        manager = config_manager or ConfigManager(resolved_paths)
        try:
            manager.load()
        except QuantsMindError as exc:
            logger.warning("Falling back to default configuration: %s", exc.message)
            manager.reset()

        context = cls(
            config_manager=manager,
            paths=resolved_paths,
            security_resolver=security_resolver,
        )
        context._build_store()
        context._restore_workspace()
        return context

    def _build_store(self) -> None:
        """Attach conversation storage when the setting allows it.

        A directory that cannot be created disables persistence rather than
        stopping startup: a broken data directory is not a reason to refuse to
        open the application.
        """

        agent = self.config_manager.config.agent
        self.conversations = None
        if not agent.persist_conversations:
            logger.info("Conversation persistence is switched off")
            return
        root = self.paths.data_dir / "conversations"
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - unwritable data directory
            logger.warning("Conversations cannot be stored in %s: %s", root, exc)
            return
        self.conversations = ConversationStore(
            root, max_conversations=agent.max_stored_conversations
        )
        self._storage_limits = StorageLimits(
            max_messages=agent.max_stored_messages,
            max_tool_results=agent.max_stored_tool_results,
            max_chars_per_item=agent.max_stored_item_chars,
        )

    # -- event plumbing ----------------------------------------------------
    def _record_event(self, event: AgentEvent) -> None:
        """Keep a bounded event history for diagnostics and tests."""

        self.recorded_events.append(event)
        if len(self.recorded_events) > 400:
            del self.recorded_events[: len(self.recorded_events) - 400]
        self.session.record_event(event)
        # A turn is the unit of durable progress: saving on completion, failure
        # or cancellation means a crash costs at most the turn in progress, and
        # the record says whether that turn finished.
        if event.type in _TURN_END_EVENTS:
            self._last_turn_finished = event.type is AgentEventType.AGENT_COMPLETED
            self.save_conversation()

    # -- configuration -----------------------------------------------------
    def apply_configuration(self) -> None:
        """Re-read configuration and rebuild everything that depends on it."""

        self._rebuild()
        config = self.config_manager.config
        self.events.emit(
            AgentEventType.CONFIG_CHANGED,
            {"model": config.provider.model, "base_url": config.provider.base_url},
        )

    def _request_options(self) -> dict[str, Any]:
        provider = self.config_manager.config.provider
        return {
            "model": provider.model,
            "temperature": provider.temperature,
            "max_output_tokens": provider.max_output_tokens,
            "timeout_seconds": provider.timeout_seconds,
        }

    def _build_permission_provider(self) -> StaticPermissionProvider:
        """Assemble the active policy.

        Rule *order is load-bearing*, so the list is built explicitly rather than
        by mutating a default one. :meth:`StaticPermissionProvider.decide` sorts
        candidates by "exact tool first, then a non-wildcard target pattern", and
        that sort is stable: two rules sharing the same tool wildcard and target
        pattern keep their insertion order, which makes a permissive wildcard
        added after a restrictive one unreachable. The order below is therefore
        always permissive-before-restrictive within one key group:

        1. exact-tool rules, which the sort always places first,
        2. permissive wildcards,
        3. restrictive wildcards (ASK/DENY).

        Reads are allowed because they change nothing. Writes and commands ask,
        and three operations ask regardless of how permissive the flags are:
        ``delete_file`` (irreversible), ``undo_file`` (also a mutation, and a
        deletion when it reverts a creation) and ``run_command``
        (host-affecting). ``git_enabled`` removes the Git tools rather than
        denying them.
        """

        agent = self.config_manager.config.agent
        provider = StaticPermissionProvider()

        # 1. Per-tool rules: the sort places these ahead of every wildcard, so
        #    they cannot be widened by a global flag.
        provider.add_rule(
            PermissionRule(Permission.WRITE_WORKSPACE, Decision.ASK, tool="delete_file")
        )
        provider.add_rule(
            PermissionRule(Permission.WRITE_WORKSPACE, Decision.ASK, tool="undo_file")
        )
        provider.add_rule(
            PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK, tool="run_command")
        )

        # 2. Permissive wildcards.
        provider.add_rule(
            PermissionRule(
                Permission.READ_WORKSPACE,
                Decision.ALLOW if agent.auto_approve_reads else Decision.ASK,
            )
        )
        provider.add_rule(
            PermissionRule(
                Permission.WRITE_WORKSPACE,
                Decision.ALLOW if agent.auto_approve_writes else Decision.ASK,
            )
        )
        provider.add_rule(PermissionRule(Permission.GIT, Decision.ALLOW))

        # 3. Restrictive wildcards last, so they are reachable when the matching
        #    permissive rule above is not present.
        provider.add_rule(PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK))
        provider.add_rule(PermissionRule(Permission.NETWORK, Decision.DENY))
        return provider

    def _request_approval(self, request: PermissionRequest) -> ApprovalOutcome:
        """The broker's approver. Denies when no human is reachable.

        Returning DENY rather than ALLOW is what keeps a headless run safe: the
        CLI, tests and startup all reach this path with ``self.approval`` unset,
        so nothing mutating ever executes without a GUI dialog behind it.
        """

        if self.approval is None:
            logger.info(
                "no approver available; denying %s for %s",
                request.permission.value,
                request.tool,
            )
            return ApprovalOutcome.DENY
        # Imported lazily: this module must stay importable without Qt so the
        # headless core and the CLI never load PySide6.
        from quantsmind_agent.gui.security.approval import build_prompt

        workspace = str(self.workspace.root) if self.workspace else ""
        outcome: ApprovalOutcome = self.approval.request(build_prompt(request, workspace=workspace))
        return outcome

    def _on_approval(self, request: PermissionRequest, outcome: ApprovalOutcome) -> None:
        """Record what the user chose, so the transcript and the audit show it.

        The audit record is written durably and *before* the broker hands back a
        decision, which is what makes it a gate rather than a note: if the record
        cannot be written, the approval is not granted and the tool does not run.
        """

        self.security_audit.record(
            AuditEventType.APPROVAL_DECISION,
            decision=outcome.value,
            outcome=outcome.value,
            reason_code="human_decision" if outcome is not ApprovalOutcome.DENY else "human_denied",
            subject=request.target or request.summary,
            detail=f"{request.permission.value} for {request.tool}",
            durable=True,
        )
        self.events.emit(
            AgentEventType.PERMISSION_APPROVAL,
            {
                "tool": request.tool,
                "permission": request.permission.value,
                "summary": request.summary,
                "target": request.target,
                "approval": outcome.value,
            },
        )

    def _on_permission_decision(self, request: PermissionRequest, decision: Decision) -> None:
        logger.info("permission %s for %s on %s", decision.value, request.tool, request.target)
        if decision is not Decision.DENY:
            return
        self.security_audit.record(
            AuditEventType.SECURITY_POLICY_DENIED,
            decision=decision.value,
            outcome="denied",
            reason_code="policy_denied",
            subject=request.target or request.summary,
            detail=f"{request.permission.value} for {request.tool}",
        )

    def _rebuild(self) -> None:
        """(Re)create the gateway, tool registry, context engine and agent.

        Called at construction and whenever the workspace or the configuration
        changes. The GUI resolves the agent through :meth:`current_agent` on
        every turn, so a replacement instance is picked up immediately.
        """

        config = self.config_manager.config

        session = self.session

        permissions = PermissionBroker(
            provider=self._build_permission_provider(),
            on_decision=self._on_permission_decision,
            on_approval=self._on_approval,
            approver=self._request_approval,
        )
        self.permission_broker = permissions
        registry = ToolRegistry(
            permissions=permissions,
            event_bus=self.events,
            session_id=session.id,
        )
        if self.workspace is not None and self.workspace_trust is WorkspaceTrustState.TRUSTED:
            agent_settings = config.agent
            registry.register_all(
                build_default_tools(self.workspace, max_read_bytes=agent_settings.max_file_bytes)
            )
            # Read-only navigation: outlines and call sites, so the model can aim
            # an edit at a line range instead of reading whole modules.
            registry.register_all(
                build_code_tools(
                    self.workspace,
                    max_file_bytes=agent_settings.max_file_bytes,
                    intelligence=self.project_intel,
                )
            )
            # The journal lives on the session, not on the tools: every tool is
            # rebuilt here, and a write from the previous turn must still be
            # undoable afterwards.
            registry.register_all(
                build_mutating_tools(
                    self.workspace,
                    max_file_bytes=agent_settings.max_file_bytes,
                    journal=session.revisions,
                    intelligence=self.project_intel,
                    audit=self.security_audit,
                )
            )
            registry.register_all(
                [
                    build_undo_tool(
                        self.workspace,
                        session.revisions,
                        intelligence=self.project_intel,
                        audit=self.security_audit,
                    )
                ]
            )
            registry.register_all(
                build_command_tools(
                    self.workspace,
                    cancel_event=self._cancel,
                    default_timeout=agent_settings.command_timeout_seconds,
                    max_output_chars=agent_settings.max_command_output_chars,
                    redact_env=(config.provider.api_key_env,),
                    audit=self.security_audit,
                )
            )
            registry.register_all(
                build_terminal_tools(
                    self.workspace,
                    cancel_event=self._cancel,
                    default_timeout=agent_settings.command_timeout_seconds,
                    max_output_chars=agent_settings.max_command_output_chars,
                    audit=self.security_audit,
                )
            )
            if agent_settings.git_enabled:
                registry.register_all(build_git_tools(self.workspace))

        self.gateway = ModelGateway(
            self._build_model_provider(config),
            event_bus=self.events,
            session_id=session.id,
            default_timeout=config.provider.timeout_seconds,
        )

        context_engine = self.context_engine
        context_engine.set_custom_prompt(config.agent.system_prompt)
        # Restricted workspaces contribute no project content to the prompt. The
        # agent may still answer general questions, but it cannot read, index or
        # transmit the untrusted project.
        context_engine.set_workspace(
            self.workspace if self.workspace_trust is WorkspaceTrustState.TRUSTED else None
        )

        self.tools = registry
        self.agent = AgentCore(
            self.gateway,
            registry,
            context_engine,
            session,
            event_bus=self.events,
            max_iterations=config.agent.max_iterations,
            request_options=self._request_options,
            cancel_event=self._cancel,
        )

    # -- workspace ---------------------------------------------------------
    def _build_model_provider(self, config: Configuration) -> Any:
        """The configured provider, or a router over several.

        Routing is built only when it has been configured *and* has somewhere
        to go. With one endpoint a router would add a layer and change nothing, so
        the single-provider path stays exactly as it was.

        Every endpoint gets its **own** credential resolution (M3B-S1.1). The
        old code passed one callable to all of them, so a fallback endpoint
        received the primary endpoint's key.
        """

        routing = config.routing
        primary = self._build_endpoint_provider(config.provider)
        if not routing.enabled or not routing.fallbacks:
            return primary

        from quantsmind_agent.providers.routing import ProviderRouter, RouteCandidate, RoutingPolicy

        candidates = [
            RouteCandidate(
                provider=primary, model=config.provider.model, label=config.provider.name
            )
        ]
        for index, fallback in enumerate(routing.fallbacks, start=1):
            candidates.append(
                RouteCandidate(
                    provider=self._build_endpoint_provider(fallback),
                    model=fallback.model,
                    label=f"{fallback.name or fallback.base_url}#{index}",
                )
            )
        return ProviderRouter(candidates, policy=RoutingPolicy(max_attempts=routing.max_attempts))

    def _build_endpoint_provider(self, settings: ProviderSettings) -> Any:
        """A provider whose credential is released only after authorisation.

        The callable is evaluated at request time, inside the provider's
        header construction, so the gateway authorises the endpoint first and
        a refused endpoint never causes a secret to be loaded.
        """

        try:
            identity = settings.identity()
            endpoint = canonicalize_endpoint(settings.base_url)
        except (EndpointPolicyError, QuantsMindError) as exc:
            raise LLMConfigurationError(
                f"The provider endpoint could not be resolved: {exc}",
                user_message=(
                    "The provider address in Settings could not be understood, so no API "
                    "key was sent. Check the base URL."
                ),
                details={"base_url": settings.base_url},
            ) from exc

        required = settings.requires_api_key()
        gateway = self.auth_gateway

        def resolve_credential() -> str | None:
            return gateway.credential_for(
                identity,
                endpoint,
                required=required,
                configured_env_name=settings.api_key_env,
            )

        options = gateway.client_options(endpoint, credential_bearing=required)
        self.security_audit.record(
            AuditEventType.PROVIDER_SELECTED,
            provider_id=identity.provider_id,
            model=settings.model,
            endpoint_id=endpoint.endpoint_id,
            decision="selected",
            reason_code=identity.trust.value,
        )
        return build_provider(
            settings,
            api_key=resolve_credential,
            follow_redirects=bool(options["follow_redirects"]),
            trust_env=bool(options["trust_env"]),
        )

    # -- workspace ---------------------------------------------------------
    @property
    def workspace_intel(self) -> WorkspaceIntelligence | None:
        """The active workspace intelligence, when a project is open.

        Named separately from :attr:`project_intel` because it *is* a project
        intelligence with a snapshot attached; the old name remains the shared
        type the navigation tools consume.
        """

        return self.project_intel

    def workspace_snapshot(self, *, refresh: bool = False) -> WorkspaceSnapshot | None:
        """The bounded workspace facts a plan should be proposed against.

        Discovery is synchronous and bounded; callers that run on a GUI thread
        should use it sparingly or from a worker. A missing workspace is ``None``
        rather than an invented empty snapshot.
        """

        intel = self.project_intel
        if intel is None:
            return None
        return intel.refresh_snapshot() if refresh else intel.snapshot

    def _restore_workspace(self) -> None:
        last = self.config_manager.config.last_workspace
        if not last:
            return
        try:
            workspace = Workspace(last)
        except QuantsMindError as exc:
            logger.info("Could not reopen the last workspace: %s", exc.message)
            return
        # Reopening the folder the user already had open may continue their
        # session only when a valid persisted decision to trust it exists. An
        # unknown, corrupt or future record fails closed: Restricted Mode, with
        # no tools or project indexing granted.
        stored = self.trust_store.state_for(workspace.root)
        self.workspace_trust = (
            WorkspaceTrustState.TRUSTED
            if stored is WorkspaceTrustState.TRUSTED
            else WorkspaceTrustState.RESTRICTED
        )
        self.workspace = workspace
        # Restoring a project has to build the same intelligence that opening one
        # does. Without this the reopened window had an agent with no project
        # context until the user reopened the folder by hand.
        if self.workspace_trust is WorkspaceTrustState.TRUSTED:
            self.project_intel = WorkspaceIntelligence(workspace, resources=self.resources)
        else:
            self.project_intel = None
        self.context_engine.set_project_intel(self.project_intel)
        self.session.set_workspace(workspace)
        self._rebuild()

    def open_workspace(self, path: str | Path, *, trusted: bool | None = None) -> Workspace:
        """Point the whole application at ``path``.

        ``trusted`` records an explicit human decision. When it is omitted the
        stored decision is used, and an unknown, undecided, corrupt or future
        record fails closed: Restricted Mode. The desktop folder picker asks
        first and always passes a decision explicitly; programmatic and CLI
        callers must pass ``trusted=True`` (or use Restricted Mode) themselves.
        """

        workspace = Workspace(path)
        if trusted is not None:
            # Persist the human decision before it is used; a decision that did
            # not reach the trust file is not a decision.
            self.trust_store.decide(workspace.root, trusted=trusted)
            if (
                trusted
                and self.trust_store.state_for(workspace.root) is not WorkspaceTrustState.TRUSTED
            ):
                raise QuantsMindError(
                    "The workspace trust decision was refused by a newer trust file",
                    user_message=(
                        "The workspace trust settings belong to a newer version of "
                        "QuantsMind-Agent, so the folder was not trusted. Update the "
                        "application before trusting it."
                    ),
                )
            self.workspace_trust = (
                WorkspaceTrustState.TRUSTED if trusted else WorkspaceTrustState.RESTRICTED
            )
            self._audit_trust(workspace, trusted)
        else:
            stored = self.trust_store.state_for(workspace.root)
            self.workspace_trust = (
                WorkspaceTrustState.TRUSTED
                if stored is WorkspaceTrustState.TRUSTED
                else WorkspaceTrustState.RESTRICTED
            )
        # A different project is a different question, so this is a new instance
        # rather than a reset: nothing from the previous project can leak into the
        # new one's answers.
        self.project_intel = (
            WorkspaceIntelligence(workspace, resources=self.resources)
            if self.workspace_trust is WorkspaceTrustState.TRUSTED
            else None
        )
        self.context_engine.set_project_intel(self.project_intel)
        self.workspace = workspace
        #: ChangeSet apply and rollback engines, rebuilt per workspace so no
        #: state from a previous project leaks into this one.
        self.changeset_applier = ChangeSetApplier(
            workspace=workspace,
            store=self.changeset_store,
            permissions=self.permission_broker,
            audit=self.security_audit,
            governor=self.resources,
            cancel_event=self._cancel,
        )
        self.changeset_rollback = ReverseApplier(
            workspace=workspace,
            store=self.changeset_store,
            permissions=self.permission_broker,
            audit=self.security_audit,
        )
        self.session.set_workspace(workspace)
        # A project the user opened by hand supersedes any stale "unavailable".
        self._unavailable_workspace = ""
        self._rebuild()

        config = self.config_manager.config
        config.last_workspace = str(workspace.root)
        self._save_config()
        self.events.emit(
            AgentEventType.WORKSPACE_OPENED,
            {"path": str(workspace.root), "trust": self.workspace_trust.value},
        )
        return workspace

    @property
    def workspace_key(self) -> str:
        """The stable identity of the open workspace, or empty if none is open."""

        if self.workspace is None:
            return ""
        return workspace_root_id(self.workspace.root)

    def apply_changeset(self, changeset_id: str) -> Any:
        """Apply an approved ChangeSet through the journaled transaction engine.

        Reads the stored approval's plan digest so the GUI never has to handle
        credentials or tokens. Raises if the changeset cannot be applied.
        """

        if self.workspace is None:
            raise QuantsMindError(
                "No workspace is open",
                user_message="Open a project before applying a ChangeSet.",
            )
        if self.changeset_applier is None:
            raise QuantsMindError(
                "ChangeSet apply engine is not initialised",
                user_message="The ChangeSet engine is not available.",
            )
        stored = self.changeset_store.stored(self.workspace_key, changeset_id)
        manifest = stored.manifest
        if manifest is None:
            raise QuantsMindError(
                f"ChangeSet {changeset_id} has no stored manifest",
                details={"code": "changeset_not_found", "changeset_id": changeset_id},
            )
        from quantsmind_agent.services.changesets.models import ChangeSet

        change_set = ChangeSet.from_dict(manifest)
        if change_set.approval is None:
            raise QuantsMindError(
                f"ChangeSet {changeset_id} is not approved",
                details={"code": "changeset_not_approved", "changeset_id": changeset_id},
            )
        return self.changeset_applier.apply(
            changeset_id,
            self.workspace_key,
            approval_token=change_set.approval.plan_digest,
        )

    def rollback_changeset(self, changeset_id: str) -> Any:
        """Roll back an applied ChangeSet through the journaled engine.

        Raises if the changeset cannot be rolled back.
        """

        if self.workspace is None:
            raise QuantsMindError(
                "No workspace is open",
                user_message="Open a project before rolling back a ChangeSet.",
            )
        if self.changeset_rollback is None:
            raise QuantsMindError(
                "ChangeSet rollback engine is not initialised",
                user_message="The ChangeSet engine is not available.",
            )
        return self.changeset_rollback.rollback(changeset_id, self.workspace_key)

    def set_workspace_trust(self, trusted: bool) -> None:
        """Trust or restrict the open workspace and apply it immediately.

        Restricting cancels the active turn and removes the tool registry, the
        project index and the workspace prompt content at once. It cannot be
        undone by the agent or by repository content; only another human
        decision lifts it.
        """

        self._apply_workspace_trust(trusted, persist=True)

    def revoke_workspace_trust(self) -> None:
        """Remove the stored decision and restrict the open workspace.

        The session becomes restricted immediately, but no replacement record is
        stored: the next time the folder is opened it is undecided again, so the
        trust prompt asks the human rather than inheriting this restriction.
        """

        workspace = self.workspace
        if workspace is None:
            raise QuantsMindError(
                "No workspace is open",
                user_message="Open a project before changing its trust state.",
            )
        self.trust_store.revoke(workspace.root)
        self._apply_workspace_trust(False, persist=False)

    def _apply_workspace_trust(self, trusted: bool, *, persist: bool) -> None:
        workspace = self.workspace
        if workspace is None:
            raise QuantsMindError(
                "No workspace is open",
                user_message="Open a project before changing its trust state.",
            )
        if persist:
            self.trust_store.decide(workspace.root, trusted=trusted)
            if (
                trusted
                and self.trust_store.state_for(workspace.root) is not WorkspaceTrustState.TRUSTED
            ):
                raise QuantsMindError(
                    "The workspace trust decision was refused by a newer trust file",
                    user_message=(
                        "The workspace trust settings belong to a newer version of "
                        "QuantsMind-Agent, so the folder was not trusted. Update the "
                        "application before trusting it."
                    ),
                )
        self.workspace_trust = (
            WorkspaceTrustState.TRUSTED if trusted else WorkspaceTrustState.RESTRICTED
        )
        self._audit_trust(workspace, trusted)
        if trusted:
            self.project_intel = WorkspaceIntelligence(workspace, resources=self.resources)
        else:
            # Refuse the next gate immediately; an in-flight turn is cancelled
            # rather than left running with tools it no longer qualifies for.
            self.cancel()
            self.project_intel = None
        self.context_engine.set_project_intel(self.project_intel)
        self._rebuild()
        self.events.emit(
            AgentEventType.WORKSPACE_TRUST_CHANGED,
            {"path": str(workspace.root), "trust": self.workspace_trust.value},
        )

    def _audit_trust(self, workspace: Workspace, trusted: bool) -> None:
        self.security_audit.record(
            AuditEventType.WORKSPACE_TRUST_CHANGED,
            decision="trusted" if trusted else "restricted",
            reason_code="user_decision",
            detail=str(workspace.root),
        )

    @property
    def workspace_trusted(self) -> bool:
        return self.workspace is not None and self.workspace_trust is WorkspaceTrustState.TRUSTED

    @property
    def workspace_restricted(self) -> bool:
        return (
            self.workspace is not None and self.workspace_trust is not WorkspaceTrustState.TRUSTED
        )

    def close_workspace(self) -> None:
        self.workspace = None
        self.workspace_trust = WorkspaceTrustState.UNDECIDED
        self.project_intel = None
        self.context_engine.set_project_intel(None)
        self.session.set_workspace(None)
        self.context_engine.set_workspace(None)
        self._rebuild()
        config = self.config_manager.config
        config.last_workspace = None
        self._save_config()

    def _save_config(self) -> None:
        try:
            self.config_manager.save()
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Could not persist the configuration: %s", exc)

    # -- conversation ------------------------------------------------------
    # -- conversations -----------------------------------------------------
    def save_conversation(self, *, title: str = "") -> None:
        """Write the current conversation to disk.

        Called after every turn and on shutdown. A persistence failure is logged
        and swallowed: losing the transcript is bad, but refusing to run because
        the disk is full would be worse.
        """

        if self.conversations is None:
            return
        provider = self.config_manager.config.provider
        payload = snapshot_session(
            self.session,
            title=title or self._conversation_title,
            provider={
                "name": provider.name,
                "model": provider.model,
                "base_url": provider.base_url,
            },
            finished=self._last_turn_finished,
            limits=self._storage_limits,
        )
        self._conversation_title = payload["title"]
        try:
            self.conversations.save(payload)
        except (OSError, ValueError) as exc:
            logger.warning("Could not persist the conversation: %s", exc)

    def resume_latest_conversation(self) -> bool:
        """Reopen the most recent conversation, if the setting allows it.

        Approval grants and the undo journal are deliberately *not* restored: the
        user re-approves, and nothing can be undone until they ask for it again.
        """

        if self.conversations is None:
            return False
        summary = self.conversations.latest()
        if summary is None:
            return False
        return self.open_conversation(summary.id)

    def resume_on_startup(self, *, conversation_id: str = "", honour_setting: bool = True) -> bool:
        """Resume at startup, honouring ``resume_last_conversation``.

        ``conversation_id`` names a specific conversation, which overrides the
        "most recent" choice but not the setting: turning resumption off means off
        whether the newest conversation or a named one was asked for. Used by both
        the window and the console entry point so they cannot disagree about what
        "start up and pick up where you left off" means.
        """

        if self.conversations is None:
            return False
        setting = self.config_manager.config.agent.resume_last_conversation
        if honour_setting and not setting:
            logger.info("Conversation resumption is switched off")
            return False
        target = conversation_id
        if not target:
            summary = self.conversations.latest()
            if summary is None:
                return False
            target = summary.id
        return self.open_conversation(target)

    @property
    def resumed_conversation_id(self) -> str:
        """The conversation reopened at startup, so the UI does not reopen it twice."""

        return self._resumed_id

    def open_conversation(self, conversation_id: str) -> bool:
        """Switch to a stored conversation.

        Everything session-scoped is reset first -- undo history, recorded events,
        permission grants, the turn counter -- so nothing leaks between
        conversations. A workspace that no longer exists is reported and left
        unopened rather than silently substituted.
        """

        if self.conversations is None:
            return False
        payload = self.conversations.load(conversation_id)
        if payload is None:
            logger.info("Conversation %s is not available", conversation_id)
            return False

        self.session.reset(keep_workspace=False)
        self.events.clear_history()
        self.recorded_events.clear()
        self.tools.permissions.clear_session_grants()
        self.reset_cancel()
        # Cleared before the restore, not only when the stored project turns out to
        # be missing: a stale path from the conversation being left would otherwise
        # be reported against this one.
        self._unavailable_workspace = ""

        if not self.session.restore_from(payload):
            return False
        self._conversation_title = str(payload.get("title") or "")
        self._last_turn_finished = bool(payload.get("finished", True))
        self._resumed_finished = self._last_turn_finished
        self._resumed_id = conversation_id
        # Recorded so the UI can admit it when the stored transcript is partial
        # rather than presenting a shortened history as the whole thing.
        truncated = payload.get("truncated")
        self._resumed_truncated = (
            int(truncated.get("dropped_messages", 0))
            + int(truncated.get("dropped_tool_results", 0))
            if isinstance(truncated, dict)
            else 0
        )

        stored_workspace = str(payload.get("workspace") or "")
        if stored_workspace:
            if self._restore_stored_workspace(stored_workspace):
                logger.info("Reopened %s in %s", conversation_id, stored_workspace)
            else:
                # The project moved or was deleted. Opening it under another name
                # would put the agent in the wrong project.
                self._unavailable_workspace = stored_workspace
                logger.info("Stored workspace %s is not available", stored_workspace)
        return True

    def _restore_stored_workspace(self, stored: str) -> bool:
        if not Path(stored).is_dir():
            return False
        try:
            self.open_workspace(stored)
        except QuantsMindError as exc:
            logger.info("Could not reopen %s: %s", stored, exc.message)
            return False
        return True

    def list_conversations(self) -> list[ConversationSummary]:
        """Stored conversations, newest first."""

        if self.conversations is None:
            return []
        return self.conversations.list()

    def rename_conversation(self, conversation_id: str, title: str) -> bool:
        """Rename a stored conversation."""

        if self.conversations is None:
            return False
        renamed = self.conversations.rename(conversation_id, title)
        if renamed and conversation_id == self.session.id:
            self._conversation_title = title.strip() or self._conversation_title
        return renamed

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a stored conversation. The open one is left running.

        Deletion is total and immediate. A record deleted here is gone from the
        list, cannot be reopened, and will not be resumed at the next start.
        """

        if self.conversations is None:
            return False
        return self.conversations.delete(conversation_id)

    def export_conversation(
        self,
        target: str | Path,
        *,
        conversation_id: str = "",
        fmt: str = "md",
    ) -> Path | None:
        """Export a conversation as Markdown or JSON.

        Defaults to the open conversation. Returns ``None`` when there is nothing
        to export, so the caller can tell the user rather than writing an empty
        file.
        """

        if self.conversations is None:
            return None
        wanted = conversation_id or self.session.id
        if self.conversations.load(wanted) is None:
            return None
        try:
            return self.conversations.export(wanted, Path(target), fmt=fmt)
        except (OSError, ValueError) as exc:
            logger.warning("Could not export conversation %s: %s", wanted, exc)
            return None

    @property
    def conversation_title(self) -> str:
        return self._conversation_title

    @property
    def unavailable_workspace(self) -> str:
        """A stored project that could not be reopened, for the UI to explain."""

        return self._unavailable_workspace

    @property
    def resumed_outcome(self) -> str:
        """How the resumed conversation's last turn actually ended.

        ``ok``, ``cancelled``, ``failed`` or ``interrupted``. Read from the stored
        record rather than guessed, so a reopened conversation can say its last
        turn never finished instead of implying it did.
        """

        if not self._resumed_id:
            return ""
        if not self._resumed_finished:
            return "interrupted"
        return self.session.last_turn_outcome()

    @property
    def resumed_dropped_items(self) -> int:
        """Older items the resumed record had already dropped under its limits."""

        return self._resumed_truncated

    @property
    def last_turn_finished(self) -> bool:
        """Whether the last turn ended cleanly, as recorded on disk."""

        return self._last_turn_finished

    # -- conversation ------------------------------------------------------
    def reset_conversation(self) -> None:
        """Clear the transcript but keep the project open.

        The conversation being left behind is saved first: "new conversation" is
        not a reason to lose the previous transcript.
        """

        if self.session.messages or self.session.turns:
            self.save_conversation()
        self.session.reset(keep_workspace=True)
        self.events.clear_history()
        self.recorded_events.clear()
        # A new conversation is a new trust boundary: "Allow for Session"
        # grants from the previous one must not carry over.
        self.deny_pending_approval()
        self.reset_cancel()
        self._unavailable_workspace = ""
        self._rebuild()
        self.tools.permissions.clear_session_grants()

    # -- status ------------------------------------------------------------
    def status_line(self) -> str:
        return self.session.summary()

    def credential_resolution(
        self,
        settings: ProviderSettings | None = None,
        *,
        authorize: bool = False,
        record: bool = False,
    ) -> CredentialResolution:
        """The authoritative credential decision for a configuration.

        ``authorize=False`` is the diagnostic mode: it never performs a name
        resolution or writes an audit record. Readiness and the request path
        additionally ask the gateway for the endpoint decision.
        """

        provider = settings or self.config_manager.config.provider
        try:
            endpoint = canonicalize_endpoint(provider.base_url)
            identity = provider.identity()
        except (EndpointPolicyError, QuantsMindError):
            return CredentialResolution.unavailable(
                provider_id=provider.provider_id or "unknown",
                credential_id=f"provider:{provider.provider_id or 'unknown'}:default:api_key",
                reason_code="invalid_endpoint",
                detail="the configured provider endpoint could not be parsed",
            )
        if authorize:
            return self.auth_gateway.resolve_credential(
                identity,
                endpoint,
                required=provider.requires_api_key(),
                configured_env_name=provider.api_key_env,
                record=record,
            )
        return self.credential_service.resolve(
            identity,
            required=provider.requires_api_key(),
            configured_env_name=provider.api_key_env,
        )

    def masked_credential(self) -> str:
        """A UI-safe description of the active credential (never the value)."""

        return self.credential_resolution().masked()

    def readiness_report(self) -> ReadinessReport:
        """Structured readiness: the same decisions the request path makes."""

        if self.workspace is None:
            return ReadinessReport(
                ready=False,
                code=ReadinessCode.WORKSPACE_MISSING,
                reason="Open a project folder first.",
            )

        provider = self.config_manager.config.provider
        try:
            endpoint = canonicalize_endpoint(provider.base_url)
            identity = provider.identity()
        except (EndpointPolicyError, QuantsMindError):
            return ReadinessReport(
                ready=False,
                code=ReadinessCode.ENDPOINT_INVALID,
                reason=(
                    "The provider address in Settings is invalid, so no API key can be "
                    "sent. Correct the base URL."
                ),
            )

        if identity.identity_mismatch:
            return ReadinessReport(
                ready=False,
                code=ReadinessCode.IDENTITY_MISMATCH,
                reason=(
                    "The provider identity in Settings does not match the model address, "
                    "so no API key was sent. Open Settings and choose the provider again."
                ),
                endpoint_reason="provider_endpoint_mismatch",
            )

        required = provider.requires_api_key()
        resolution = self.credential_service.resolve(
            identity,
            required=required,
            configured_env_name=provider.api_key_env,
        )
        if not resolution.usable:
            if resolution.status is CredentialStatus.UNAVAILABLE:
                code = ReadinessCode.CREDENTIAL_UNAVAILABLE
                reason = (
                    "The credential store could not be read. Check the OS credential "
                    "service and restart."
                )
            elif resolution.reason_code == "credential_disabled":
                code = ReadinessCode.CREDENTIAL_DISABLED
                reason = (
                    "The API key for this provider is disabled. Open Settings to enable "
                    "it or enter a new one."
                )
            elif resolution.reason_code == "credential_revoked":
                code = ReadinessCode.CREDENTIAL_DISABLED
                reason = (
                    "The API key for this provider was removed. Open Settings to enter a new one."
                )
            else:
                code = ReadinessCode.CREDENTIAL_MISSING
                reason = "No API key configured. Open Settings to add one."
            return ReadinessReport(
                ready=False,
                code=code,
                reason=reason,
                credential=resolution,
                endpoint_reason=resolution.reason_code,
            )

        if not provider.model:
            return ReadinessReport(
                ready=False,
                code=ReadinessCode.MODEL_MISSING,
                reason="No model selected. Open Settings to choose one.",
                credential=resolution,
            )

        decision = self.auth_gateway.authorize(identity, endpoint, record=False)
        if not decision.allowed:
            return ReadinessReport(
                ready=False,
                code=ReadinessCode.ENDPOINT_DENIED,
                reason=(
                    "The model endpoint was refused by the security policy "
                    f"({decision.reason_code}). Open Settings to correct the provider "
                    "address."
                ),
                credential=resolution,
                endpoint_reason=decision.reason_code,
            )

        if resolution.status is CredentialStatus.DEGRADED:
            return ReadinessReport(
                ready=True,
                code=ReadinessCode.CREDENTIAL_STORAGE_DEGRADED,
                reason=("Ready, but the API key is stored without a secure OS credential store."),
                credential=resolution,
            )
        return ReadinessReport(
            ready=True,
            code=ReadinessCode.READY,
            reason="Ready",
            credential=resolution,
        )

    def readiness(self) -> tuple[bool, str]:
        """Whether a request can be sent, plus a user-facing reason."""

        report = self.readiness_report()
        return report.ready, report.reason

    def attach_bridge(self, bridge: Any) -> None:
        """Attach the GUI worker bridge (called once by the window)."""

        self.bridge = bridge

    def attach_approval(self, approval: Any) -> None:
        """Attach the GUI approval bridge.

        Called by the main window once it exists. Attaching is what makes an
        ``ASK`` reachable: before this, :meth:`_request_approval` denies every
        mutating call, which is why a headless start is safe by default.
        """

        self.approval = approval

    def request_approval(self, request: PermissionRequest) -> ApprovalOutcome:
        """Ask the attached bridge to prompt the user.

        Exposed so the GUI, tests and the CLI can exercise one approval path
        rather than reaching into the broker.
        """

        return self._request_approval(request)

    def deny_pending_approval(self) -> None:
        """Cancel an outstanding prompt, e.g. when the window closes."""

        if self.approval is not None and hasattr(self.approval, "cancel_pending"):
            self.approval.cancel_pending()

    def cancel(self) -> None:
        """Stop the running turn and any blocking tool sharing its cancel flag."""

        self._cancel.set()
        self.agent.cancel()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def current_agent(self) -> AgentCore:
        """The live :class:`~quantsmind_agent.runtime.agent.core.AgentCore`.

        The GUI resolves the agent through this method on every turn, because
        opening a workspace or saving settings replaces the instance.
        """

        return self.agent

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        # Whether a turn was in flight has to be read *before* cancelling: the cancel
        # below sets the flag unconditionally, so checking afterwards reported every
        # clean shutdown as an interrupted turn and marked good conversations as
        # unfinished forever.
        turn_was_running = self.bridge is not None and bool(getattr(self.bridge, "is_busy", False))
        self.deny_pending_approval()
        if turn_was_running and self.bridge is not None:
            self.bridge.cancel()
        else:
            self.cancel()
        # The transcript is worth keeping even on the way out; a turn that was still
        # running when the user quit is recorded as unfinished rather than complete.
        if self.session.messages or self.session.turns:
            self._last_turn_finished = self._last_turn_finished and not turn_was_running
            self.save_conversation()
