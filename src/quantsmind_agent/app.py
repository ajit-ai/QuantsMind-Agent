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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quantsmind_agent.config import ConfigManager
from quantsmind_agent.context import ContextEngine
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.events import AgentEvent, AgentEventType, EventBus
from quantsmind_agent.llm import ModelGateway
from quantsmind_agent.logging_setup import get_logger, setup_logging, shutdown_logging
from quantsmind_agent.paths import AppPaths, default_paths
from quantsmind_agent.permissions import (
    Decision,
    Permission,
    PermissionBroker,
    PermissionRequest,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.providers import build_provider
from quantsmind_agent.session import Session
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.workspace_tools import build_default_tools

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quantsmind_agent.agent import AgentCore

__all__ = ["ApplicationContext", "main"]

logger = get_logger(__name__)


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
        self.bridge: Any | None = None
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
        context._restore_workspace()
        return context

    # -- event plumbing ----------------------------------------------------
    def _record_event(self, event: AgentEvent) -> None:
        """Keep a bounded event history for diagnostics and tests."""

        self.recorded_events.append(event)
        if len(self.recorded_events) > 400:
            del self.recorded_events[: len(self.recorded_events) - 400]
        self.session.record_event(event)

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
        provider = StaticPermissionProvider()
        for rule in provider.default_rules():
            provider.add_rule(rule)
        if not self.config_manager.config.agent.auto_approve_reads:
            # Reads become interactive; with no approver wired, ASK degrades
            # to DENY, so nothing is read without an explicit approval.
            provider.add_rule(PermissionRule(Permission.READ_WORKSPACE, Decision.ASK))
        return provider

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
        )
        registry = ToolRegistry(
            permissions=permissions,
            event_bus=self.events,
            session_id=session.id,
        )
        if self.workspace is not None:
            registry.register_all(
                build_default_tools(self.workspace, max_read_bytes=config.agent.max_file_bytes)
            )

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
        self.workspace = workspace
        self.session.set_workspace(workspace)
        self.context_engine.set_workspace(workspace)
        self._rebuild()

        config = self.config_manager.config
        config.last_workspace = str(workspace.root)
        self._save_config()
        self.events.emit(AgentEventType.WORKSPACE_OPENED, {"path": str(workspace.root)})
        return workspace

    def close_workspace(self) -> None:
        self.workspace = None
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
    def reset_conversation(self) -> None:
        """Clear the transcript but keep the project open."""

        self.session.reset(keep_workspace=True)
        self.events.clear_history()
        self.recorded_events.clear()
        self._rebuild()

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

    def current_agent(self) -> AgentCore:
        """The live :class:`~quantsmind_agent.agent.AgentCore`.

        The GUI resolves the agent through this method on every turn, because
        opening a workspace or saving settings replaces the instance.
        """

        return self.agent

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        if self.bridge is not None and getattr(self.bridge, "is_busy", False):
            self.bridge.cancel()


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
    print(f"ready       : {'yes' if ready else 'no'} ({reason})")
    return 0 if ready else 1


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
