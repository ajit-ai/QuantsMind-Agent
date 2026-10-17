"""Application composition root.

:class:`ApplicationContext` wires the subsystems together::

    Configuration -> Model Gateway -> Tool Registry -> Agent Core
                   -> Session       -> Context Engine
                   -> Event Bus     -> GUI bridge

The GUI receives this object and never constructs providers, tools or agents by
itself, which keeps provider logic out of the widget layer.

This module also provides :func:`main`, the console entry point, so the same
composition is used whether the app is launched from a shortcut, from
``python -m quantsmind_agent`` or from a test.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quantsmind_agent.code_tools import build_code_tools
from quantsmind_agent.command_tools import build_command_tools
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.context import ContextEngine
from quantsmind_agent.conversations import (
    ConversationStore,
    ConversationSummary,
    StorageLimits,
    snapshot_session,
)
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.events import AgentEvent, AgentEventType, EventBus
from quantsmind_agent.git_tools import build_git_tools
from quantsmind_agent.llm import ModelGateway
from quantsmind_agent.logging_setup import get_logger, setup_logging, shutdown_logging
from quantsmind_agent.paths import AppPaths, default_paths
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionBroker,
    PermissionRequest,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.project_intel import ProjectIntelligence
from quantsmind_agent.providers import build_provider
from quantsmind_agent.session import Session
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.workspace_tools import build_default_tools
from quantsmind_agent.write_tools import build_mutating_tools, build_undo_tool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quantsmind_agent.agent import AgentCore

__all__ = ["ApplicationContext", "main"]

logger = get_logger(__name__)

#: Events that end a turn. A turn is the smallest unit of durable progress.
_TURN_END_EVENTS = frozenset(
    {
        AgentEventType.AGENT_COMPLETED,
        AgentEventType.AGENT_FAILED,
        AgentEventType.AGENT_CANCELLED,
    }
)


class ApplicationContext:
    """Owns every long-lived subsystem and the transitions between them.

    The gateway, tool registry and agent are always present; only the workspace
    is optional, because the application can start with no project open.
    """

    def __init__(self, config_manager: ConfigManager, paths: AppPaths) -> None:
        self.config_manager = config_manager
        self.paths = paths
        self.events = EventBus()
        self.recorded_events: list[AgentEvent] = []
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
        #: Project-scoped discovery and symbol/import map. Owned here rather than by
        #: a tool because the tools are rebuilt on every workspace or settings
        #: change, which is exactly when a cache would be thrown away. Session-scoped
        #: and deliberately not persisted: it is a claim about files on disk.
        self.project_intel: ProjectIntelligence | None = None
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
    @classmethod
    def create(
        cls,
        *,
        paths: AppPaths | None = None,
        config_manager: ConfigManager | None = None,
    ) -> ApplicationContext:
        """Build a fully wired context, tolerating a broken config file."""

        resolved_paths = paths or (config_manager.paths if config_manager else default_paths())
        manager = config_manager or ConfigManager(resolved_paths)
        try:
            manager.load()
        except QuantsMindError as exc:
            logger.warning("Falling back to default configuration: %s", exc.message)
            manager.reset()

        context = cls(config_manager=manager, paths=resolved_paths)
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
        from quantsmind_agent.gui.approval import build_prompt

        workspace = str(self.workspace.root) if self.workspace else ""
        outcome: ApprovalOutcome = self.approval.request(build_prompt(request, workspace=workspace))
        return outcome

    def _on_approval(self, request: PermissionRequest, outcome: ApprovalOutcome) -> None:
        """Record what the user chose, so the transcript shows intent."""

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

    def _rebuild(self) -> None:
        """(Re)create the gateway, tool registry, context engine and agent.

        Called at construction and whenever the workspace or the configuration
        changes. The GUI resolves the agent through :meth:`current_agent` on
        every turn, so a replacement instance is picked up immediately.
        """

        from quantsmind_agent.agent import AgentCore

        config = self.config_manager.config
        session = self.session

        permissions = PermissionBroker(
            provider=self._build_permission_provider(),
            on_decision=self._on_permission_decision,
            on_approval=self._on_approval,
            approver=self._request_approval,
        )
        registry = ToolRegistry(
            permissions=permissions,
            event_bus=self.events,
            session_id=session.id,
        )
        if self.workspace is not None:
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
                )
            )
            registry.register_all(
                [
                    build_undo_tool(
                        self.workspace,
                        session.revisions,
                        intelligence=self.project_intel,
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
                )
            )
            if agent_settings.git_enabled:
                registry.register_all(build_git_tools(self.workspace))

        self.gateway = ModelGateway(
            build_provider(config.provider, api_key=self.config_manager.api_key_value),
            event_bus=self.events,
            session_id=session.id,
            default_timeout=config.provider.timeout_seconds,
        )

        context_engine = self.context_engine
        context_engine.set_custom_prompt(config.agent.system_prompt)
        context_engine.set_workspace(self.workspace)

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
    def _restore_workspace(self) -> None:
        last = self.config_manager.config.last_workspace
        if not last:
            return
        try:
            self.workspace = Workspace(last)
        except QuantsMindError as exc:
            logger.info("Could not reopen the last workspace: %s", exc.message)
            return
        self.session.set_workspace(self.workspace)
        self.context_engine.set_workspace(self.workspace)
        self._rebuild()

    def open_workspace(self, path: str | Path) -> Workspace:
        """Point the whole application at ``path``."""

        workspace = Workspace(path)
        # A different project is a different question, so this is a new instance
        # rather than a reset: nothing from the previous project can leak into the
        # new one's answers.
        self.project_intel = ProjectIntelligence(workspace)
        self.context_engine.set_project_intel(self.project_intel)
        self.workspace = workspace
        self.session.set_workspace(workspace)
        self.context_engine.set_workspace(workspace)
        # A project the user opened by hand supersedes any stale "unavailable".
        self._unavailable_workspace = ""
        self._rebuild()

        config = self.config_manager.config
        config.last_workspace = str(workspace.root)
        self._save_config()
        self.events.emit(AgentEventType.WORKSPACE_OPENED, {"path": str(workspace.root)})
        return workspace

    def close_workspace(self) -> None:
        self.workspace = None
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

    def readiness(self) -> tuple[bool, str]:
        """Whether a request can be sent, plus a user-facing reason."""

        config = self.config_manager.config
        if self.workspace is None:
            return False, "Open a project folder first."
        if config.provider.requires_api_key() and not self.config_manager.has_api_key():
            return False, "No API key configured. Open Settings to add one."
        if not config.provider.model:
            return False, "No model selected. Open Settings to choose one."
        return True, "Ready"

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
        """The live :class:`~quantsmind_agent.agent.AgentCore`.

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


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantsmind-agent",
        description="QuantsMind-Agent \u2014 an AI coding agent for local projects.",
    )
    parser.add_argument(
        "--workspace",
        metavar="PATH",
        help="Open this project folder on startup instead of prompting.",
    )
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        help="Use this directory for config/secrets/logs instead of the user default.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging (developer mode).",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Do not write a log file (useful for debugging a broken install).",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Load the configuration, print a redacted summary and exit.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start with a blank conversation even if resume_last_conversation is on.",
    )
    parser.add_argument(
        "--conversation",
        metavar="ID",
        help="Resume this stored conversation instead of the most recent one.",
    )
    parser.add_argument(
        "--list-conversations",
        action="store_true",
        help="Print the stored conversations and exit.",
    )
    parser.add_argument(
        "--export",
        metavar="PATH",
        help="Export a conversation to PATH and exit (.md or .json).",
    )
    parser.add_argument(
        "--export-format",
        choices=("md", "json"),
        default="md",
        help="Format for --export when the filename does not say (default: md).",
    )
    return parser


