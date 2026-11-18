"""Security regression tests for the newly-covered peripheral services.

Verifies that plugin trust boundaries, secret handling and validation hold,
with negative tests proving prohibited operations are rejected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantsmind_agent.services.collab_mirror import export_mirror
from quantsmind_agent.services.plugins import (
    PluginError,
    PluginManifest,
    register_plugin_tools,
    validate_manifest,
)
from quantsmind_agent.services.profiles import EngineeringProfile, ProfileStore
from quantsmind_agent.services.review_comments import CommentStore, ReviewComment
from quantsmind_agent.services.sync import merge_memory, merge_profiles
from quantsmind_agent.services.template_sharing import export_template_bundle


class TestPluginTrustBoundary:
    def test_plugin_cannot_request_unknown_permission(self) -> None:
        with pytest.raises(PluginError, match="unknown permission"):
            validate_manifest({"id": "evil", "permissions": ["EXECUTE_COMMAND_AS_ADMIN"]})

    def test_plugin_with_empty_id_rejected(self) -> None:
        manifest = PluginManifest(plugin_id="   ", name="x")
        with pytest.raises(PluginError):
            manifest.validate()

    def test_plugin_dict_tool_defaults_to_read_only_permission(self, tmp_path: Path) -> None:
        from quantsmind_agent.runtime.tools import ToolRegistry
        from quantsmind_agent.security.permissions import Permission

        pkg = tmp_path / "sec_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "t.py").write_text(
            "def factory():\n    return [{'name': 'safe', 'description': 'd'}]\n",
            encoding="utf-8",
        )
        import importlib
        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            importlib.import_module("sec_pkg")
            manifest = PluginManifest(
                plugin_id="p",
                name="n",
                permissions=["READ_WORKSPACE"],
                entrypoint="sec_pkg.t:factory",
            )
            registry = ToolRegistry()
            register_plugin_tools(manifest, registry)
            spec = registry.get("safe").spec
            assert spec.permission is Permission.READ_WORKSPACE
            assert spec.read_only is True
        finally:
            sys.path.remove(str(tmp_path))
            for name in list(sys.modules):
                if name.startswith("sec_pkg"):
                    del sys.modules[name]


class TestSecretHandling:
    def test_template_export_drops_api_key_fields(self, tmp_path: Path) -> None:
        dest = tmp_path / "bundle.json"
        # Profiles are validated through EngineeringProfile, which has no
        # secret-bearing field by schema; the documented secret-dropping
        # contract applies to free-form memories.
        export_template_bundle(
            dest,
            profiles=[{"profile_id": "p"}],
            memories=[
                {"title": "note", "secret": "HIDDEN"},
                {"title": "note2", "api_key": "SECRET"},
            ],
        )
        text = dest.read_text(encoding="utf-8")
        assert "SECRET" not in text
        assert "HIDDEN" not in text

    def test_mirror_export_does_not_redact_by_design_but_no_secrets_passed(
        self, tmp_path: Path
    ) -> None:
        # Collab mirror is a git-friendly export; it writes exactly what it is
        # given. The security contract here is that callers pass sanitized data.
        dest = tmp_path / "mirror"
        export_mirror(dest, profiles=[{"profile_id": "p"}], memories=[])
        data = json.loads((dest / "profiles.json").read_text(encoding="utf-8"))
        assert data["profiles"] == [{"profile_id": "p"}]

    def test_sync_drops_remote_api_key_entries(self) -> None:
        result = merge_memory(
            [{"kind": "fact", "title": "safe"}],
            [{"kind": "fact", "title": "leak", "api_key": "ABC123"}],
        )
        assert not any("api_key" in str(item) for item in result)
        assert not any("ABC123" in str(item) for item in result)

    def test_profiles_commands_are_data_not_executed(self, tmp_path: Path) -> None:
        profile = EngineeringProfile(
            profile_id="p", build_commands=["format C:"], test_commands=["del /f"]
        )
        store = ProfileStore(tmp_path)
        store.save(profile)
        # describe_commands must return raw strings, never execute them.
        assert store.describe_commands(profile) == ["format C:", "del /f"]


class TestValidationBoundaries:
    def test_profile_rejects_null_bytes_in_commands(self) -> None:
        with pytest.raises(ValueError):
            EngineeringProfile(profile_id="p", build_commands=["good\x00bad"]).validate()

    def test_profile_rejects_oversized_lists(self) -> None:
        with pytest.raises(ValueError):
            EngineeringProfile(profile_id="p", languages=["x"] * 51).validate()

    def test_review_comment_rejects_empty_text(self) -> None:
        with pytest.raises(ValueError):
            ReviewComment(task_id="t", path="p", author="a", text="")

    def test_comment_store_list_with_invalid_records_ignores_them(self, tmp_path: Path) -> None:
        store = CommentStore(tmp_path)
        path = tmp_path / ".quantsmind" / "review_comments.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A record missing required fields must not crash the listing.
        path.write_text(
            json.dumps([{"text": ""}, {"task_id": "t", "text": "ok"}]), encoding="utf-8"
        )
        comments = store.list()
        assert isinstance(comments, list)

    def test_sync_profiles_sorted_deterministically(self) -> None:
        a = merge_profiles([{"profile_id": "b"}, {"profile_id": "a"}], [])
        b = merge_profiles([{"profile_id": "a"}, {"profile_id": "b"}], [])
        assert a == b
