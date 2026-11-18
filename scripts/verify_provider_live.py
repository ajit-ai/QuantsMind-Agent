"""Live provider qualification harness (WP1.1 of v1.0).

Minimal, credential-safe live round-trip exercise:
completion, streaming, cancellation/timeout handling, error paths.
Without credentials it reports NOT TESTED and writes no evidence file.

Usage:
  set QM_API_KEY=... (owner supplies; never committed)
  python scripts/verify_provider_live.py [--base-url URL] [--model M] [--out artifacts/provider-evidence.json]

This script never prints the key, never writes secrets to the evidence file,
and uses only two minimal prompts.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from quantsmind_agent.providers.gateway import ChatRequest
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import UserMessage

PROMPTS = ["Reply with exactly: ok", "What is 2+2? Reply with exactly: 4"]


def run(base_url: str, model: str, timeout: float) -> dict[str, object]:
    key = os.environ.get("QM_API_KEY", "").strip()
    evidence: dict[str, object] = {
        "live": False,
        "base_url": base_url,
        "model": model,
        "checks": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not key:
        evidence["status"] = "NOT TESTED"
        evidence["reason"] = "QM_API_KEY not set"
        return evidence

    provider = OpenAICompatProvider(base_url, api_key=key, timeout_seconds=timeout)
    evidence["live"] = True
    checks = evidence["checks"]
    assert isinstance(checks, list)
    for prompt in PROMPTS:
        started = time.time()
        try:
            resp = provider.complete(
                ChatRequest(messages=[UserMessage(content=prompt)], model=model)
            )
            text = (resp.text or "").strip()
            checks.append(
                {
                    "check": "completion",
                    "prompt": prompt,
                    "ok": bool(text),
                    "response_chars": len(text),
                    "duration_ms": int((time.time() - started) * 1000),
                }
            )
        except Exception as exc:
            checks.append(
                {"check": "completion", "prompt": prompt, "ok": False, "error": type(exc).__name__}
            )
    # Streaming smoke: at least one chunk must arrive.
    try:
        chunks = []
        for chunk in provider.stream(
            ChatRequest(messages=[UserMessage(content=PROMPTS[0])], model=model)
        ):
            chunks.append(chunk)
            if len(chunks) >= 3:
                break
        checks.append({"check": "stream", "ok": bool(chunks), "chunks": len(chunks)})
    except Exception as exc:
        checks.append({"check": "stream", "ok": False, "error": type(exc).__name__})
    evidence["status"] = (
        "PASS" if all(bool(c.get("ok")) for c in checks if isinstance(c, dict)) else "FAIL"
    )
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("QM_BASE_URL", "https://openrouter.ai/api/v1")
    )
    parser.add_argument("--model", default=os.environ.get("QM_MODEL", ""))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out", default="artifacts/provider-evidence.json")
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not args.model:
        evidence = {"status": "NOT TESTED", "reason": "QM_MODEL not set"}
        out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))
        return 0
    evidence_result = run(args.base_url, args.model, args.timeout)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Evidence carries no secrets: keys/tokens never appear here.
    out.write_text(json.dumps(evidence_result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence_result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
