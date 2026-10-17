"""Agent Core: the provider-independent tool-calling loop.

The loop implemented here::

    User task
        -> Context Engine (bounded prompt)
        -> Model Gateway
        -> tool requests, or a final answer
        -> Tool Registry (permission-checked)
        -> observation fed back as tool messages
        -> repeat until the model answers, the limit is hit, or we cancel

The loop is synchronous and interruptible. The GUI runs it on a worker thread
and calls :meth:`AgentCore.cancel` to stop it; every iteration re-checks the
cancel flag, so cancellation takes effect within one model round-trip.

Streaming policy: tool-calling rounds always use non-streamed responses,
because partially-received tool arguments cannot be executed safely. The round
that produces the final answer streams when the provider supports it, so the
user sees text appear progressively.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from quantsmind_agent.context import ContextEngine
from quantsmind_agent.errors import QuantsMindError, ToolNotFoundError
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.llm import ChatRequest, ChatResponse, ModelGateway
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.schema import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from quantsmind_agent.session import Session
from quantsmind_agent.tools import ToolRegistry, ToolResult

__all__ = ["AgentCore", "AgentOutcome", "StreamEmitter", "TurnEvidence"]

logger = get_logger(__name__)

#: Tools whose success *provably* changed the workspace. A successful call from
#: this set is what makes a repeated read or re-run legitimate.
#:
#: ``run_command`` is deliberately absent: the application cannot know what a
#: command did, so it cannot claim the workspace changed. Treating an unknown
#: effect as evidence would let the model re-run the identical failing test
#: command until the iteration budget ran out, which is the loop this exists to
#: stop. Editing, then running the same command again, is still allowed.
_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "undo_file"})


def _call_signature(call: ToolCall) -> str:
    """A stable identity for one tool call, so a repeat can be recognised.

    Arguments are sorted by key, because the same call with the same arguments
    in a different order is still the same call.
    """

    try:
        rendered = json.dumps(call.arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - exotic arguments
        rendered = repr(sorted(call.arguments.items()))
    return f"{call.name}:{rendered}"


class StreamEmitter:
    """Callbacks used to push incremental progress to the GUI."""

    __slots__ = ("on_iteration", "on_text", "on_tool_end", "on_tool_start")

    def __init__(
        self,
        *,
        on_text: Callable[[str], None] | None = None,
        on_tool_start: Callable[[str, str], None] | None = None,
        on_tool_end: Callable[[ToolResult], None] | None = None,
        on_iteration: Callable[[int], None] | None = None,
    ) -> None:
        self.on_text = on_text
        self.on_tool_start = on_tool_start
        self.on_tool_end = on_tool_end
        self.on_iteration = on_iteration

    @classmethod
    def none(cls) -> StreamEmitter:
        return cls()


@dataclass(slots=True)
class AgentOutcome:
    """Result of one :meth:`AgentCore.run` invocation."""

    text: str = ""
    iterations: int = 0
    tool_results: list[ToolResult] = field(default_factory=list)
    cancelled: bool = False
    stopped_reason: str = "final_answer"
    error: str | None = None
    tokens: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and not self.cancelled

    @property
    def used_tools(self) -> bool:
        return bool(self.tool_results)

    def describe(self) -> str:
        state = "cancelled" if self.cancelled else (self.error or self.stopped_reason)
        return f"{self.iterations} iteration(s), {len(self.tool_results)} tool call(s), {state}"


class TurnEvidence:
    """What the agent has already tried this turn, and what changed since.

    A coding loop is edit -> run -> read the failure -> edit again. Repeating a
    step without new evidence is the failure mode that burns the iteration budget
    and the user's patience: the same test command three times in a row, the same
    file rewritten identically, the same read five times because the model forgot
    what it already saw.

    So the loop remembers each distinct call and whether anything has changed
    since it last ran. A repeat is refused *only* while nothing has changed --
    revalidating after an edit is exactly the workflow working, and blocking that
    would break the thing this is meant to protect.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}
        self._epoch = 0

    @property
    def epoch(self) -> int:
        """How many changes have landed this turn."""

        return self._epoch

    def record_change(self) -> None:
        """Note that the workspace changed, which reopens every previous call."""

        self._epoch += 1

    def is_repeat(self, signature: str) -> bool:
        """Whether this exact call already ran with nothing changed since."""

        return self._seen.get(signature) == self._epoch

    def remember(self, signature: str) -> None:
        self._seen[signature] = self._epoch


