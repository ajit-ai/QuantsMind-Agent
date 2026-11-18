"""ChangeSet operations (v0.11.0 WP1-B).

These tests are the refusal catalogue. Each one describes a transaction that would
damage a workspace or mislead a reviewer -- a file the user edited in the meantime,
a path that leaves the root, a rename that undoes another rename -- and asserts the
engine says no before anything is staged, not after.

Where a check is Windows-specific the platform is passed explicitly rather than
detected, so the case runs on every machine and cannot rot into a skip.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from quantsmind_agent.services.changesets.operations import (
    DEFAULT_MAX_CONTENT_BYTES,
    FILE_OPERATION_TYPES,
    MODE_POLICY_PRESERVE,
    CommandSpec,
    Fingerprint,
    Hunk,
    Operation,
    OperationStatus,
    OperationType,
    OperationValidationError,
    Risk,
    StagedContent,
    normalize_path,
    validate_operations,
)

ROOT = "root-a"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content(text: str = "print('hello')\n") -> StagedContent:
    blob = _digest(text)
    return StagedContent(
        staged_blob=f"sha256/{blob[:2]}/{blob}",
        sha256=blob,
        size=len(text.encode("utf-8")),
    )


def _fingerprint(text: str) -> Fingerprint:
    return Fingerprint(
        exists=True,
        sha256=_digest(text),
        size=len(text.encode("utf-8")),
        mode=0o644,
        mtime_ns=1_720_000_000_000_000_000,
    )


def create_op(
    *, order: int = 1, path: str = "src/new.py", text: str = "print('hello')\n"
) -> Operation:
    content = _content(text)
    return Operation(
        op_id=f"op-{order:04d}",
        order=order,
        type=OperationType.CREATE_FILE,
        root_id=ROOT,
        path=path,
        base=Fingerprint(exists=False),
        result=Fingerprint(exists=True, sha256=content.sha256, size=content.size),
        content=content,
    )


def modify_op(
    *,
    order: int = 1,
    path: str = "src/app.py",
    base_text: str = "old\n",
    text: str = "new\n",
) -> Operation:
    content = _content(text)
    return Operation(
        op_id=f"op-{order:04d}",
        order=order,
        type=OperationType.MODIFY_FILE,
        root_id=ROOT,
        path=path,
        base=_fingerprint(base_text),
        result=Fingerprint(exists=True, sha256=content.sha256, size=content.size),
        content=content,
    )


def delete_op(*, order: int = 1, path: str = "src/old.py", text: str = "gone\n") -> Operation:
    return Operation(
        op_id=f"op-{order:04d}",
        order=order,
        type=OperationType.DELETE_FILE,
        root_id=ROOT,
        path=path,
        base=_fingerprint(text),
        result=Fingerprint(exists=False),
        risk=Risk.MEDIUM,
    )


def rename_op(
    *,
    order: int = 1,
    path: str = "src/a.py",
    destination: str = "src/b.py",
    text: str = "same bytes\n",
) -> Operation:
    base = _fingerprint(text)
    return Operation(
        op_id=f"op-{order:04d}",
        order=order,
        type=OperationType.RENAME_FILE,
        root_id=ROOT,
        path=path,
        destination_path=destination,
        base=base,
        result=base,
        risk=Risk.MEDIUM,
    )


def command_op(
    *,
    order: int = 1,
    argv: tuple[str, ...] = ("pytest", "-q"),
    cwd: str = ".",
    timeout_seconds: int = 120,
    expected_exit_codes: tuple[int, ...] = (0,),
) -> Operation:
    return Operation(
        op_id=f"op-{order:04d}",
        order=order,
        type=OperationType.RUN_COMMAND,
        command=CommandSpec(
            argv=argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            expected_exit_codes=expected_exit_codes,
        ),
        risk=Risk.MEDIUM,
    )


class FakeResolver:
    """A workspace that refuses one path, so containment has a real case to catch.

    The production resolver is :class:`Workspace`; the seam here is the same
    ``resolve_for`` the apply engine calls, so the test exercises the wiring rather
    than a re-implementation of it.
    """

    def __init__(self, escapes: tuple[str, ...] = ("src/escape.py",)) -> None:
        self.escapes = escapes
        self.seen: list[tuple[str, str]] = []

    def resolve_for(self, root_id: str, relative: str) -> str:
        self.seen.append((root_id, relative))
        if relative in self.escapes:
            raise ValueError(f"'{relative}' resolves outside the workspace root")
        return f"/workspace/{relative}"


def reason(action: Any) -> str:
    """Run ``action`` and return the reason code it refuses with."""

    with pytest.raises(OperationValidationError) as raised:
        action()
    return raised.value.reason_code


# -- the authorised vocabulary ----------------------------------------------
def test_the_operation_types_are_the_five_accepted_ones() -> None:
    assert {item.value for item in OperationType} == {
        "CREATE_FILE",
        "MODIFY_FILE",
        "DELETE_FILE",
        "RENAME_FILE",
        "RUN_COMMAND",
    }
    # A command changes verification, never file state (§8.6): it must not be
    # counted where the engine decides what needs a pre-image.
    file_only = set(OperationType) - {OperationType.RUN_COMMAND}
    assert file_only == FILE_OPERATION_TYPES


def test_the_operation_statuses_are_the_six_accepted_ones() -> None:
    assert {item.value for item in OperationStatus} == {
        "PENDING",
        "COMMITTED",
        "SKIPPED",
        "FAILED",
        "ROLLED_BACK",
        "UNKNOWN",
    }


@pytest.mark.parametrize(
    "factory",
    [create_op, modify_op, delete_op, rename_op, command_op],
)
def test_one_of_each_accepted_operation_validates(factory: Any) -> None:
    operation = factory()
    assert validate_operations([operation]) == (operation,)


def test_a_transaction_of_every_kind_runs_in_order() -> None:
    operations = [create_op(), modify_op(order=2), delete_op(order=3), rename_op(order=4)]
    assert [item.op_id for item in validate_operations(operations)] == [
        "op-0001",
        "op-0002",
        "op-0003",
        "op-0004",
    ]


def test_the_declared_types_can_be_narrowed_by_the_caller() -> None:
    """A task-linked proposal may only offer file edits, so a command is refused."""

    assert reason(
        lambda: validate_operations([command_op()], allowed_types={OperationType.CREATE_FILE})
    ) == ("operation_type_unsupported")


# -- fingerprints ------------------------------------------------------------
def test_an_existing_fingerprint_without_a_digest_is_not_a_fingerprint() -> None:
    with pytest.raises(ValueError, match="sha256"):
        Fingerprint(exists=True, sha256="", size=10)


def test_an_absent_fingerprint_cannot_carry_a_digest() -> None:
    with pytest.raises(ValueError, match="absent"):
        Fingerprint(exists=False, sha256=_digest("x"))


def test_the_digest_and_size_decide_and_the_clock_does_not() -> None:
    """mtime and mode are evidence, never compare-and-swap inputs (§6.2).

    A touched-but-unchanged file is still the file the reviewer approved, and a
    re-timed file is not permission to overwrite.
    """

    approved = _fingerprint("same\n")
    retouched = Fingerprint(
        exists=True,
        sha256=approved.sha256,
        size=approved.size,
        mode=0o600,
        mtime_ns=approved.mtime_ns + 10**9,
    )
    assert approved.matches(retouched) is True

    edited = Fingerprint(
        exists=True,
        sha256=_digest("user edited it\n"),
        size=len("user edited it\n"),
        mode=approved.mode,
        mtime_ns=approved.mtime_ns,
    )
    assert approved.matches(edited) is False

    appeared = Fingerprint(exists=False)
    assert approved.matches(appeared) is False


def test_a_fingerprint_survives_the_manifest_round_trip() -> None:
    original = _fingerprint("text\n")
    assert Fingerprint.from_dict(original.to_dict()) == original


# -- per-operation semantics (§8.2-§8.6) ------------------------------------
def test_create_expects_absence_and_states_what_it_leaves_behind() -> None:
    assert reason(
        lambda: validate_operations(
            [replace_operation(create_op(), base=Fingerprint(exists=True, sha256=_digest("x")))]
        )
    ) == ("base_exists_mismatch")
    assert reason(
        lambda: validate_operations(
            [replace_operation(create_op(), result=Fingerprint(exists=False))]
        )
    ) == ("result_exists_mismatch")


def test_modify_expects_the_file_it_is_rewriting() -> None:
    assert (
        reason(
            lambda: validate_operations(
                [replace_operation(modify_op(), base=Fingerprint(exists=False))]
            )
        )
        == "base_exists_mismatch"
    )


def test_delete_expects_a_pre_image_and_leaves_nothing_and_carries_no_content() -> None:
    assert (
        reason(
            lambda: validate_operations(
                [
                    replace_operation(
                        delete_op(), result=Fingerprint(exists=True, sha256=_digest("x"))
                    )
                ]
            )
        )
        == "result_exists_mismatch"
    )
    assert reason(
        lambda: validate_operations([replace_operation(delete_op(), content=_content())])
    ) == ("content_not_allowed")


def test_create_without_content_cannot_be_verified_afterwards() -> None:
    assert reason(lambda: validate_operations([replace_operation(create_op(), content=None)])) == (
        "content_required"
    )


def test_a_blob_that_disagrees_with_the_expected_result_is_refused() -> None:
    """The staged bytes and the promise must be the same fact (§6.2).

    Otherwise the review shows one file and the commit writes another, and the
    post-write hash check compares against a number nobody approved.
    """

    lying = replace_operation(
        create_op(), result=Fingerprint(exists=True, sha256=_digest("other"), size=5)
    )
    assert reason(lambda: validate_operations([lying])) == "blob_result_mismatch"

    staged = create_op()
    assert staged.content is not None
    wrong_size = replace_operation(
        staged,
        content=replace_content(staged.content, size=staged.content.size + 1),
    )
    assert reason(lambda: validate_operations([wrong_size])) == "blob_result_mismatch"


def test_a_blob_without_a_digest_is_not_content_addressed() -> None:
    assert (
        reason(
            lambda: validate_operations(
                [
                    replace_operation(
                        create_op(),
                        content=replace_content(create_op().content, sha256="not-a-hash"),
                    )
                ]
            )
        )
        == "blob_digest_invalid"
    )
    assert (
        reason(
            lambda: validate_operations(
                [
                    replace_operation(
                        create_op(), content=replace_content(create_op().content, staged_blob="")
                    )
                ]
            )
        )
        == "blob_required"
    )


def test_content_beyond_the_per_file_cap_is_refused_before_any_pre_image_is_staged() -> None:
    oversized_content = replace_content(create_op().content, size=DEFAULT_MAX_CONTENT_BYTES + 1)
    oversized = replace_operation(
        create_op(),
        result=Fingerprint(
            exists=True,
            sha256=oversized_content.sha256,
            size=oversized_content.size,
        ),
        content=oversized_content,
    )
    assert reason(lambda: validate_operations([oversized])) == "content_too_large"


def test_binary_content_has_no_hunks() -> None:
    binary = replace_operation(
        create_op(),
        content=replace_content(
            create_op().content,
            binary=True,
            hunks=(Hunk("h1", 1, 1, 1, 1, "@@ -1,1 +1,1 @@"),),
        ),
    )
    assert reason(lambda: validate_operations([binary])) == "hunks_on_binary"


def test_rename_needs_a_destination_and_must_not_rewrite_bytes() -> None:
    assert reason(
        lambda: validate_operations([replace_operation(rename_op(), destination_path="")])
    ) == ("destination_mismatch")
    assert reason(
        lambda: validate_operations(
            [replace_operation(create_op(), destination_path="src/other.py")]
        )
    ) == ("destination_mismatch")
    assert reason(
        lambda: validate_operations([replace_operation(rename_op(), destination_path="src/a.py")])
    ) == ("rename_to_self")
    rewritten = replace_operation(
        rename_op(),
        result=Fingerprint(exists=True, sha256=_digest("different"), size=10),
    )
    assert reason(lambda: validate_operations([rewritten])) == "rename_content_changed"
    assert reason(
        lambda: validate_operations([replace_operation(rename_op(), content=_content())])
    ) == ("content_not_allowed")


def test_the_only_mode_policy_at_v1_is_preserve() -> None:
    assert MODE_POLICY_PRESERVE == "PRESERVE"
    assert reason(
        lambda: validate_operations([replace_operation(modify_op(), mode_policy="CHMOD_0600")])
    ) == ("mode_policy_unsupported")


def test_a_command_is_fixed_argv_no_shell_and_bounded() -> None:
    assert reason(lambda: validate_operations([replace_operation(command_op(), command=None)])) == (
        "command_required"
    )
    assert reason(lambda: validate_operations([command_op(argv=())])) == "argv_empty"
    assert (
        reason(lambda: validate_operations([command_op(timeout_seconds=0)])) == "timeout_required"
    )
    assert reason(lambda: validate_operations([command_op(expected_exit_codes=())])) == (
        "expected_exit_codes_required"
    )

    for injected in (
        "pytest; rm -rf /",
        "pytest && echo done",
        "pytest | tee out",
        "pytest $(whoami)",
    ):
        with pytest.raises(OperationValidationError) as raised:
            validate_operations([command_op(argv=(injected,))])
        assert raised.value.reason_code == "command_metacharacters"


def test_a_command_that_claims_file_changes_is_inconsistent() -> None:
    """§8.6: a command cannot be a file operation in disguise.

    A transaction that says "this command edits the config" would be rolled back by
    the engine pretending it had a pre-image for it.
    """

    with_path = replace_operation(command_op(), path="src/app.py")
    assert reason(lambda: validate_operations([with_path])) == "path_not_allowed"

    with_fingerprint = replace_operation(
        command_op(),
        base=_fingerprint("x\n"),
        result=_fingerprint("x\n"),
    )
    assert reason(lambda: validate_operations([with_fingerprint])) == "fingerprint_not_allowed"


def test_a_command_cwd_is_validated_like_any_other_path() -> None:
    assert reason(lambda: validate_operations([command_op(cwd="../elsewhere")])) == "path_traversal"


# -- identity and ordering --------------------------------------------------
def test_op_ids_are_their_own_order() -> None:
    assert reason(lambda: validate_operations([replace_operation(create_op(), op_id="op-7")])) == (
        "op_id_invalid"
    )
    assert (
        reason(
            lambda: validate_operations([replace_operation(create_op(order=1), op_id="op-0002")])
        )
        == "op_id_order_mismatch"
    )


def test_an_empty_transaction_is_not_a_change() -> None:
    assert reason(lambda: validate_operations([])) == "no_operations"


def test_ids_and_orders_must_be_unique_and_ascending() -> None:
    twin = create_op()
    assert reason(lambda: validate_operations([twin, replace_operation(twin, order=2)])) == (
        "op_id_duplicate"
    )
    # Two operations claiming one order cannot both keep the id/order rule, so the
    # duplicate is refused as a bad id rather than as a bad order.
    assert (
        reason(
            lambda: validate_operations(
                [create_op(order=1), replace_operation(modify_op(order=1), op_id="op-0002")]
            )
        )
        == "op_id_order_mismatch"
    )

    reversed_order = [modify_op(order=2), create_op(order=1)]
    assert reason(lambda: validate_operations(reversed_order)) == "order_not_ascending"


# -- path containment (§8.1, §14.1) -----------------------------------------
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", "path_empty"),
        ("src/a\x00b.py", "path_null_byte"),
        ("../outside.py", "path_traversal"),
        ("src/../../outside.py", "path_traversal"),
        ("/etc/passwd", "path_absolute"),
        ("C:/Windows/system32/cmd.exe", "path_absolute_drive"),
        ("src/app.py:evil", "path_invalid_character"),
        ("src/CON.py", "path_reserved_name"),
        ("CON/sub/x.py", "path_reserved_name"),
        ("src/NUL", "path_reserved_name"),
        ("src/a.py.", "path_trailing_dot"),
        ("src/a.py ", "path_trailing_dot"),
    ],
)
def test_dangerous_path_shapes_are_refused_before_resolution(
    path: str,
    expected: str,
) -> None:
    assert (
        reason(lambda: validate_operations([replace_operation(create_op(path=path))])) == expected
    )


def test_paths_are_compared_after_normalisation_not_as_written() -> None:
    """``src\\a.py`` and ``src/a.py`` are one target, and both spellings are legal."""

    windows_spelling = replace_operation(create_op(path="src\\a.py"))
    assert validate_operations([windows_spelling]) == (windows_spelling,)
    assert normalize_path("src\\a.py") == "src/a.py"
    assert normalize_path("./src/a.py") == "src/a.py"
    assert normalize_path(".") == ""
    # A dotfile keeps its dot: stripping it would retarget the operation.
    assert normalize_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"


def test_two_spellings_of_one_target_are_a_collision() -> None:
    first = create_op(path="src/a.py")
    second = modify_op(order=2, path="src\\a.py")
    assert reason(lambda: validate_operations([first, second])) == "duplicate_target"


def test_the_workspace_resolver_has_the_last_word_on_escape() -> None:
    resolver = FakeResolver(escapes=("src/escape.py",))
    assert (
        reason(lambda: validate_operations([create_op(path="src/escape.py")], resolver=resolver))
        == "path_escape"
    )
    assert (ROOT, "src/escape.py") in resolver.seen


def test_both_ends_of_a_rename_are_resolved() -> None:
    resolver = FakeResolver(escapes=("src/b.py",))
    assert reason(lambda: validate_operations([rename_op()], resolver=resolver)) == "path_escape"
    assert resolver.seen == [(ROOT, "src/a.py"), (ROOT, "src/b.py")]


# -- scope (§8.8) ------------------------------------------------------------
def test_an_operation_outside_the_declared_scope_is_refused() -> None:
    assert reason(lambda: validate_operations([create_op(path="docs/new.md")], scope=["src/"])) == (
        "out_of_scope"
    )
    assert reason(lambda: validate_operations([rename_op()], scope=["docs/"])) == "out_of_scope"


def test_scope_accepts_directories_and_globs() -> None:
    assert validate_operations([create_op(path="src/deep/new.py")], scope=["src/"])
    assert validate_operations([create_op(path="src/app.py")], scope=["src/*.py"])
    # With no scope declared the workspace containment is the only boundary.
    assert validate_operations([create_op(path="anything/at/all.py")], scope=())


# -- cross-operation collisions (§8.1, §14.1) ------------------------------
def test_two_operations_may_not_write_one_target() -> None:
    assert reason(
        lambda: validate_operations([create_op(), modify_op(order=2, path="src/new.py")])
    ) == ("duplicate_target")


def test_windows_case_insensitive_collisions_are_detected_when_asked_for() -> None:
    """``Readme.md`` and ``README.md`` are one file on the platform this ships on."""

    mac = [create_op(path="README.md"), modify_op(order=2, path="readme.md")]
    assert validate_operations(mac, case_insensitive_paths=False)

    assert reason(lambda: validate_operations(mac, case_insensitive_paths=True)) == (
        "duplicate_target"
    )


def test_a_rename_of_something_another_operation_produces_is_refused() -> None:
    chained = [
        create_op(path="src/staged.py"),
        rename_op(order=2, path="src/staged.py", destination="src/final.py"),
    ]
    assert reason(lambda: validate_operations(chained)) == "chained_rename"


def test_a_name_swap_is_refused_by_name_not_as_a_chain() -> None:
    swapped = [
        rename_op(order=1, path="src/a.py", destination="src/b.py"),
        rename_op(order=2, path="src/b.py", destination="src/a.py"),
    ]
    assert reason(lambda: validate_operations(swapped)) == "rename_swap"


def test_a_deletion_and_a_write_to_the_same_path_are_one_writer() -> None:
    conflicting = [
        delete_op(path="src/app.py"),
        modify_op(order=2, path="src/app.py"),
    ]
    assert reason(lambda: validate_operations(conflicting)) == "duplicate_target"


# -- the manifest round trip -----------------------------------------------
def test_an_operation_survives_the_manifest_round_trip() -> None:
    original = modify_op()
    payload = original.to_dict()

    assert jsonable(payload)
    assert Operation.from_dict(payload) == original


def test_a_rename_keeps_its_destination_through_the_manifest() -> None:
    original = rename_op()
    assert Operation.from_dict(original.to_dict()) == original


def test_a_command_keeps_its_argv_cwd_and_timeouts() -> None:
    original = command_op(argv=("python", "-m", "pytest"), cwd="src", timeout_seconds=30)
    assert Operation.from_dict(original.to_dict()) == original


def test_an_unknown_operation_type_in_a_manifest_is_refused_not_guessed() -> None:
    """Reading a manifest may never invent semantics the spec does not define."""

    payload = create_op().to_dict()
    payload["type"] = "WRITE_SHELL_COMMAND"
    with pytest.raises(ValueError, match="unsupported operation type"):
        Operation.from_dict(payload)


def test_progress_is_recorded_by_replacing_the_entry_not_mutating_it() -> None:
    """Operations are frozen because a digest is computed over them."""

    original = create_op()
    committed = original.with_status(
        OperationStatus.COMMITTED,
        applied_at="2026-11-05T09:00:00Z",
        actual_sha256=original.result.sha256,
    )

    assert original.status is OperationStatus.PENDING
    assert committed.status is OperationStatus.COMMITTED
    assert committed.actual_sha256 == original.result.sha256
    with pytest.raises(FrozenInstanceError):
        original.status = OperationStatus.COMMITTED  # type: ignore[misc]


def test_the_target_path_is_where_the_effect_lands() -> None:
    assert create_op(path="src/a.py").target_path == "src/a.py"
    assert rename_op().target_path == "src/b.py"
    assert delete_op().target_path == "src/old.py"


def test_a_refusal_names_the_operation_it_refused() -> None:
    """A reason code without an op id cannot be shown to a reviewer."""

    with pytest.raises(OperationValidationError) as raised:
        validate_operations([create_op(), modify_op(order=2, path="src/new.py")])
    assert raised.value.op_id == "op-0002"
    assert raised.value.reason_code == "duplicate_target"


# -- helpers ----------------------------------------------------------------
def jsonable(payload: Mapping[str, Any]) -> bool:
    import json

    return bool(json.loads(json.dumps(payload)) == payload)


def replace_operation(operation: Operation, **updates: Any) -> Operation:
    from dataclasses import replace

    return replace(operation, **updates)


def replace_content(content: StagedContent | None, **updates: Any) -> StagedContent:
    """Rewrite a staged-content entry, for the cases that must make it lie."""

    from dataclasses import replace

    assert content is not None
    return replace(content, **updates)
