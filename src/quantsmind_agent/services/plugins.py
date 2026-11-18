"""Plugin SDK alpha (WP3.4 of v1.2).

Manifest + sandboxed Python entrypoints. Plugins register **tools** only
through the permission broker; a plugin manifest must declare permissions and
the broker enforces them. No code runs at import time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.security.permissions import Permission

__all__ = ["PluginError", "PluginManifest", "load_plugin", "validate_manifest"]


class PluginError(RuntimeError):
    pass


@dataclass
class PluginManifest:
    plugin_id: str
    name: str
    version: str = "0.1.0"
    permissions: list[str] = field(default_factory=list)
    entrypoint: str = ""  # module:function

    def validate(self) -> None:
        if not self.plugin_id or not self.plugin_id.strip():
            raise PluginError("plugin_id required")
        for permission in self.permissions:
            if permission not in {p.value for p in Permission}:
                raise PluginError(f"plugin requests unknown permission: {permission}")


def validate_manifest(data: dict[str, Any]) -> PluginManifest:
    manifest = PluginManifest(
        plugin_id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        version=str(data.get("version", "0.1.0")),
        permissions=[str(p) for p in data.get("permissions", [])],
        entrypoint=str(data.get("entrypoint", "")),
    )
    manifest.validate()
    return manifest


def load_plugin(manifest_path: str | Path) -> PluginManifest:
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return validate_manifest(data)


def register_plugin_tools(manifest: PluginManifest, registry: ToolRegistry) -> int:
    """Register entrypoint tools through the permission broker.

    ``entrypoint`` is ``module:function``; it must return a list of tools
    (ToolSpec/run callables) or a single tool. Imports happen here (never at
    manifest load), failures raise :class:`PluginError`, and every tool keeps
    its declared permission so the broker still gates execution. Returns the
    number of tools registered.
    """
    manifest.validate()
    if not manifest.entrypoint:
        return 0
    module_name, _, func_name = manifest.entrypoint.partition(":")
    if not module_name or not func_name:
        raise PluginError(f"invalid entrypoint: {manifest.entrypoint!r} (expected module:function)")
    import importlib

    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, func_name)
    except (ImportError, AttributeError) as exc:
        raise PluginError(f"plugin entrypoint {manifest.entrypoint!r} unavailable: {exc}") from exc
    tools = factory()
    if not isinstance(tools, list):
        tools = [tools]
    count = 0
    for tool in tools:
        # Normalize: dict factories -> ToolSpec-like registered by name.
        if isinstance(tool, dict) and "name" in tool:
            registry.register(_DictTool(tool))
        elif hasattr(tool, "spec") or callable(tool):
            registry.register(tool)
        else:
            raise PluginError(f"plugin tool {tool!r} is not registrable")
        count += 1
    return count


class _DictTool:
    """Minimal dict-defined tool with a real ToolSpec (registry-compatible)."""

    def __init__(self, data: dict[str, Any]) -> None:
        from quantsmind_agent.runtime.tools import ToolSpec

        self._name = str(data.get("name", "plugin"))
        self.spec = ToolSpec(
            name=self._name,
            description=str(data.get("description", "plugin tool")),
            input_schema={"type": "object", "properties": {}},
            permission=Permission.READ_WORKSPACE,
            summary_template=f"plugin {self._name}",
        )

    def run(self, arguments: dict[str, Any]) -> Any:
        from quantsmind_agent.runtime.tools import ToolResult

        return ToolResult.success(self._name, f"plugin {self._name}", {"arguments": arguments})
