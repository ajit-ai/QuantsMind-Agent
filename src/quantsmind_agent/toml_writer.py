"""A very small TOML writer.

Python ships :mod:`tomllib` for *reading* TOML but no writer. QuantsMind-Agent
only ever writes a handful of scalar/table values, so a ~60-line serialiser is
cheaper than taking on a runtime dependency.

Supported value types: ``str``, ``int``, ``float``, ``bool``, ``None``, lists of
those, and nested mappings (rendered as ``[table]`` sections).
"""

from __future__ import annotations

from typing import Any

__all__ = ["dumps"]


def _format_key(key: str) -> str:
    if key and all(ch.isalnum() or ch in "_-" for ch in key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        rendered = ", ".join(_format_value(item) for item in value)
        if any("\n" in str(item) for item in value):
            body = "".join(f"    {_format_value(item)},\n" for item in value)
            return f"[\n{body}]"
        return f"[{rendered}]"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def _is_table(value: Any) -> bool:
    return isinstance(value, dict)


def _render_table(name: str, table: dict[str, Any], lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not _is_table(v) and v is not None}
    tables = {k: v for k, v in table.items() if _is_table(v)}
    if scalars or not lines:
        if lines:
            lines.append("")
        if name:
            lines.append(f"[{name}]")
        for key, value in scalars.items():
            lines.append(f"{_format_key(key)} = {_format_value(value)}")
    for key, value in tables.items():
        child = f"{name}.{_format_key(key)}" if name else _format_key(key)
        _render_table(child, value, lines)


def dumps(data: dict[str, Any]) -> str:
    """Serialise ``data`` to a TOML document."""

    lines: list[str] = []
    _render_table("", dict(data), lines)
    text = "\n".join(lines).strip("\n")
    return text + "\n" if text else ""
