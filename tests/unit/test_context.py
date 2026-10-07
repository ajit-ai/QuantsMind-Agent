"""Unit tests for the Context Engine."""

from __future__ import annotations

from pathlib import Path

from quantsmind_agent.context import DEFAULT_SYSTEM_PROMPT, ContextEngine
from quantsmind_agent.schema import AssistantMessage, Message, MessageRole, UserMessage
from quantsmind_agent.workspace import Workspace


class TestSystemPrompt:
    def test_states_the_ground_rules(self) -> None:
        prompt = ContextEngine().system_prompt()
        assert "QuantsMind-Agent" in prompt
        assert "relative to the workspace root" in prompt
        assert "cannot run commands" in prompt

    def test_mentions_every_v0_1_tool(self) -> None:
        for tool in ("project_structure", "read_file", "search_content", "list_directory"):
            assert tool in DEFAULT_SYSTEM_PROMPT

    def test_workspace_summary_is_included(self, workspace: Workspace) -> None:
        prompt = ContextEngine(workspace).system_prompt()
        assert str(workspace.root) in prompt
        assert "README.md" in prompt

    def test_custom_prompt_is_appended(self) -> None:
        engine = ContextEngine(custom_prompt="Always answer in French.")
        assert "Operator instructions" in engine.system_prompt()
        assert "Always answer in French." in engine.system_prompt()


class TestWorkspaceSummary:
    def test_without_a_workspace(self) -> None:
        assert "No project is currently open" in ContextEngine().workspace_summary()

    def test_lists_top_level_entries(self, workspace: Workspace) -> None:
        summary = ContextEngine(workspace).workspace_summary()
        assert "src/" in summary
        assert "README.md" in summary

    def test_is_cached_until_the_workspace_changes(
        self, workspace: Workspace, sample_project: Path
    ) -> None:
        engine = ContextEngine(workspace)
        first = engine.workspace_summary()
        (sample_project / "brand_new.txt").write_text("x", encoding="utf-8")
        assert engine.workspace_summary() == first
        engine.set_workspace(Workspace(sample_project))
        assert "brand_new.txt" in engine.workspace_summary()

    def test_ignored_directories_are_hidden(self, workspace: Workspace) -> None:
        summary = ContextEngine(workspace).workspace_summary()
        assert "node_modules" not in summary
        assert ".git" not in summary

    def test_can_be_switched(self, sample_project: Path) -> None:
        engine = ContextEngine()
        assert "No project" in engine.workspace_summary()
        engine.set_workspace(Workspace(sample_project))
        assert "README.md" in engine.workspace_summary()

    def test_workspace_property(self, workspace: Workspace) -> None:
        engine = ContextEngine(workspace)
        assert engine.workspace is workspace


class TestBuild:
    def test_system_first_user_last(self, workspace: Workspace) -> None:
        bundle = ContextEngine(workspace).build("What does this do?", history=[])
        assert bundle.messages[0].role is MessageRole.SYSTEM
        assert bundle.messages[-1].role is MessageRole.USER
        assert bundle.messages[-1].content == "What does this do?"

    def test_tools_are_carried(self, workspace: Workspace) -> None:
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        assert ContextEngine(workspace).build("q", tools=tools).tools == tools

    def test_history_is_trimmed_to_turns(self, workspace: Workspace) -> None:
        history: list[Message] = []
        for index in range(10):
            history.append(UserMessage(content=f"q{index}"))
            history.append(AssistantMessage(content=f"a{index}"))
        bundle = ContextEngine(workspace, history_turns=2).build("now", history=history)
        users = [m for m in bundle.messages if m.role is MessageRole.USER]
        assert len(users) == 3  # two kept plus the new question

    def test_history_can_be_disabled(self, workspace: Workspace) -> None:
        engine = ContextEngine(workspace, history_turns=0)
        history = [UserMessage(content="old"), AssistantMessage(content="reply")]
        assert len(engine.build("new", history=history).messages) == 2

    def test_oversized_history_is_dropped(self, workspace: Workspace) -> None:
        engine = ContextEngine(workspace, history_turns=100, max_context_chars=50)
        history = [UserMessage(content="x" * 5000), AssistantMessage(content="y" * 5000)]
        assert len(engine.build("q", history=history).messages) == 2

    def test_workspace_can_be_excluded(self, workspace: Workspace) -> None:
        bundle = ContextEngine(workspace).build("q", include_workspace=False)
        assert "Current workspace" not in bundle.messages[0].content

    def test_reports_an_estimate(self, workspace: Workspace) -> None:
        bundle = ContextEngine(workspace).build("hello")
        assert bundle.estimated_chars > 0
        assert "messages" in bundle.describe()

    def test_bundle_exposes_the_summary(self, workspace: Workspace) -> None:
        bundle = ContextEngine(workspace).build("q")
        assert str(workspace.root) in bundle.workspace_summary
