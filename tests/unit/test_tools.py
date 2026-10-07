"""Unit tests for the tool contracts and registry."""

from __future__ import annotations

from typing import Any

import pytest

from quantsmind_agent.errors import ToolFailedError, ToolNotFoundError
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.permissions import (
    Decision,
    Permission,
    PermissionBroker,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.tools import ToolRegistry, ToolResult, ToolSpec, validate_arguments


class EchoTool:
    """Minimal tool used to exercise the registry without a workspace."""

    def __init__(
        self, name: str = "echo", permission: Permission = Permission.READ_WORKSPACE
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description="Echo the message back.",
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            permission=permission,
            summary_template="echo {message}",
        )
        self.calls: list[dict[str, Any]] = []

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        self.calls.append(arguments)
        return ToolResult.success(self.spec.name, "echoed", {"text": arguments["message"]})


class BoomTool:
    """Tool that reports a controlled failure."""

    spec = ToolSpec(
        name="boom",
        description="Always fails.",
        input_schema={"type": "object", "properties": {}, "required": []},
        permission=Permission.READ_WORKSPACE,
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult.failure("boom", "boom", "expected failure")


class CrashTool:
    """Tool that raises an unexpected exception (a bug)."""

    spec = ToolSpec(
        name="crash",
        description="Raises an unexpected exception.",
        input_schema={"type": "object", "properties": {}, "required": []},
        permission=Permission.READ_WORKSPACE,
    )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom")


def _open_registry(*tools: object, **kwargs: Any) -> ToolRegistry:
    registry = ToolRegistry(
        permissions=PermissionBroker(StaticPermissionProvider.for_v0_1()), **kwargs
    )
    registry.register_all(list(tools))
    return registry


class TestToolResult:
    def test_success_and_failure(self) -> None:
        ok = ToolResult.success("t", "summary", {"a": 1})
        assert ok.ok and ok.data == {"a": 1} and ok.error is None
        bad = ToolResult.failure("t", "summary", "nope")
        assert not bad.ok and bad.error == "nope"

    def test_to_dict_omits_empty_fields(self) -> None:
        assert "data" not in ToolResult.success("t", "s").to_dict()
        assert "error" not in ToolResult.success("t", "s").to_dict()
        assert ToolResult.failure("t", "s", "e").to_dict()["error"] == "e"

    def test_observation_text(self) -> None:
        text = ToolResult.success("read_file", "read a.py", {"content": "x"}).to_observation()
        assert text.startswith("TOOL RESULT (read_file)")
        assert '"content"' in text

    def test_observation_truncates(self) -> None:
        big = ToolResult.success("t", "s", {"blob": "x" * 5000})
        text = big.to_observation(max_chars=100)
        assert "truncated" in text
        assert len(text) < 400

    def test_failure_observation(self) -> None:
        assert "TOOL ERROR" in ToolResult.failure("t", "s", "bad").to_observation()

    def test_repr(self) -> None:
        assert "ok=True" in repr(ToolResult.success("t", "s"))


class TestToolSpec:
    def test_openai_schema(self) -> None:
        schema = EchoTool().spec.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "echo"
        assert schema["function"]["parameters"]["required"] == ["message"]

    def test_summarize_uses_template(self) -> None:
        assert EchoTool().spec.summarize({"message": "hi"}) == "echo hi"

    def test_summarize_falls_back_when_template_does_not_match(self) -> None:
        assert EchoTool().spec.summarize({"unknown": 1}) == "echo"


class TestArgumentValidation:
    def test_missing_required(self) -> None:
        with pytest.raises(ToolFailedError) as info:
            validate_arguments("echo", {"properties": {"a": {}}, "required": ["a"]}, {})
        assert "missing required" in str(info.value).lower()

    def test_wrong_type(self) -> None:
        with pytest.raises(ToolFailedError):
            validate_arguments(
                "echo", {"properties": {"a": {"type": "string"}}, "required": []}, {"a": 5}
            )

    def test_bool_is_not_accepted_as_integer(self) -> None:
        with pytest.raises(ToolFailedError):
            validate_arguments(
                "echo", {"properties": {"a": {"type": "integer"}}, "required": []}, {"a": True}
            )

    def test_unknown_properties_are_ignored(self) -> None:
        validate_arguments("echo", {"properties": {}, "required": []}, {"anything": 1})

    def test_none_values_skip_the_type_check(self) -> None:
        validate_arguments(
            "echo", {"properties": {"a": {"type": "string"}}, "required": []}, {"a": None}
        )


class TestToolRegistry:
    def test_register_and_execute(self) -> None:
        tool = EchoTool()
        registry = _open_registry(tool)
        result = registry.execute("echo", {"message": "hello"})
        assert result.ok
        assert result.data["text"] == "hello"
        assert tool.calls == [{"message": "hello"}]

    def test_duplicate_registration_rejected(self) -> None:
        registry = _open_registry(EchoTool())
        with pytest.raises(ValueError):
            registry.register(EchoTool())

    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(ToolNotFoundError) as info:
            ToolRegistry().execute("nope", {})
        assert "unknown tool" in info.value.user_message.lower()

    def test_json_string_arguments(self) -> None:
        registry = _open_registry(EchoTool())
        assert registry.execute("echo", '{"message": "x"}').ok

    def test_malformed_json_arguments(self) -> None:
        registry = _open_registry(EchoTool())
        result = registry.execute("echo", "{not json")
        assert not result.ok
        assert "invalid tool arguments" in (result.error or "")

    def test_missing_required_argument_returns_failure(self) -> None:
        registry = _open_registry(EchoTool())
        assert not registry.execute("echo", {}).ok

    def test_permission_denial_prevents_execution(self) -> None:
        tool = EchoTool(permission=Permission.WRITE_WORKSPACE)
        registry = ToolRegistry()  # default policy denies writes
        registry.register(tool)
        result = registry.execute("echo", {"message": "x"})
        assert not result.ok
        assert "not permitted" in (result.error or "")
        assert tool.calls == []

    def test_allow_all_policy(self) -> None:
        provider = StaticPermissionProvider()
        for permission in Permission:
            provider.add_rule(PermissionRule(permission, Decision.ALLOW))
        registry = ToolRegistry(permissions=PermissionBroker(provider))
        registry.register(EchoTool(permission=Permission.EXECUTE_COMMAND))
        assert registry.execute("echo", {"message": "x"}).ok

    def test_tool_reported_failure_is_propagated(self) -> None:
        registry = _open_registry(BoomTool())
        result = registry.execute("boom", {})
        assert not result.ok
        assert result.error == "expected failure"

    def test_unexpected_tool_exception_is_contained(self) -> None:
        registry = _open_registry(CrashTool())
        result = registry.execute("crash", {})
        assert not result.ok
        assert "Internal error" in (result.error or "")
        assert "RuntimeError" not in (result.error or "")

    def test_events_are_emitted_in_order(self) -> None:
        bus = EventBus()
        seen: list[AgentEventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))
        _open_registry(EchoTool(), event_bus=bus, session_id="s1").execute("echo", {"message": "x"})
        assert seen == [
            AgentEventType.TOOL_REQUESTED,
            AgentEventType.PERMISSION_DECISION,
            AgentEventType.TOOL_STARTED,
            AgentEventType.TOOL_COMPLETED,
        ]

    def test_failure_events(self) -> None:
        bus = EventBus()
        seen: list[AgentEventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))
        _open_registry(BoomTool(), event_bus=bus).execute("boom", {})
        assert AgentEventType.TOOL_FAILED in seen
        assert AgentEventType.TOOL_COMPLETED not in seen

    def test_schemas_for_the_model(self) -> None:
        registry = _open_registry(EchoTool())
        assert registry.openai_schemas()[0]["function"]["name"] == "echo"
        assert "echo" in registry
        assert registry.names == ["echo"]
        assert len(registry) == 1

    def test_register_all_multiple(self) -> None:
        registry = ToolRegistry()
        registry.register_all([EchoTool("a"), EchoTool("b")])
        assert registry.names == ["a", "b"]

    def test_result_records_duration(self) -> None:
        result = _open_registry(EchoTool()).execute("echo", {"message": "x"})
        assert result.duration_ms >= 0
