"""What a failed request means, and therefore whether it may be retried.

v0.6's router decided fallback from the *error class* alone. That is enough for a
request whose only effect is an answer, and not enough once a stream is involved:
a stream that fails after delivering text has already given the caller part of its
response, and restarting it somewhere else silently replaces half an answer with a
whole different one.

So the distinction this module makes is not "which endpoint" but "what is already
true". Three states matter, and only the first is safe to retry:

``before_dispatch``   nothing was sent. The provider provably did no work.
``partial_output``    the caller has already seen content. Retrying is a lie.
``ambiguous``         the request was sent and the outcome is unknown. It may have
                      run to completion; a retry can duplicate it.

Everything else -- an explicit rejection, a missing credential, a cancelled turn,
an expired deadline -- is not retryable for a different reason: retrying it would
either conceal the fault or ignore a decision the user made.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["DeliveryState", "RetryDecision", "classify_stream_failure"]


class DeliveryState(StrEnum):
    """How much of a response the caller has already been given."""

    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


class RetryDecision(StrEnum):
    """Whether this failure may be tried again, and on what terms."""

    #: The provider provably did no work; another endpoint may serve it.
    SAFE = "safe"
    #: The caller already has content; restarting would replace it silently.
    NO_RESTART = "no_restart"
    #: The work may have happened. Re-sending could do it twice.
    AMBIGUOUS = "ambiguous"
    #: Trying again would conceal a fault or ignore a decision.
    TERMINAL = "terminal"


#: Failures that are never retried anywhere, dispatched or not. A credential that
#: was rejected, a model the endpoint does not have, and a response that was
#: malformed are all facts about *this* endpoint, and another endpoint would only
#: hide them.
TERMINAL_FAILURES = frozenset({"authentication", "configuration", "protocol"})


def classify_stream_failure(
    *,
    delivered: DeliveryState,
    dispatched: bool,
    failure_kind: str,
    cancelled: bool = False,
    deadline_expired: bool = False,
) -> RetryDecision:
    """Decide whether a failed stream may be served again.

    The order of the questions is the point. Cancellation and deadline come first
    because they are decisions, not faults: no endpoint may be asked to continue
    work the user stopped or the task ran out of time for. Then failures that are
    about this endpoint, then what the caller already holds, then what the
    provider might already have done.
    """

    if cancelled or deadline_expired:
        return RetryDecision.TERMINAL
    if delivered is DeliveryState.COMPLETE:
        return RetryDecision.NO_RESTART
    if delivered is DeliveryState.PARTIAL:
        # Text has already reached the conversation view. Continuing elsewhere is
        # not recovery; it is a different answer presented as the same one.
        return RetryDecision.NO_RESTART
    if failure_kind in TERMINAL_FAILURES:
        return RetryDecision.TERMINAL
    if not dispatched:
        return RetryDecision.SAFE
    # Nothing delivered, but the request was sent. The provider may have completed
    # it and lost the response on the way back.
    if failure_kind in {"rate_limit", "server"}:
        # A refusal is reported without work being done, so these remain safe.
        return RetryDecision.SAFE
    return RetryDecision.AMBIGUOUS
