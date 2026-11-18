"""Command-line entry point and headless commands.

Argument parsing, the redacted configuration report and the exit codes live here;
:mod:`quantsmind_agent.app.desktop` owns the Qt launch path. Both use the same
:class:`~quantsmind_agent.app.context.ApplicationContext` composition.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from quantsmind_agent.app.context import ApplicationContext
from quantsmind_agent.app.desktop import _run_describe_window, _run_gui
from quantsmind_agent.diagnostics.logging import (
    get_logger,
    setup_logging,
    shutdown_logging,
)
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.persistence.paths import AppPaths, default_paths

__all__ = ["main"]

logger = get_logger(__name__)


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
        "--trust-workspace",
        action="store_true",
        help=(
            "Trust the workspace opened with --workspace. Without this flag an "
            "undecided workspace opens in Restricted Mode."
        ),
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
    parser.add_argument(
        "--describe-window",
        action="store_true",
        help="Print the main window's views and controls as JSON, then exit.",
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
    report = context.readiness_report()
    resolution = report.credential or context.credential_resolution()
    print(f"config file : {context.config_manager.config_file}")
    print(f"log file    : {context.paths.log_file}")
    print(f"provider    : {config.provider.name}")
    print(f"base url    : {config.provider.base_url}")
    print(f"model       : {config.provider.model}")
    print(f"api key     : {resolution.masked()}")
    print(
        f"credential  : {resolution.status.value} ({resolution.source.value}, "
        f"{resolution.storage_status.value})"
    )
    print(f"workspace   : {context.workspace.root if context.workspace else '(none)'}")
    print(f"tools       : {', '.join(context.tools.names) if context.tools else '(none)'}")
    print(f"conversations: {len(context.list_conversations())} stored")
    if context.resumed_conversation_id:
        title = context.conversation_title or context.resumed_conversation_id
        print(f"resumed     : {title} ({context.resumed_conversation_id})")
        print(f"last turn   : {context.resumed_outcome}")
    print(f"ready       : {'yes' if report.ready else 'no'} ({report.reason})")
    return 0 if report.ready else 1


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
                context.open_workspace(
                    args.workspace,
                    trusted=True if args.trust_workspace else None,
                )
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

        if args.describe_window:
            return _run_describe_window(context)

        return _run_gui(context)
    finally:
        shutdown_logging()
