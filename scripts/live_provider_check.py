"""Live provider validation, opt-in and never part of ordinary CI.

Run it deliberately::

    python scripts/live_provider_check.py

It exercises the same path the application uses -- ``ConfigManager`` for the
credential, ``providers.build_provider`` for the adapter, ``ModelGateway`` for the
request -- so a pass says something about the shipped code rather than about this
script. Bounded prompts, bounded tokens, bounded timeouts, no destructive tools.

Nothing here prints a credential, a request header, or a prompt from the user's
own workspace. The report is written to stdout as JSON and contains provider names,
capabilities, verdicts and sanitised error categories.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantsmind_agent.config import ConfigManager
from quantsmind_agent.errors import LLMError
from quantsmind_agent.llm import (
    ChatRequest,
    ModelGateway,
    StructuredOutput,
    StructuredOutputError,
)
from quantsmind_agent.paths import default_paths
from quantsmind_agent.providers import build_provider
from quantsmind_agent.schema import UserMessage

PROMPT = "Reply with exactly the word: acknowledged"
STREAM_PROMPT = "Reply with exactly the word: streamed"
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["objective", "steps"],
    "properties": {
        "objective": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "description", "tool"],
                "properties": {"id": {"type": "string"}, "tool": {"type": "string"}},
            },
        },
    },
}
TIMEOUT = 60.0
MAX_TOKENS = 64


def _gateway() -> tuple[ModelGateway, Any, str]:
    """The configured gateway, the settings behind it, and where the key came from."""

    import os

    config = ConfigManager(paths=default_paths())
    config.load()
    settings = config.config.provider
    provider = build_provider(settings, api_key=config.api_key_value, transport=None)
    source = "environment" if os.environ.get(settings.api_key_env) else "secret store"
    return ModelGateway(provider, api_key=config.api_key_value()), settings, source


def _record(results: dict[str, Any], name: str, ok: bool, **detail: Any) -> None:
    results[name] = {"result": "pass" if ok else "fail", **detail}


def check_text(gateway: ModelGateway, settings: Any, results: dict[str, Any]) -> None:
    try:
        started = time.perf_counter()
        response = gateway.complete(
            ChatRequest(
                messages=[UserMessage(content=PROMPT)],
                model=settings.model,
                max_output_tokens=16,
                temperature=0.0,
                timeout_seconds=TIMEOUT,
            )
        )
        elapsed = time.perf_counter() - started
        _record(
            results,
            "text",
            response.content.strip() == "acknowledged",
            content=response.content.strip()[:80],
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_seconds=round(elapsed, 2),
        )
    except Exception as exc:
        _record(results, "text", False, error_type=type(exc).__name__, category=_category(exc))


def check_structured(gateway: ModelGateway, settings: Any, results: dict[str, Any]) -> None:
    # The shape is stated in the prompt as well as in the schema. That is not
    # belt-and-braces for its own sake: an endpoint may honour the schema only
    # loosely, and the first run of this check proved it -- the model returned
    # steps without the required description, and the application correctly
    # refused the answer rather than accepting a plan it could not vouch for.
    request = ChatRequest(
        messages=[
            UserMessage(
                content=(
                    "Propose one step to update a greeting file. Return JSON in "
                    'exactly this shape: {"objective": "...", "steps": [{"id": '
                    '"s1", "description": "...", "tool": "write_file"}]}.'
                )
            )
        ],
        model=settings.model,
        max_output_tokens=MAX_TOKENS,
        temperature=0.0,
        timeout_seconds=TIMEOUT,
        structured=StructuredOutput(PLAN_SCHEMA, native=True),
    )
    try:
        response = gateway.complete(request)
        _record(
            results,
            "structured_output",
            isinstance(response.structured, dict),
            mode=response.structured_mode,
            keys=sorted(response.structured) if isinstance(response.structured, dict) else None,
        )
    except StructuredOutputError as exc:
        # A declared capability that the endpoint does not honour is a finding,
        # not a crash: record it as unsupported and say so.
        _record(
            results,
            "structured_output",
            False,
            unsupported=True,
            detail=str(exc)[:160],
        )
    except Exception as exc:
        _record(results, "structured_output", False, error_type=type(exc).__name__)


def check_streaming(gateway: ModelGateway, settings: Any, results: dict[str, Any]) -> None:
    try:
        started = time.perf_counter()
        chunks = list(
            gateway.stream(
                ChatRequest(
                    messages=[UserMessage(content=STREAM_PROMPT)],
                    model=settings.model,
                    max_output_tokens=16,
                    temperature=0.0,
                    timeout_seconds=TIMEOUT,
                )
            )
        )
        elapsed = time.perf_counter() - started
        text = "".join(chunk.delta for chunk in chunks)
        _record(
            results,
            "streaming",
            text.strip() == "streamed",
            chunk_count=len(chunks),
            content=text.strip()[:80],
            latency_seconds=round(elapsed, 2),
        )
    except Exception as exc:
        _record(results, "streaming", False, error_type=type(exc).__name__, category=_category(exc))


def check_plan(gateway: ModelGateway, settings: Any, results: dict[str, Any]) -> None:
    """Structured planning end to end, against the live endpoint.

    Uses the real planner and a throwaway directory holding one file, so the
    plan that comes back is validated against a workspace that actually exists.
    Nothing is written outside it and no tool is executed.
    """

    import tempfile

    from quantsmind_agent.planner import TaskPlanner

    with tempfile.TemporaryDirectory(prefix="qm-live-plan-") as folder:
        workspace = Path(folder)
        (workspace / "greeting.py").write_text("print('hello')\n", encoding="utf-8")
        planner = TaskPlanner(
            gateway=gateway,
            known_tools=frozenset({"read_file", "write_file", "edit_file"}),
            workspace=str(workspace),
            model=settings.model,
        )
        try:
            proposal = planner.propose("change the greeting in greeting.py")
        except Exception as exc:
            _record(results, "plan_proposal", False, error_type=type(exc).__name__)
            return
        if proposal.ok:
            plan = proposal.plan
            _record(
                results,
                "plan_proposal",
                True,
                native=proposal.native,
                step_count=len(plan.steps) if plan else 0,
                steps=[step.tool for step in plan.steps] if plan else [],
                objective=(plan.objective[:80] if plan else ""),
            )
        else:
            _record(
                results,
                "plan_proposal",
                False,
                problems=list(proposal.problems)[:3],
            )


def _category(exc: BaseException) -> str:
    """A sanitised name for the failure. Never its message, which may echo a URL."""

    if isinstance(exc, LLMError):
        return exc.code or type(exc).__name__
    return type(exc).__name__


def main() -> int:
    results: dict[str, Any] = {}
    try:
        gateway, settings, source = _gateway()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "the configured provider could not be built",
                    "error_type": type(exc).__name__,
                },
                indent=2,
            )
        )
        return 2

    report: dict[str, Any] = {
        "provider": settings.name,
        "base_url": settings.base_url,
        "model": settings.model,
        "credential_source": source,
        "capabilities": gateway.capabilities(settings.model).describe(),
        "checks": results,
    }

    check_text(gateway, settings, results)
    check_structured(gateway, settings, results)
    check_streaming(gateway, settings, results)
    check_plan(gateway, settings, results)

    passed = sum(1 for item in results.values() if item["result"] == "pass")
    report["passed"] = passed
    report["total"] = len(results)
    report["status"] = "pass" if passed == len(results) else "partial"
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
