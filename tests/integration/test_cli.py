"""Integration tests for the command-line entry point.

Covers argument parsing, the redacted configuration report and the exit codes,
without opening a window.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from quantsmind_agent import config as config_module
from quantsmind_agent.app import ApplicationContext, _build_parser, _paths_from_args, main
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.conversations import snapshot_session
from quantsmind_agent.secrets import FileSecretStore
from quantsmind_agent.tools import ToolResult

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _portable_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every path at ``tmp_path`` so the real profile is never touched."""

    root = tmp_path / "cli-profile"
    monkeypatch.setenv("QM_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("QM_LOG_DIR", str(root / "logs"))
    return root


@pytest.fixture(autouse=True)
def _no_os_credential_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep CLI credential lookups out of the real OS credential store.

    ``main()`` builds its own ``ConfigManager``, whose default store consults the
    Windows Credential Manager -- a machine-global resource that may hold a real
    credential saved earlier through the Settings dialog. Tests must never read
    it, for two reasons: the results would depend on machine state, and a failing
    assertion could echo a live secret into CI output.

    Redirecting the store *factory* to a temp file store isolates that external
    resource without touching production code. Everything else still runs
    unchanged: argument parsing, ``ConfigManager`` construction, the
    environment-then-store resolution order, key masking, the report format and
    the exit codes. ``test_secrets_paths.py`` still exercises the real keyring
    construction, because it calls ``build_secret_store`` directly.

    Nothing is written to or deleted from the real credential store.
    """

    store_path = tmp_path / "cli-profile" / "config" / "secrets.json"

    def _file_store(_path: Path) -> FileSecretStore:
        return FileSecretStore(store_path)

    monkeypatch.setattr(config_module, "build_secret_store", _file_store)
    yield


class TestArgumentParsing:
    def test_defaults(self) -> None:
        args = _build_parser().parse_args([])
        assert args.workspace is None
        assert args.config_dir is None
        assert args.debug is False
        assert args.no_log_file is False
        assert args.check_config is False
        assert args.no_resume is False
        assert args.conversation is None
        assert args.export is None
        assert args.export_format == "md"

    def test_flags(self) -> None:
        args = _build_parser().parse_args(
            ["--workspace", "C:\\code", "--debug", "--no-log-file", "--check-config"]
        )
        assert args.workspace == "C:\\code"
        assert args.debug is True
        assert args.no_log_file is True
        assert args.check_config is True

    def test_conversation_flags(self) -> None:
        args = _build_parser().parse_args(
            ["--conversation", "conv-1", "--no-resume", "--export", "out.json"]
        )
        assert args.conversation == "conv-1"
        assert args.no_resume is True
        assert args.export == "out.json"

    def test_an_unknown_export_format_is_refused_by_the_parser(self) -> None:
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--export", "out.pdf", "--export-format", "pdf"])

    def test_config_dir_redirects_every_location(self, tmp_path: Path) -> None:
        args = _build_parser().parse_args(["--config-dir", str(tmp_path / "portable")])
        paths = _paths_from_args(args)
        assert paths.config_dir == tmp_path / "portable" / "config"
        assert paths.log_dir == tmp_path / "portable" / "logs"

    def test_environment_override_is_used_without_the_flag(self, tmp_path: Path) -> None:
        os.environ["QM_CONFIG_DIR"] = str(tmp_path / "from-env")
        try:
            paths = _paths_from_args(_build_parser().parse_args([]))
            assert paths.config_dir == tmp_path / "from-env"
        finally:
            del os.environ["QM_CONFIG_DIR"]


class TestCheckConfig:
    def test_reports_not_ready_without_a_workspace(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--check-config", "--no-log-file"]) == 1
        out = capsys.readouterr().out
        assert "ready       : no" in out
        assert "Open a project folder first." in out

    def test_reports_tools_for_an_opened_workspace(
        self, capsys: pytest.CaptureFixture[str], sample_project: Path
    ) -> None:
        code = main(["--check-config", "--no-log-file", "--workspace", str(sample_project)])
        out = capsys.readouterr().out
        assert "read_file" in out
        assert str(sample_project.resolve()) in out
        # Readiness is deterministic here: the workspace is open and the
        # isolated store holds no key, so the default OpenAI endpoint still
        # needs one.
        assert code == 1

    def test_output_never_contains_a_credential(self, capsys: pytest.CaptureFixture[str]) -> None:
        config_dir = Path(os.environ["QM_CONFIG_DIR"])
        config_dir.mkdir(parents=True, exist_ok=True)
        # main() builds its own store; _no_os_credential_store points it at this
        # file, so the seeded value is the one it resolves.
        #
        # M3B-S1.1: the legacy slot is shared by every provider, so it no
        # longer authenticates the configured provider on its own. A credential
        # is bound to a provider identity, and the value is only reported
        # masked, never in full.
        (config_dir / "secrets.json").write_text(
            '{"provider:openai_compatible:api_key": "super-secret-key-424242"}',
            encoding="utf-8",
        )
        main(["--check-config", "--no-log-file"])
        out = capsys.readouterr().out
        assert "super-secret-key-424242" not in out
        # The legacy slot alone does not resolve for the configured provider.
        assert "api key     : (not set)" in out

    def test_a_provider_bound_credential_is_reported_masked(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_dir = Path(os.environ["QM_CONFIG_DIR"])
        config_dir.mkdir(parents=True, exist_ok=True)
        # The provider-bound slot is the one the application reads (M3B-S1.1).
        (config_dir / "secrets.json").write_text(
            '{"provider:openai:default:api_key": "super-secret-key-424242"}',
            encoding="utf-8",
        )
        main(["--check-config", "--no-log-file"])
        out = capsys.readouterr().out
        assert "super-secret-key-424242" not in out
        assert "api key     : sup" in out
        assert "******" in out

    def test_local_endpoints_need_no_key(
        self,
        capsys: pytest.CaptureFixture[str],
        sample_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = Path(os.environ["QM_CONFIG_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.toml").write_text(
            '[provider]\nbase_url = "http://localhost:11434/v1"\nmodel = "qwen2.5-coder"\n',
            encoding="utf-8",
        )
        assert main(["--check-config", "--no-log-file", "--workspace", str(sample_project)]) == 0
        out = capsys.readouterr().out
        assert "ready       : yes" in out
        assert "qwen2.5-coder" in out
        del monkeypatch

    def test_unknown_workspace_exits_with_two(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        code = main(["--check-config", "--no-log-file", "--workspace", str(tmp_path / "nope")])
        assert code == 2
        assert "could not be opened" in capsys.readouterr().err


def _store_a_conversation(
    title: str,
    *,
    conversation_id: str = "conv-seeded",
    finished: bool = True,
) -> None:
    """Write one stored conversation straight through the store.

    Built here rather than by running the agent so the console commands are tested
    against a known record, with no provider, project or network involved. It goes
    where the CLI looks -- :func:`default_paths`, i.e. the directories the
    environment redirects to -- rather than into a fixture's, which is a different
    set of paths.
    """

    from quantsmind_agent.conversations import ConversationStore
    from quantsmind_agent.paths import default_paths
    from quantsmind_agent.schema import AssistantMessage, UserMessage
    from quantsmind_agent.session import Session

    session = Session()
    session.add_message(UserMessage(content=title))
    session.record_tool_result(ToolResult.success("read_file", "read mod.py"))
    session.add_message(AssistantMessage(content="the answer to the question"))
    turn = session.begin_turn(title)
    turn.finished_at = turn.started_at + 1.0
    store = ConversationStore(default_paths().data_dir / "conversations")
    payload = snapshot_session(
        session,
        provider={"name": "openai", "model": "gpt", "base_url": "https://api.test/v1"},
        finished=finished,
    )
    payload["id"] = conversation_id
    store.save(payload)


class TestConversationCommands:
    def test_listing_reports_an_empty_store(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--list-conversations", "--no-log-file"]) == 0
        assert "No stored conversations." in capsys.readouterr().out

    def test_listing_shows_a_stored_conversation(self, capsys: pytest.CaptureFixture[str]) -> None:
        _store_a_conversation("Explain the parser")

        assert main(["--list-conversations", "--no-log-file"]) == 0
        out = capsys.readouterr().out
        assert "Explain the parser" in out
        assert "conv-seeded" in out

    def test_an_interrupted_conversation_is_flagged_in_the_listing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _store_a_conversation("Cut short", finished=False)

        main(["--list-conversations", "--no-log-file"])

        assert "interrupted" in capsys.readouterr().out

    def test_export_writes_markdown(self, tmp_path: Path) -> None:
        _store_a_conversation("Explain the parser")
        target = tmp_path / "dossier"

        assert (
            main(["--export", str(target), "--no-log-file", "--conversation", "conv-seeded"]) == 0
        )
        text = target.with_suffix(".md").read_text(encoding="utf-8")
        assert "Explain the parser" in text
        assert "the answer to the question" in text

    def test_the_filename_suffix_decides_the_format(self, tmp_path: Path) -> None:
        import json as json_module

        _store_a_conversation("Explain the parser")
        target = tmp_path / "dossier.json"

        assert (
            main(["--export", str(target), "--no-log-file", "--conversation", "conv-seeded"]) == 0
        )
        assert json_module.loads(target.read_text(encoding="utf-8"))["id"] == "conv-seeded"

    def test_exporting_a_conversation_that_does_not_exist_exits_one(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        _store_a_conversation("Explain the parser")

        code = main(
            ["--export", str(tmp_path / "out.md"), "--no-log-file", "--conversation", "gone"]
        )

        assert code == 1
        assert "may not be stored" in capsys.readouterr().err

    def test_the_cli_resumes_the_last_conversation(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _store_a_conversation("Explain the parser")

        assert main(["--check-config", "--no-log-file"]) in (0, 1)
        out = capsys.readouterr().out
        assert "resumed" in out
        assert "Explain the parser" in out
        assert "conv-seeded" in out

    def test_no_resume_leaves_the_console_start_blank(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _store_a_conversation("Explain the parser")

        main(["--check-config", "--no-log-file", "--no-resume"])

        assert "resumed" not in capsys.readouterr().out

    def test_resumption_switched_off_in_config_is_honoured_by_the_cli(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _store_a_conversation("Explain the parser")
        config_dir = Path(os.environ["QM_CONFIG_DIR"])
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            "[agent]\nresume_last_conversation = false\n", encoding="utf-8"
        )

        main(["--check-config", "--no-log-file"])

        assert "resumed" not in capsys.readouterr().out

    def test_a_named_conversation_is_resumed_by_the_cli(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _store_a_conversation("Explain the parser", conversation_id="conv-named")

        main(["--check-config", "--no-log-file", "--conversation", "conv-named"])

        assert "conv-named" in capsys.readouterr().out

    def test_a_deleted_conversation_is_not_listed_or_resumed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from quantsmind_agent.paths import default_paths

        _store_a_conversation("Explain the parser")
        store = default_paths().data_dir / "conversations"
        (store / "conv-seeded.json").unlink()

        main(["--list-conversations", "--no-log-file"])
        assert "Explain the parser" not in capsys.readouterr().out

        main(["--check-config", "--no-log-file"])
        assert "resumed" not in capsys.readouterr().out


class TestStartup:
    def test_describing_the_window_reports_the_views_without_running_them(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        """The acceptance harness cannot see inside the process; this flag can.

        It is how the *packaged* application is shown to carry the task workflow
        rather than only the source tree, so the report has to name the real
        views and the real controls.
        """

        project = tmp_path / "project"
        project.mkdir()
        main(
            [
                "--workspace",
                str(project),
                "--config-dir",
                str(tmp_path / "config"),
                "--describe-window",
                "--no-log-file",
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert report["views"] == ["Chat", "Task"]
        assert report["current_view"] == "Chat"
        controls = report["task_panel"]["controls"]
        assert {"Propose plan", "Approve", "Reject", "Run", "Pause", "Cancel"} == set(controls)
        # Nothing may be runnable before a plan has been approved.
        assert controls["Approve"]["enabled"] is False
        assert controls["Run"]["enabled"] is False
        assert report["task_panel"]["task_attached"] is False
        assert report["task_panel"]["worker_running"] is False

    def test_context_starts_without_a_project(self, config_manager: ConfigManager) -> None:
        context = ApplicationContext.create(
            paths=config_manager.paths, config_manager=config_manager
        )
        assert context.workspace is None
        assert context.tools.names == []
        assert context.agent.is_running is False
        ready, reason = context.readiness()
        assert ready is False
        assert reason == "Open a project folder first."

    def test_cli_and_context_share_the_same_paths(
        self, config_manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quantsmind_agent.paths import default_paths

        monkeypatch.setenv("QM_CONFIG_DIR", str(config_manager.paths.config_dir))
        monkeypatch.setenv("QM_LOG_DIR", str(config_manager.paths.log_dir))
        assert default_paths().config_dir == config_manager.paths.config_dir
        assert default_paths().log_dir == config_manager.paths.log_dir
