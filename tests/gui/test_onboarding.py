"""Onboarding tests (WP2.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.gui.shell.onboarding import OnboardingDialog
from quantsmind_agent.services.onboarding import OnboardingState, OnboardingStore

pytestmark = pytest.mark.gui


def test_store_persists(tmp_path: Path) -> None:
    store = OnboardingStore(tmp_path)
    assert store.load().current == "provider"
    store.save(OnboardingState(provider_done=True))
    assert OnboardingStore(tmp_path).load().current == "trust"
    store.save(OnboardingState(provider_done=True, trust_done=True, profile_done=True))
    assert OnboardingStore(tmp_path).load().complete


def test_dialog_renders_steps(qapp: object, tmp_path: Path) -> None:
    dialog = OnboardingDialog(tmp_path)
    assert dialog.provider_input is not None
    assert dialog.profile_list is not None
    assert "provider" in dialog.status.text()
    dialog._skip()
    assert dialog.state.complete
    assert OnboardingStore(tmp_path).load().complete
