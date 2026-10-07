"""Integration tests for the command-line entry point.

Covers argument parsing, the redacted configuration report and the exit codes,
without opening a window.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from quantsmind_agent import config as config_module
from quantsmind_agent.app import ApplicationContext, _build_parser, _paths_from_args, main
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.secrets import FileSecretStore

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

    def test_flags(self) -> None:
        args = _build_parser().parse_args(
            ["--workspace", "C:\\code", "--debug", "--no-log-file", "--check-config"]
        )
        assert args.workspace == "C:\\code"
        assert args.debug is True
        assert args.no_log_file is True
        assert args.check_config is True

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
        (config_dir / "secrets.json").write_text(
            '{"provider:openai_compatible:api_key": "super-secret-key-424242"}',
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


class TestStartup:
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