def _paths_from_args(args: argparse.Namespace) -> AppPaths:
    if not args.config_dir:
        return default_paths()
    base = Path(args.config_dir).expanduser()
    return AppPaths(
        config_dir=base / "config",
        log_dir=base / "logs",
        cache_dir=base / "cache",
        data_dir=base / "data",
    )


def _run_check_config(context: ApplicationContext) -> int:
    config = context.config_manager.config
    ready, reason = context.readiness()
    print(f"config file : {context.config_manager.config_file}")
    print(f"log file    : {context.paths.log_file}")
    print(f"provider    : {config.provider.name}")
    print(f"base url    : {config.provider.base_url}")
    print(f"model       : {config.provider.model}")
    print(f"api key     : {context.config_manager.masked_api_key()}")
    print(f"workspace   : {context.workspace.root if context.workspace else '(none)'}")
    print(f"tools       : {', '.join(context.tools.names) if context.tools else '(none)'}")
    print(f"conversations: {len(context.list_conversations())} stored")
    if context.resumed_conversation_id:
        title = context.conversation_title or context.resumed_conversation_id
        print(f"resumed     : {title} ({context.resumed_conversation_id})")
        print(f"last turn   : {context.resumed_outcome}")
    print(f"ready       : {'yes' if ready else 'no'} ({reason})")
    return 0 if ready else 1


