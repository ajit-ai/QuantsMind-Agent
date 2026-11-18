"""Live provider qualification — gated, never requires network in CI (WP1.1)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from quantsmind_agent.providers.gateway import ChatRequest
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import UserMessage

pytestmark = pytest.mark.integration

LIVE = bool(os.environ.get("QM_API_KEY")) and bool(os.environ.get("QM_MODEL"))


@pytest.mark.skipif(not LIVE, reason="live provider not configured (QM_API_KEY/QM_MODEL unset)")
class TestLiveProvider:
    def test_minimal_completion(self) -> None:
        provider = OpenAICompatProvider(
            os.environ.get("QM_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=os.environ["QM_API_KEY"],
            timeout_seconds=30,
        )
        resp = provider.complete(
            ChatRequest(
                messages=[UserMessage(content="Reply with exactly: ok")],
                model=os.environ["QM_MODEL"],
            )
        )
        assert resp.text


def test_harness_reports_not_tested_without_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("QM_API_KEY", raising=False)
    monkeypatch.delenv("QM_MODEL", raising=False)
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "scripts/verify_provider_live.py", "--out", str(tmp_path / "e.json")],
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    data = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    assert data["status"] == "NOT TESTED"
    assert "QM_API_KEY" not in (tmp_path / "e.json").read_text(encoding="utf-8")
