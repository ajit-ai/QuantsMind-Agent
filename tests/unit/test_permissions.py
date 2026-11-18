"""Unit tests for the permission layer."""

from __future__ import annotations

import pytest

from quantsmind_agent.errors import PermissionDeniedError
from quantsmind_agent.security.permissions import (
    Decision,
    Permission,
    PermissionBroker,
    PermissionRequest,
    PermissionRule,
    StaticPermissionProvider,
)


def _request(
    permission: Permission = Permission.READ_WORKSPACE,
    tool: str = "read_file",
    target: str = "src/app.py",
) -> PermissionRequest:
    return PermissionRequest(permission=permission, tool=tool, summary="read", target=target)


class TestPermissionTaxonomy:
    def test_all_v0_1_capabilities_exist(self) -> None:
        values = {permission.value for permission in Permission}
        assert values == {
            "READ_WORKSPACE",
            "WRITE_WORKSPACE",
            "EXECUTE_COMMAND",
            "GIT",
            "NETWORK",
            "CHANGESET_APPLY",
            "CHANGESET_ROLLBACK",
        }


class TestStaticProvider:
    def test_default_policy_allows_reads(self) -> None:
        provider = StaticPermissionProvider.for_v0_1()
        assert provider.decide(_request()) is Decision.ALLOW

    @pytest.mark.parametrize(
        "permission",
        [
            Permission.WRITE_WORKSPACE,
            Permission.EXECUTE_COMMAND,
            Permission.GIT,
            Permission.NETWORK,
        ],
    )
    def test_default_policy_denies_everything_else(self, permission: Permission) -> None:
        provider = StaticPermissionProvider.for_v0_1()
        assert provider.decide(_request(permission)) is Decision.DENY

    def test_unknown_rules_default_to_deny(self) -> None:
        assert StaticPermissionProvider().decide(_request()) is Decision.DENY

    def test_tool_specific_rule_wins(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.READ_WORKSPACE, Decision.DENY))
        provider.add_rule(
            PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW, tool="read_file")
        )
        assert provider.decide(_request(tool="read_file")) is Decision.ALLOW
        assert provider.decide(_request(tool="search_content")) is Decision.DENY

    def test_target_glob_rules(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(
            PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW, target_pattern="src/*")
        )
        assert provider.decide(_request(target="src/app.py")) is Decision.ALLOW
        assert provider.decide(_request(target="secrets.txt")) is Decision.DENY

    def test_rules_property_is_a_copy(self) -> None:
        provider = StaticPermissionProvider.for_v0_1()
        assert isinstance(provider.rules, tuple)


class TestBroker:
    def test_require_allows(self) -> None:
        broker = PermissionBroker(StaticPermissionProvider.for_v0_1())
        broker.require(_request())  # must not raise

    def test_require_raises_on_deny(self) -> None:
        broker = PermissionBroker(StaticPermissionProvider.for_v0_1())
        with pytest.raises(PermissionDeniedError) as info:
            broker.require(_request(Permission.WRITE_WORKSPACE))
        assert info.value.details["permission"] == "WRITE_WORKSPACE"
        assert "not permitted" in info.value.user_message

    def test_ask_without_approver_degrades_to_deny(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.READ_WORKSPACE, Decision.ASK))
        broker = PermissionBroker(provider)
        assert broker.evaluate(_request()) is Decision.DENY

    def test_ask_with_approver_allows(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.READ_WORKSPACE, Decision.ASK))
        broker = PermissionBroker(provider, approver=lambda request: True)
        assert broker.evaluate(_request()) is Decision.ALLOW

    def test_approver_can_refuse(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.READ_WORKSPACE, Decision.ASK))
        broker = PermissionBroker(provider)
        broker.set_approver(lambda request: False)
        assert broker.evaluate(_request()) is Decision.DENY

    def test_approver_receives_the_request(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.GIT, Decision.ASK))
        seen: list[PermissionRequest] = []

        def approver(request: PermissionRequest) -> bool:
            seen.append(request)
            return True

        broker = PermissionBroker(provider, approver=approver)
        broker.evaluate(_request(permission=Permission.GIT, tool="git_status"))
        assert len(seen) == 1
        assert seen[0].tool == "git_status"

    def test_history_records_every_decision(self) -> None:
        broker = PermissionBroker(StaticPermissionProvider.for_v0_1())
        broker.evaluate(_request())
        broker.evaluate(_request(Permission.NETWORK))
        assert [decision for _, decision in broker.history] == [
            Decision.ALLOW,
            Decision.DENY,
        ]

    def test_decision_callback_is_invoked(self) -> None:
        seen: list[tuple[str, Decision]] = []
        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_1(),
            on_decision=lambda request, decision: seen.append((request.tool, decision)),
        )
        broker.evaluate(_request())
        assert seen == [("read_file", Decision.ALLOW)]

    def test_provider_can_be_replaced(self) -> None:
        broker = PermissionBroker(StaticPermissionProvider())
        assert broker.evaluate(_request()) is Decision.DENY
        broker.set_provider(StaticPermissionProvider.for_v0_1())
        assert broker.evaluate(_request()) is Decision.ALLOW
        assert isinstance(broker.provider, StaticPermissionProvider)

    def test_default_broker_uses_the_v0_1_policy(self) -> None:
        broker = PermissionBroker()
        assert broker.evaluate(_request()) is Decision.ALLOW
        assert broker.evaluate(_request(permission=Permission.NETWORK)) is Decision.DENY

    def test_strict_provider_denies_writes(self) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW))
        broker = PermissionBroker(provider)
        with pytest.raises(PermissionDeniedError):
            broker.require(_request(Permission.NETWORK))