class AgentCore:
    """The first real tool-capable agent.

    Parameters
    ----------
    gateway:
        Provider-independent model access.
    tools:
        Permission-enforcing tool registry.
    context:
        Prompt assembly.
    session:
        Conversation and turn bookkeeping.
    event_bus:
        Optional bus. When omitted, the gateway's bus is reused so a single
        event stream reaches the GUI.
    max_iterations:
        Hard cap on model round-trips per user turn.
    use_streaming:
        Stream the final answer when the provider supports it.
    request_options:
        Returns ``(model, temperature, max_tokens, timeout)`` for each request,
        so the GUI can apply live configuration changes.
    cancel_event:
        Optional shared cancellation flag. When supplied, the tools that can
        block for a long time -- notably ``run_command`` -- observe the *same*
        flag as :meth:`cancel`, so pressing Stop stops a running command rather
        than only being noticed at the next model round-trip. Omitting it keeps
        the core self-contained.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        tools: ToolRegistry,
        context: ContextEngine,
        session: Session,
        *,
        event_bus: EventBus | None = None,
        max_iterations: int = 8,
        use_streaming: bool = True,
        request_options: Callable[[], dict[str, Any]] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._context = context
        self._session = session
        self._events: EventBus = event_bus if event_bus is not None else gateway.event_bus
        self._max_iterations = max(1, int(max_iterations))
        self._use_streaming = use_streaming
        self._request_options = request_options or (lambda: {})
        self._cancel = cancel_event if cancel_event is not None else threading.Event()
        #: Set when :meth:`cancel` is called before the turn's loop starts.
        self._cancel_pending = False
        self._running = False

    @property
    def cancel_event(self) -> threading.Event:
        """The shared cancellation flag, also handed to blocking tools."""

        return self._cancel

    # -- state -------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    def set_max_iterations(self, value: int) -> None:
        self._max_iterations = max(1, int(value))

    def set_streaming(self, enabled: bool) -> None:
        self._use_streaming = bool(enabled)

    def cancel(self) -> None:
        """Request cancellation.

        Takes effect at the next iteration boundary, or as soon as a streamed
        response yields. An in-flight HTTP request cannot be aborted, but a
        late answer is discarded rather than shown.

        Calling this before the turn starts cancels that turn too, which is why
        a Stop click right after Send is honoured.
        """

        if self._running:
            self._cancel.set()
        else:
            self._cancel_pending = True

    def reset_cancel(self) -> None:
        """Discard a pending cancellation (called before starting a turn)."""

        self._cancel.clear()
        self._cancel_pending = False

    def _emit(self, event_type: AgentEventType, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, payload, session_id=self._session.id)

    # -- main entry point --------------------------------------------------
    def run(self, user_text: str, *, emitter: StreamEmitter | None = None) -> AgentOutcome:
        """Execute one user turn to completion.

        Never raises for expected failures: errors are returned on the
        :class:`AgentOutcome` and mirrored as an ``agent_failed`` event.
        """

        if not user_text.strip():
            return AgentOutcome(error="The request is empty.", stopped_reason="empty_request")
        if self._running:
            return AgentOutcome(
                error="An agent turn is already running.",
                stopped_reason="already_running",
            )

        if self._cancel_pending:
            self._cancel.set()
        else:
            self._cancel.clear()
        self._cancel_pending = False
        self._running = True
        emitter = emitter or StreamEmitter.none()
        outcome = AgentOutcome()

        user_message = UserMessage(content=user_text)
        self._session.add_message(user_message)
        record = self._session.begin_turn(user_text)
        workspace = self._session.workspace
        self._emit(AgentEventType.USER_MESSAGE, {"text": user_text})
        self._emit(
            AgentEventType.AGENT_STARTED,
            {"workspace": str(workspace.root) if workspace else None},
        )

        try:
            self._loop(user_message, outcome, emitter)
        except QuantsMindError as exc:
            logger.warning("Agent turn failed: %s", exc.message)
            outcome.error = exc.user_message
            record.error = exc.user_message
            self._emit(
                AgentEventType.AGENT_FAILED,
                {"error": exc.user_message, "code": exc.code},
            )
        except Exception as exc:
            logger.exception("Unexpected agent failure")
            outcome.error = (
                "The agent hit an unexpected internal error. "
                "See the log file for details, then try again."
            )
            record.error = outcome.error
            self._emit(
                AgentEventType.AGENT_FAILED,
                {"error": outcome.error, "type": type(exc).__name__},
            )
        finally:
            self._running = False
            self._session.end_turn(record)

        if outcome.cancelled:
            self._emit(AgentEventType.AGENT_CANCELLED, {"iterations": outcome.iterations})
        elif outcome.error is None:
            self._emit(
                AgentEventType.AGENT_COMPLETED,
                {
                    "iterations": outcome.iterations,
                    "tool_calls": len(outcome.tool_results),
                    "chars": len(outcome.text),
                },
            )
        record.iterations = outcome.iterations
        record.final_text = outcome.text
        record.cancelled = outcome.cancelled
        return outcome

    # -- the loop ----------------------------------------------------------
    def _loop(
        self,
        user_message: UserMessage,
        outcome: AgentOutcome,
        emitter: StreamEmitter,
    ) -> None:
        # History for this turn excludes the message we just appended.
        prior = self._session.history_for_context()[:-1]
        tools = self._tools.openai_schemas()
        observations: list[Message] = []
        evidence = TurnEvidence()

        for iteration in range(1, self._max_iterations + 1):
            outcome.iterations = iteration
            if emitter.on_iteration:
                emitter.on_iteration(iteration)
            if self._cancel.is_set():
                outcome.cancelled = True
                outcome.stopped_reason = "cancelled"
                return

            # The final round offers no tools, so the model must write prose.
            offer_tools = iteration < self._max_iterations
            bundle = self._context.build(
                user_message.content,
                history=prior,
                tools=tools if offer_tools else [],
            )
            request = self._build_request(bundle.messages, observations, tools, offer_tools)

            if offer_tools and self._use_streaming:
                # Streaming is only used when no tool call may arrive.
                text, response = self._complete(request)
            elif offer_tools:
                text, response = self._complete(request)
            else:
                text, response = self._stream(request, emitter)

            if response.tool_calls:
                assistant = AssistantMessage(
                    content=response.content,
                    tool_calls=list(response.tool_calls),
                    model=response.model,
                    finish_reason=response.finish_reason,
                    total_tokens=response.total_tokens,
                )
                self._session.add_message(assistant)
                observations.append(assistant)
                for call in response.tool_calls:
                    observations.append(self._execute_call(call, outcome, emitter, evidence))
                continue

            self._finish(outcome, text, response)
            if outcome.cancelled:
                # The user pressed Stop while this answer was in flight. The
                # request cannot be aborted mid-flight (that would require
                # dropping the HTTP connection), so the late answer is
                # discarded rather than shown.
                self._discard_pending_answer(outcome)
                return
            self._session.add_message(
                AssistantMessage(
                    content=text,
                    model=response.model,
                    finish_reason=response.finish_reason,
                    total_tokens=response.total_tokens,
                )
            )
            return

        self._exhausted(outcome)

    def _discard_pending_answer(self, outcome: AgentOutcome) -> None:
        """Mark the turn cancelled and drop the answer the user stopped."""

        outcome.cancelled = True
        outcome.stopped_reason = "cancelled"
        outcome.text = ""
        outcome.tokens = None

    # -- request plumbing --------------------------------------------------
    def _build_request(
        self,
        base_messages: list[Message],
        observations: list[Message],
        tools: list[dict[str, Any]],
        offer_tools: bool,
    ) -> ChatRequest:
        options = self._request_options()
        return ChatRequest(
            messages=[*base_messages, *observations],
            model=str(options.get("model") or "unset-model"),
            temperature=float(options.get("temperature", 0.2)),
            max_output_tokens=int(options.get("max_output_tokens", 4096)),
            tools=tools if offer_tools else [],
            timeout_seconds=float(options.get("timeout_seconds", 120.0)),
        )

    def _complete(self, request: ChatRequest) -> tuple[str, ChatResponse]:
        response = self._gateway.complete(request)
        return response.content, response

    def _stream(self, request: ChatRequest, emitter: StreamEmitter) -> tuple[str, ChatResponse]:
        parts: list[str] = []
        calls: list[ToolCall] = []
        finish: str | None = None
        streamed = False

        try:
            for chunk in self._gateway.stream(request):
                if chunk.delta:
                    parts.append(chunk.delta)
                    streamed = True
                    if emitter.on_text:
                        emitter.on_text(chunk.delta)
                calls.extend(chunk.tool_calls)
                if chunk.finish_reason:
                    finish = chunk.finish_reason
                if self._cancel.is_set():
                    break
        except QuantsMindError:
            # A provider may not support streaming; fall back to one shot so the
            # user still gets an answer instead of an error.
            logger.info("Streaming failed; retrying this turn without streaming")
            return self._complete(request)

        text = "".join(parts)
        if not streamed and not calls:
            # Nothing arrived (empty or unsupported stream): ask once without
            # streaming so the user still gets an answer instead of nothing.
            return self._complete(request)

        response = ChatResponse(
            content=text,
            model=request.model,
            tool_calls=calls,
            finish_reason=finish,
        )
        return text, response

    def _execute_call(
        self,
        call: ToolCall,
        outcome: AgentOutcome,
        emitter: StreamEmitter,
        evidence: TurnEvidence,
    ) -> ToolMessage:
        if emitter.on_tool_start:
            emitter.on_tool_start(call.name, _short_args(call.arguments))

        signature = _call_signature(call)
        if evidence.is_repeat(signature):
            # No evidence has appeared since this call last ran, so running it
            # again cannot produce a different result. Refusing it here is what
            # keeps the loop from spinning; it is an expected, recoverable
            # outcome, reported to the model rather than raised.
            result = ToolResult.failure(
                call.name,
                call.name,
                f"'{call.name}' was already called with these exact arguments in this turn "
                "and nothing has changed since, so it would repeat the same result. "
                "Change something first, or answer with what you already know.",
            )
        else:
            evidence.remember(signature)
            try:
                result = self._tools.execute(call.name, call.arguments)
            except ToolNotFoundError as exc:
                # The model invented a tool. This is an expected, recoverable
                # mistake: report it back as an observation instead of failing the
                # whole turn, so the agent can retry with a tool that exists.
                logger.info("Agent requested an unknown tool: %s", call.name)
                result = ToolResult.failure(call.name, call.name, exc.user_message)

        if result.ok and _MUTATING_TOOLS.intersection({call.name}):
            evidence.record_change()

        outcome.tool_results.append(result)
        self._session.record_tool_result(result)
        if emitter.on_tool_end:
            emitter.on_tool_end(result)
        return ToolMessage(
            content=result.to_observation(),
            tool_name=call.name,
            tool_call_id=call.id,
            ok=result.ok,
        )

    def _finish(self, outcome: AgentOutcome, text: str, response: ChatResponse) -> None:
        outcome.text = text
        outcome.tokens = response.total_tokens
        outcome.stopped_reason = "final_answer"

    def _exhausted(self, outcome: AgentOutcome) -> None:
        outcome.stopped_reason = "max_iterations"
        if not outcome.text:
            outcome.text = (
                "The agent reached its tool-call limit without producing an answer. "
                "Try a more specific request."
            )
        self._session.add_message(AssistantMessage(content=outcome.text))


def _short_args(arguments: dict[str, Any], limit: int = 90) -> str:
    """Compact one-line rendering of tool arguments for the activity panel."""

    parts = []
    for key, value in list(arguments.items())[:3]:
        text = str(value)
        if len(text) > limit:
            text = text[: limit - 1] + "\u2026"
        parts.append(f"{key}={text}")
    rendered = ", ".join(parts)
    return f"({rendered})" if rendered else "()"
