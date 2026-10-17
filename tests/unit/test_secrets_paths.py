"""Unit tests for secret storage and application paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from quantsmind_agent.errors import ConfigurationError
from quantsmind_agent.paths import AppPaths, default_paths
from quantsmind_agent.secrets import (
    FileSecretStore,
    KeyringSecretStore,
    SecretStore,
    build_secret_store,
    mask_secret,
)


class TestFileSecretStore:
    def test_round_trip(self, app_paths: AppPaths) -> None:
        store = FileSecretStore(app_paths.secrets_file)
        assert store.get("a") is None
        store.set("a", SecretStr("value-one"))
        stored = store.get("a")
        assert stored is not None
        assert stored.get_secret_value() == "value-one"
        assert store.keys() == ["a"]
        assert store.delete("a") is True
        assert store.delete("a") is False

    def test_secrets_live_outside_the_repository(self, app_paths: AppPaths) -> None:
        FileSecretStore(app_paths.secrets_file).set("k", SecretStr("v"))
        assert app_paths.secrets_file.exists()
        assert app_paths.config_dir in app_paths.secrets_file.parents

    def test_overwrite(self, app_paths: AppPaths) -> None:
        store = FileSecretStore(app_paths.secrets_file)
        store.set("k", SecretStr("first"))
        store.set("k", SecretStr("second"))
        stored = store.get("k")
        assert stored is not None
        assert stored.get_secret_value() == "second"

    def test_corrupt_secret_file(self, app_paths: AppPaths) -> None:
        app_paths.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        app_paths.secrets_file.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            FileSecretStore(app_paths.secrets_file).get("k")

    def test_non_object_secret_file(self, app_paths: AppPaths) -> None:
        app_paths.secrets_file.parent.mkdir(parents=True, exist_ok=True)
        app_paths.secrets_file.write_text(json.dumps([1, 2]), encoding="utf-8")
        with pytest.raises(ConfigurationError):
            FileSecretStore(app_paths.secrets_file).keys()

    def test_secret_str_masks_repr(self, app_paths: AppPaths) -> None:
        FileSecretStore(app_paths.secrets_file).set("k", SecretStr("top-secret-value"))
        stored = FileSecretStore(app_paths.secrets_file).get("k")
        assert stored is not None
        assert "top-secret-value" not in repr(stored)
        assert "top-secret-value" not in str(stored)


class TestKeyringSecretStore:
    def test_falls_back_to_the_file_store(self, app_paths: AppPaths) -> None:
        fallback = FileSecretStore(app_paths.secrets_file)
        store = KeyringSecretStore(fallback)
        store.set("k", SecretStr("fallback-value"))
        stored = store.get("k")
        assert stored is not None
        assert stored.get_secret_value() == "fallback-value"

    def test_keys_come_from_the_enumerable_half(self, app_paths: AppPaths) -> None:
        fallback = FileSecretStore(app_paths.secrets_file)
        fallback.set("a", SecretStr("1"))
        assert KeyringSecretStore(fallback).keys() == ["a"]


class TestMaskSecret:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "(not set)"),
            ("", "(not set)"),
            ("short", "*****"),
            ("sk-abcdefghijklmnop", "sk-******op"),
        ],
    )
    def test_variants(self, value: str | None, expected: str) -> None:
        assert mask_secret(value) == expected

    def test_long_secret_is_never_shown_whole(self) -> None:
        secret = "a" * 40
        assert secret not in mask_secret(secret)


class TestBuildSecretStore:
    def test_prefers_keyring(self, app_paths: AppPaths) -> None:
        assert isinstance(build_secret_store(app_paths.secrets_file), KeyringSecretStore)

    def test_returns_a_secret_store(self, app_paths: AppPaths) -> None:
        assert isinstance(build_secret_store(app_paths.secrets_file), SecretStore)

    def test_can_force_the_file_store(self, app_paths: AppPaths) -> None:
        assert isinstance(
            build_secret_store(app_paths.secrets_file, prefer_keyring=False),
            FileSecretStore,
        )


class TestPaths:
    def test_default_paths_are_absolute(self) -> None:
        paths = default_paths()
        assert paths.config_file.is_absolute()
        assert paths.config_file.name == "config.toml"
        assert paths.secrets_file.name == "secrets.json"
        assert paths.log_file.name == "quantsmind-agent.log"

    def test_ensure_creates_every_directory(self, tmp_path: Path) -> None:
        paths = AppPaths(
            config_dir=tmp_path / "c",
            log_dir=tmp_path / "l",
            cache_dir=tmp_path / "cc",
            data_dir=tmp_path / "d",
        ).ensure()
        for directory in (paths.config_dir, paths.log_dir, paths.cache_dir, paths.data_dir):
            assert directory.is_dir()

    def test_ensure_is_idempotent(self, tmp_path: Path) -> None:
        paths = AppPaths(
            config_dir=tmp_path / "c",
            log_dir=tmp_path / "l",
            cache_dir=tmp_path / "cc",
            data_dir=tmp_path / "d",
        )
        assert paths.ensure() is paths.ensure()

    def test_config_dir_environment_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QM_CONFIG_DIR", str(tmp_path / "custom-config"))
        assert default_paths().config_dir == tmp_path / "custom-config"

    def test_log_dir_environment_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QM_LOG_DIR", str(tmp_path / "custom-logs"))
        assert default_paths().log_dir == tmp_path / "custom-logs"

    def test_redirecting_the_config_dir_redirects_the_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The conversation store lives in the data directory. If QM_CONFIG_DIR moved
        # only the config, a redirected run would still write transcripts into the
        # real user profile -- which is what happened, and what a portable install
        # or a test must never do.
        monkeypatch.setenv("QM_CONFIG_DIR", str(tmp_path / "portable" / "config"))

        paths = default_paths()

        assert paths.data_dir == tmp_path / "portable" / "data"
        assert paths.cache_dir == tmp_path / "portable" / "cache"

    def test_the_environment_and_the_flag_produce_the_same_layout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from quantsmind_agent.app import _build_parser, _paths_from_args

        monkeypatch.setenv("QM_CONFIG_DIR", str(tmp_path / "portable" / "config"))
        by_env = default_paths()

        by_flag = _paths_from_args(
            _build_parser().parse_args(["--config-dir", str(tmp_path / "portable")])
        )

        assert by_flag.data_dir == by_env.data_dir
        assert by_flag.cache_dir == by_env.cache_dir

    def test_data_and_cache_can_be_redirected_independently(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("QM_CONFIG_DIR", str(tmp_path / "portable" / "config"))
        monkeypatch.setenv("QM_DATA_DIR", str(tmp_path / "elsewhere" / "data"))
        monkeypatch.setenv("QM_CACHE_DIR", str(tmp_path / "elsewhere" / "cache"))

        paths = default_paths()

        assert paths.data_dir == tmp_path / "elsewhere" / "data"
        assert paths.cache_dir == tmp_path / "elsewhere" / "cache"

    def test_fspath(self, tmp_path: Path) -> None:
        paths = AppPaths(tmp_path, tmp_path, tmp_path, tmp_path)
        assert str(paths) == str(tmp_path)
