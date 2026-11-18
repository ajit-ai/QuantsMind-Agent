"""The ChangeSet state machine (v0.11.0 WP1-A).

QM-P09-CS-001 §4.2 is written as a table, so the tests are too: every pair in the
seventeen-by-seventeen matrix is either listed and legal or refused. A lifecycle
tested only on its happy path is a lifecycle where an illegal move -- applying an
already-applied set, approving after an amendment -- is discovered by a user whose
files it overwrote.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from quantsmind_agent.services.changesets.lifecycle import (
    CANCELLED_STATES,
    FILE_EVIDENCE_STATES,
    TERMINAL_STATES,
    TRANSITION_TABLE,
    UNSTABLE_STATES,
    ChangeSetLifecycle,
    ChangeSetStatus,
    InvalidTransition,
    TransitionFacts,
    TransitionGuardFailed,
    Trigger,
    can_transition,
    expected_trigger,
)

APPROVED_FACTS = TransitionFacts(approval_recorded=True, approval_digest_matches=True)
EVIDENCE_FACTS = TransitionFacts(file_evidence=True, ops_committed=1)

#: The values both lifecycles spell the same way. They mean different things, which
#: is why the two enums may never be interchangeable.
SHARED_VALUES = {"draft", "awaiting_approval", "approved", "verifying", "failed", "cancelled"}


def facts_for(current: ChangeSetStatus, target: ChangeSetStatus) -> TransitionFacts:
    """The minimum evidence that makes a listed move pass its §4.3 guard.

    Kept in one function so the matrix tests legality rather than guard plumbing,
    which has its own cases below.
    """

    if target is ChangeSetStatus.APPROVED:
        return APPROVED_FACTS
    if (current, target) == (ChangeSetStatus.FAILED, ChangeSetStatus.ROLLING_BACK):
        return EVIDENCE_FACTS
    return TransitionFacts()


def walk(
    statuses: list[ChangeSetStatus],
    *,
    actor: str = "test",
) -> ChangeSetLifecycle:
    """Drive a lifecycle through ``statuses`` so it arrives at the last one."""

    life = ChangeSetLifecycle()
    for target in statuses:
        source = life.status
        life.transition(
            target,
            trigger=expected_trigger(source, target) or Trigger.AMEND,
            facts=facts_for(source, target),
            actor=actor,
        )
    return life


def path_to(target: ChangeSetStatus) -> list[ChangeSetStatus]:
    """Shortest legal walk from ``DRAFT`` to ``target``, read from the table.

    Reaching a state is a precondition for testing its exits. Deriving the path from
    the same table under test means an unreachable state fails here instead of
    quietly skipping its whole row.
    """

    if target is ChangeSetStatus.DRAFT:
        return []
    seen = {ChangeSetStatus.DRAFT}
    queue: list[tuple[list[ChangeSetStatus], ChangeSetStatus]] = [([], ChangeSetStatus.DRAFT)]
    while queue:
        path, current = queue.pop(0)
        for nxt in TRANSITION_TABLE[current]:
            if nxt in seen:
                continue
            if nxt is target:
                return [*path, nxt]
            seen.add(nxt)
            queue.append(([*path, nxt], nxt))
    raise AssertionError(f"{target.value} is unreachable from DRAFT")


# -- the shape of the state set ---------------------------------------------
def test_the_lifecycle_has_exactly_the_seventeen_accepted_states() -> None:
    assert {item.value for item in ChangeSetStatus} == {
        "draft",
        "proposed",
        "awaiting_approval",
        "approved",
        "applying",
        "applied",
        "verifying",
        "verified",
        "partially_applied",
        "failed",
        "conflicted",
        "rejected",
        "cancelled",
        "superseded",
        "rolling_back",
        "rolled_back",
        "rollback_failed",
    }
    # An interrupted apply is a recovery question about APPLYING, not a state the
    # engine can retreat to; adding one would let it forget the journal existed.
    assert not any("INTERRUPT" in item.name for item in ChangeSetStatus)
    assert len(ChangeSetStatus) == 17


def test_every_state_has_a_row_in_the_table_even_when_terminal() -> None:
    assert set(TRANSITION_TABLE) == set(ChangeSetStatus)


def test_the_groupings_match_the_specification() -> None:
    unstable = {
        ChangeSetStatus.APPLYING,
        ChangeSetStatus.ROLLING_BACK,
        ChangeSetStatus.PARTIALLY_APPLIED,
    }
    with_evidence = {
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.VERIFIED,
        ChangeSetStatus.FAILED,
        ChangeSetStatus.ROLLED_BACK,
        ChangeSetStatus.ROLLBACK_FAILED,
        ChangeSetStatus.PARTIALLY_APPLIED,
    }
    abandoned = {
        ChangeSetStatus.CONFLICTED,
        ChangeSetStatus.CANCELLED,
        ChangeSetStatus.SUPERSEDED,
    }

    assert unstable == UNSTABLE_STATES
    assert with_evidence == FILE_EVIDENCE_STATES
    assert abandoned == CANCELLED_STATES
    # FAILED is excluded from terminal on purpose: with accepted file evidence it
    # still owes the user a rollback.
    assert ChangeSetStatus.FAILED not in TERMINAL_STATES


def test_only_the_two_amend_self_transitions_exist() -> None:
    loops = {
        (source, target)
        for source, targets in TRANSITION_TABLE.items()
        for target in targets
        if source is target
    }
    assert loops == {
        (ChangeSetStatus.DRAFT, ChangeSetStatus.DRAFT),
        (ChangeSetStatus.PROPOSED, ChangeSetStatus.PROPOSED),
    }


def test_the_walk_reaches_every_state_so_no_matrix_row_is_vacuous() -> None:
    for target in ChangeSetStatus:
        assert isinstance(path_to(target), list)


# -- the whole matrix -------------------------------------------------------
@pytest.mark.parametrize("source", list(ChangeSetStatus))
def test_every_listed_transition_is_accepted(source: ChangeSetStatus) -> None:
    for target, trigger in TRANSITION_TABLE[source].items():
        life = walk(path_to(source))
        assert life.status is source, f"could not reach {source}"

        record = life.transition(target, trigger=trigger, facts=facts_for(source, target))

        assert life.status is target
        assert record.from_status == source.value
        assert record.to_status == target.value
        assert record.trigger == trigger.value


@pytest.mark.parametrize("source", list(ChangeSetStatus))
def test_every_unlisted_transition_is_refused_without_change(source: ChangeSetStatus) -> None:
    for target in ChangeSetStatus:
        if target in TRANSITION_TABLE[source]:
            continue
        life = walk(path_to(source))
        before = (life.status, life.revision, life.approved, len(life.history))

        with pytest.raises(InvalidTransition) as raised:
            life.transition(target, trigger=Trigger.CANCEL, facts=APPROVED_FACTS)

        assert raised.value.current is source
        assert raised.value.target is target
        assert (life.status, life.revision, life.approved, len(life.history)) == before


@pytest.mark.parametrize("source", list(ChangeSetStatus))
def test_can_transition_agrees_with_the_table(source: ChangeSetStatus) -> None:
    for target in ChangeSetStatus:
        legal = can_transition(source, target, facts=facts_for(source, target))
        assert legal is (target in TRANSITION_TABLE[source])

    # Without the evidence a guard asks for, a listed move is still refused.
    if ChangeSetStatus.APPROVED in TRANSITION_TABLE[source]:
        assert can_transition(source, ChangeSetStatus.APPROVED) is False


def test_can_transition_refuses_a_stay_put_that_is_not_an_amendment() -> None:
    """The task helper waves self-transitions through; this lifecycle may not."""

    for status in ChangeSetStatus:
        if status in (ChangeSetStatus.DRAFT, ChangeSetStatus.PROPOSED):
            continue
        assert can_transition(status, status) is False


# -- §4.3 guards ------------------------------------------------------------
def test_approval_needs_a_record_whose_digest_matches_the_revision() -> None:
    life = walk(path_to(ChangeSetStatus.AWAITING_APPROVAL))
    incomplete = (
        TransitionFacts(),
        TransitionFacts(approval_recorded=True),
        TransitionFacts(approval_digest_matches=True),
    )
    for facts in incomplete:
        with pytest.raises(TransitionGuardFailed):
            life.transition(ChangeSetStatus.APPROVED, trigger=Trigger.APPROVE, facts=facts)
        assert life.status is ChangeSetStatus.AWAITING_APPROVAL
        assert life.approved is False

    life.transition(ChangeSetStatus.APPROVED, trigger=Trigger.APPROVE, facts=APPROVED_FACTS)
    assert life.approved is True


def test_an_amendment_clears_the_approval_and_moves_the_revision() -> None:
    life = walk(path_to(ChangeSetStatus.APPROVED))
    assert life.revision == 1

    record = life.transition(ChangeSetStatus.PROPOSED, trigger=Trigger.AMEND)

    assert life.approved is False
    assert life.revision == 2
    assert record.previous_revision == 1
    assert record.revision == 2


def test_the_old_revision_cannot_be_approved_again_after_an_amendment() -> None:
    """Approval is bound to a revision; an amendment cannot inherit one.

    This is the forged-approval shape: content changed underneath a decision made
    about different content.
    """

    life = walk(path_to(ChangeSetStatus.APPROVED))
    life.transition(ChangeSetStatus.PROPOSED, trigger=Trigger.AMEND)

    with pytest.raises(TransitionGuardFailed):
        life.transition(ChangeSetStatus.APPROVED, trigger=Trigger.APPROVE)


@pytest.mark.parametrize(
    ("target", "retained"),
    [
        (ChangeSetStatus.APPLYING, True),
        (ChangeSetStatus.CONFLICTED, True),
        (ChangeSetStatus.AWAITING_APPROVAL, False),
        (ChangeSetStatus.CANCELLED, False),
        (ChangeSetStatus.SUPERSEDED, False),
        (ChangeSetStatus.PROPOSED, False),
    ],
)
def test_leaving_approved_invalidates_except_toward_the_apply_it_was_given_for(
    target: ChangeSetStatus,
    retained: bool,
) -> None:
    life = walk(path_to(ChangeSetStatus.APPROVED))
    trigger = expected_trigger(ChangeSetStatus.APPROVED, target)
    assert trigger is not None

    life.transition(target, trigger=trigger)

    assert life.approved is retained


def test_a_preflight_conflict_commits_nothing_and_can_be_redrawn() -> None:
    life = walk(path_to(ChangeSetStatus.CONFLICTED))
    approved_after_conflict = life.approved
    assert life.status in CANCELLED_STATES

    life.transition(ChangeSetStatus.PROPOSED, trigger=Trigger.AMEND)

    assert approved_after_conflict is True
    assert life.approved is False
    assert life.revision == 2


def test_failed_only_offers_rollback_when_there_is_something_to_undo() -> None:
    dead = walk(path_to(ChangeSetStatus.FAILED))
    assert dead.status is ChangeSetStatus.FAILED

    with pytest.raises(TransitionGuardFailed):
        dead.transition(ChangeSetStatus.ROLLING_BACK, trigger=Trigger.USER_ROLLBACK)
    assert dead.status is ChangeSetStatus.FAILED

    evidenced = walk(
        [
            *path_to(ChangeSetStatus.PARTIALLY_APPLIED),
            ChangeSetStatus.FAILED,
        ]
    )
    evidenced.transition(
        ChangeSetStatus.ROLLING_BACK,
        trigger=Trigger.USER_ROLLBACK,
        facts=EVIDENCE_FACTS,
    )
    assert evidenced.status is ChangeSetStatus.ROLLING_BACK


def test_partial_apply_is_unstable_until_the_user_decides() -> None:
    life = walk(path_to(ChangeSetStatus.PARTIALLY_APPLIED))
    unstable_before = life.is_unstable
    evidence_before = life.may_have_file_evidence

    life.transition(ChangeSetStatus.FAILED, trigger=Trigger.ACCEPT_PARTIAL)

    assert life.status is ChangeSetStatus.FAILED
    assert unstable_before is True
    assert evidence_before is True
    # Still evidence, no longer unstable: the user has decided, the files have not
    # been restored.
    assert life.is_unstable is False
    assert life.may_have_file_evidence is True


# -- the trigger is part of the law ----------------------------------------
def test_a_wrongly_named_trigger_is_refused() -> None:
    """Naming matters: "amend" carries revision++ and approval clearing with it."""

    life = walk(path_to(ChangeSetStatus.PROPOSED))
    before = (life.status, life.revision, len(life.history))

    with pytest.raises(InvalidTransition) as raised:
        life.transition(ChangeSetStatus.AWAITING_APPROVAL, trigger=Trigger.SUBMIT)

    assert "caused by request_approval" in str(raised.value)
    assert (life.status, life.revision, len(life.history)) == before


def test_a_rollback_may_be_retried_and_a_settled_record_may_not_be_reopened() -> None:
    life = walk(
        [
            *path_to(ChangeSetStatus.APPLIED),
            ChangeSetStatus.ROLLING_BACK,
            ChangeSetStatus.ROLLBACK_FAILED,
        ]
    )
    life.transition(ChangeSetStatus.ROLLING_BACK, trigger=Trigger.RETRY_ROLLBACK)
    life.transition(ChangeSetStatus.ROLLED_BACK, trigger=Trigger.RESTORE_VERIFIED)

    assert life.status in TERMINAL_STATES
    for target in ChangeSetStatus:
        with pytest.raises(InvalidTransition):
            life.transition(target, trigger=Trigger.CANCEL, facts=APPROVED_FACTS)


@pytest.mark.parametrize(
    "terminal",
    [ChangeSetStatus.CANCELLED, ChangeSetStatus.SUPERSEDED, ChangeSetStatus.REJECTED],
)
def test_abandoning_a_transaction_stops_everything(terminal: ChangeSetStatus) -> None:
    life = walk(path_to(terminal))

    assert life.status is terminal
    assert TRANSITION_TABLE[terminal] == {}


def test_an_applied_transaction_cannot_be_applied_twice() -> None:
    life = walk(path_to(ChangeSetStatus.APPLIED))

    with pytest.raises(InvalidTransition):
        life.transition(
            ChangeSetStatus.APPLYING,
            trigger=Trigger.PREFLIGHT_PASS,
            facts=APPROVED_FACTS,
        )
    assert life.status is ChangeSetStatus.APPLIED


def test_inconclusive_verification_returns_to_applied_never_to_verified() -> None:
    life = walk(path_to(ChangeSetStatus.VERIFYING))
    life.transition(ChangeSetStatus.APPLIED, trigger=Trigger.VERIFICATION_INCONCLUSIVE)

    assert life.status is ChangeSetStatus.APPLIED
    # APPLIED -> VERIFIED is not listed: a pass has to be produced, not assumed.
    assert expected_trigger(ChangeSetStatus.APPLIED, ChangeSetStatus.VERIFIED) is None


# -- no way around it --------------------------------------------------------
def test_status_and_revision_have_no_public_setter() -> None:
    life = ChangeSetLifecycle()
    with pytest.raises(AttributeError):
        life.status = ChangeSetStatus.APPLIED  # type: ignore[misc]
    with pytest.raises(AttributeError):
        life.revision = 99  # type: ignore[misc]


def test_the_accepted_move_is_recorded_in_a_form_the_journal_can_carry() -> None:
    life = walk(path_to(ChangeSetStatus.AWAITING_APPROVAL))
    record = life.transition(
        ChangeSetStatus.APPROVED,
        trigger=Trigger.APPROVE,
        facts=APPROVED_FACTS,
        actor="user:owner",
        detail="digest verified",
    )
    payload = record.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["from_status"] == "awaiting_approval"
    assert payload["actor"] == "user:owner"
    assert payload["sequence"] == len(life.history)
    assert inspect.signature(ChangeSetLifecycle.transition).parameters["at"].default is None


def test_a_timestamp_supplied_by_the_caller_is_kept_verbatim() -> None:
    """Recovery orders transitions against journal writes, so the clock must be settable."""

    life = ChangeSetLifecycle()
    record = life.transition(
        ChangeSetStatus.PROPOSED,
        trigger=Trigger.SUBMIT,
        at="2026-11-05T09:00:00Z",
    )
    assert record.at == "2026-11-05T09:00:00Z"


def test_history_is_a_tuple_so_a_caller_cannot_rewrite_the_trail() -> None:
    life = walk(path_to(ChangeSetStatus.PROPOSED))
    assert isinstance(life.history, tuple)
    assert life.history == life.history


def test_a_lifecycle_rebuilt_from_a_manifest_keeps_its_identity() -> None:
    """Recovery restores persisted facts; a fresh object must not invent better ones.

    A restarted process that forgot the revision would renumber an amendment from 1
    and let it be approved as though the user had seen it.
    """

    life = walk(path_to(ChangeSetStatus.APPROVED))
    life.transition(ChangeSetStatus.PROPOSED, trigger=Trigger.AMEND)

    restored = ChangeSetLifecycle(
        life.status,
        revision=life.revision,
        approved=life.approved,
        history=life.history,
    )

    assert restored.status is ChangeSetStatus.PROPOSED
    assert restored.revision == 2
    assert restored.approved is False
    assert len(restored.history) == len(life.history) == 3
    assert restored.history[-1].approval_invalidated is True


def test_a_revision_below_one_is_not_a_revision() -> None:
    with pytest.raises(ValueError, match="revision"):
        ChangeSetLifecycle(ChangeSetStatus.DRAFT, revision=0)


# -- independence from the task lifecycle -----------------------------------
def test_the_two_lifecycles_stay_distinct_types_even_where_values_match() -> None:
    from quantsmind_agent.services.tasks.models import TaskStatus

    shared = {item.value for item in TaskStatus} & {item.value for item in ChangeSetStatus}
    assert shared == SHARED_VALUES
    # Same spelling, different promise: task "approved" says nothing about files, so
    # the two enums must never be interchangeable.
    assert type(TaskStatus.DRAFT) is TaskStatus
    assert type(ChangeSetStatus.DRAFT) is ChangeSetStatus
    assert len({TaskStatus, ChangeSetStatus}) == 2


def test_the_lifecycle_module_never_imports_the_task_service() -> None:
    source = Path(inspect.getfile(ChangeSetLifecycle)).read_text(encoding="utf-8")
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    assert not [name for name in names if "services.tasks" in name]