def _run_list_conversations(context: ApplicationContext) -> int:
    """Print the stored conversations. Plain text, so it pipes into anything."""

    summaries = context.list_conversations()
    if not summaries:
        print("No stored conversations.")
        return 0
    print(f"{'ID':<20} {'UPDATED':<17} {'TURNS':>5}  TITLE")
    for summary in summaries:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(summary.updated_at))
        flag = "" if summary.finished else "  (interrupted)"
        print(f"{summary.id:<20} {when:<17} {summary.turn_count:>5}  {summary.title}{flag}")
    return 0


def _run_export(context: ApplicationContext, args: argparse.Namespace) -> int:
    fmt = args.export_format
    if Path(args.export).suffix.lower() in (".md", ".markdown"):
        fmt = "md"
    elif Path(args.export).suffix.lower() == ".json":
        fmt = "json"
    written = context.export_conversation(
        args.export, conversation_id=args.conversation or "", fmt=fmt
    )
    if written is None:
        target = args.conversation or "the open conversation"
        print(f"Could not export {target}; it may not be stored.", file=sys.stderr)
        return 1
    print(f"Exported to {written}")
    return 0


def _resume_for_startup(context: ApplicationContext, args: argparse.Namespace) -> None:
    """Honour ``resume_last_conversation`` once, for both front ends.

    Done here rather than in the window so the console path resumes too, and done
    before the window is built so the window does not resume a second time.
    """

    if args.no_resume:
        logger.info("Resumption declined on the command line")
        return
    if context.resume_on_startup(conversation_id=args.conversation):
        logger.info("Resumed conversation %s", context.resumed_conversation_id)
    elif args.conversation:
        print(f"No stored conversation with id {args.conversation}.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns a process exit code."""

    args = _build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    paths = _paths_from_args(args)

    try:
        paths.ensure()
    except OSError as exc:
        print(
            f"Cannot create the application data directory ({paths.config_dir}): {exc}",
            file=sys.stderr,
        )
        return 2

    setup_logging(
        level="DEBUG" if args.debug else "INFO",
        debug=args.debug,
        paths=paths,
        file_enabled=not args.no_log_file,
    )
    logger.info("Starting %s", "QuantsMind-Agent")

    try:
        context = ApplicationContext.create(paths=paths)
    except QuantsMindError as exc:
        logger.error("Startup failed: %s", exc.message)
        print(f"QuantsMind-Agent could not start: {exc.user_message}", file=sys.stderr)
        shutdown_logging()
        return 2

    try:
        # Open the requested workspace before anything reports readiness, so
        # ``--workspace ... --check-config`` shows what it would open.
        if args.workspace:
            try:
                context.open_workspace(args.workspace)
            except QuantsMindError as exc:
                print(exc.user_message, file=sys.stderr)
                return 2

        if args.list_conversations:
            return _run_list_conversations(context)

        if args.export:
            return _run_export(context, args)

        _resume_for_startup(context, args)

        if args.check_config:
            return _run_check_config(context)

        return _run_gui(context)
    finally:
        shutdown_logging()


def _run_gui(context: ApplicationContext) -> int:
    """Start the Qt application. Imported lazily so headless tests skip Qt."""

    from PySide6.QtWidgets import QApplication

    from quantsmind_agent.gui.main_window import MainWindow
    from quantsmind_agent.gui.style import apply_theme

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_theme(app)

    # The window owns its worker bridge; it resolves the agent per turn.
    window = MainWindow(context)
    ready, reason = context.readiness()
    logger.info("Startup readiness: %s (%s)", "ready" if ready else "not ready", reason)
    window.show()
    return app.exec()
