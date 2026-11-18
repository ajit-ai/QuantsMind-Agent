"""v1.5 service tests."""

from __future__ import annotations

from pathlib import Path

from quantsmind_agent.services.audit_report import write_audit_report
from quantsmind_agent.services.evidence import export_evidence_pack
from quantsmind_agent.services.evidence_signing import sign_evidence_pack, verify_evidence_pack
from quantsmind_agent.services.onboarding_v2 import seed_workspace
from quantsmind_agent.services.policy_templates import PolicyTemplate, PolicyTemplateLibrary


def test_evidence_sign_verify(tmp_path: Path) -> None:
    pack = export_evidence_pack(tmp_path / "p.zip", audit_events=[], changesets=[], verification=[])
    sign_evidence_pack(pack)
    assert verify_evidence_pack(pack)


def test_policy_templates(tmp_path: Path) -> None:
    lib = PolicyTemplateLibrary(tmp_path)
    t = PolicyTemplate(name="strict", require_approval_for_writes=True)
    lib.save(t)
    assert lib.apply("strict") is not None
    import pytest

    from quantsmind_agent.services.policy_templates import PolicyTemplateError

    with pytest.raises(PolicyTemplateError):
        lib.save(PolicyTemplate(name="", max_timeout_seconds=9999))


def test_seed_idempotent(tmp_path: Path) -> None:
    first = seed_workspace(tmp_path, init_git=False)
    second = seed_workspace(tmp_path, init_git=False)
    assert first["actions"] and second["actions"] == []
    from quantsmind_agent.services.profiles import ProfileStore

    assert ProfileStore(tmp_path).get("default") is not None


def test_audit_report(tmp_path: Path) -> None:
    out = write_audit_report(tmp_path, {"approval.decision": 5})
    assert out.exists()
