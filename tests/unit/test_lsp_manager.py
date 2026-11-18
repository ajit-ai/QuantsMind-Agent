"""LSP manager + Tier-A fallback tests (WP4)."""

from __future__ import annotations

from quantsmind_agent.services.lsp.manager import LSPManager, configured_server_command


def test_no_config_is_unavailable_not_error() -> None:
    m = LSPManager(None)
    assert not m.available
    assert "no language server configured" in m.status
    assert m.ensure_started() is None
    assert m.diagnostics("file:///x.py") == []
    assert m.hover("file:///x.py", 0, 0) is None
    assert m.definition("file:///x.py", 0, 0) == []
    assert m.completion("file:///x.py", 0, 0) == []
    m.shutdown()  # safe no-op


def test_missing_binary_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("QM_LSP_COMMAND", '["definitely-not-real-lsp-xyz"]')
    assert configured_server_command(tmp_path) is None
    m = LSPManager(tmp_path)
    assert not m.available
    assert "no language server configured" in m.status


def test_tier_a_fallback_symbols() -> None:
    m = LSPManager(None)
    syms = m.symbols_fallback("def foo():\n    pass\n")
    assert any(getattr(s, "name", "") == "foo" for s in syms)


def test_cancel_stale_safe() -> None:
    m = LSPManager(None)
    m.cancel_stale()  # no hang, no raise


def test_doc_lifecycle_without_server_no_raise(tmp_path) -> None:
    m = LSPManager(tmp_path)
    m.did_open("file:///x.py", "python", "x = 1\n")
    m.did_change("file:///x.py", "x = 2\n")
    m.did_close("file:///x.py")
    m.shutdown()
