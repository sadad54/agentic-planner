"""Typed failure routing and repair integrity.

Two ideas live here.

The first is that a failure should be classified before it is corrected. A
malformed plan and an ungrounded argument are both "invalid", but they call for
opposite responses: the first is a wiring mistake the model can fix from the same
information, while the second means a value does not exist and no amount of
regeneration will conjure it. Collapsing both into a generic retry throws away
precisely the information needed to choose.

The second is that a repair pass must not be able to fix a check by changing the
thing being checked. Told that a user_literal has no supporting evidence, the
cheapest available fix is to relabel it as a default. That passes validation and
lets an invented number straight through, turning the retry loop into a
laundering mechanism for the exact failure the grounding system exists to catch.
So repairs are diffed against the original and rejected on any source downgrade
or status upgrade, regardless of what the model claims it did.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .types import (
    ErrorCode,
    ErrorFamily,
    Plan,
    ValidationError,
    ValidationResult,
)


class RepairAction(str, Enum):
    ACCEPT = "accept"                  # nothing wrong
    REGENERATE = "regenerate"          # same evidence, fix the structure
    CLARIFY = "clarify"                # a value does not exist; ask the user
    REFUSE = "refuse"                  # a safety rule was broken
    ABSTAIN = "abstain"                # attempts exhausted; hand to a human


# Which family wins when several fire at once. A policy breach outranks a
# grounding gap, which outranks a wiring mistake: the most serious diagnosis
# should pick the action, not whichever error happened to be found first.
_PRECEDENCE: list[ErrorFamily] = [
    ErrorFamily.REPAIR,
    ErrorFamily.POLICY,
    ErrorFamily.GROUNDING,
    ErrorFamily.CONSISTENCY,
    ErrorFamily.STRUCTURAL,
]

_ACTION_BY_FAMILY: dict[ErrorFamily, RepairAction] = {
    ErrorFamily.REPAIR: RepairAction.REFUSE,
    ErrorFamily.POLICY: RepairAction.REFUSE,
    ErrorFamily.GROUNDING: RepairAction.CLARIFY,
    ErrorFamily.CONSISTENCY: RepairAction.REGENERATE,
    ErrorFamily.STRUCTURAL: RepairAction.REGENERATE,
}

# Sources that tie a value to something real, versus sources that infer it.
# Moving an argument from the first group to the second during a repair is the
# laundering move.
GROUNDED_SOURCES = {"user_literal", "step_ref"}
INFERRED_SOURCES = {"normalized", "default"}

_STATUS_RANK = {
    "unsupported": 0,
    "needs_clarification": 1,
    "ok_with_assumptions": 2,
    "ok": 3,
}


@dataclass
class RoutingDecision:
    action: RepairAction
    family: ErrorFamily | None
    attempt: int
    max_attempts: int
    errors: list[ValidationError] = field(default_factory=list)
    rationale: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.action in {
            RepairAction.ACCEPT,
            RepairAction.REFUSE,
            RepairAction.ABSTAIN,
        }

    def feedback(self) -> list[dict[str, Any]]:
        """What gets handed back to the model: assertions, not prose."""
        return [e.as_feedback() for e in self.errors]


def route(
    result: ValidationResult,
    *,
    attempt: int = 0,
    max_attempts: int = 1,
) -> RoutingDecision:
    """Classify a validation failure and choose a corrective action."""
    if result.ok:
        return RoutingDecision(
            action=RepairAction.ACCEPT,
            family=None,
            attempt=attempt,
            max_attempts=max_attempts,
            rationale="plan validated",
        )

    families = result.families
    dominant = next(f for f in _PRECEDENCE if f in families)
    relevant = [e for e in result.errors if e.family is dominant]

    if attempt >= max_attempts and dominant not in {
        ErrorFamily.POLICY,
        ErrorFamily.REPAIR,
    }:
        return RoutingDecision(
            action=RepairAction.ABSTAIN,
            family=dominant,
            attempt=attempt,
            max_attempts=max_attempts,
            errors=relevant,
            rationale=(
                f"{attempt} of {max_attempts} repair attempts used; handing to a human "
                f"rather than looping"
            ),
        )

    action = _ACTION_BY_FAMILY[dominant]
    return RoutingDecision(
        action=action,
        family=dominant,
        attempt=attempt,
        max_attempts=max_attempts,
        errors=relevant,
        rationale=_RATIONALE[dominant],
    )


_RATIONALE: dict[ErrorFamily, str] = {
    ErrorFamily.STRUCTURAL: (
        "wiring or signature fault; the evidence was fine, so regenerate against "
        "the same information"
    ),
    ErrorFamily.CONSISTENCY: (
        "top-level fields contradict each other; regenerate with the contradiction "
        "named"
    ),
    ErrorFamily.GROUNDING: (
        "a value has no honest source; regenerating cannot invent one, so ask the "
        "user instead"
    ),
    ErrorFamily.POLICY: (
        "a safety rule was broken; this is refused rather than repaired"
    ),
    ErrorFamily.REPAIR: (
        "the repair attempted to pass a check by altering the thing being checked; "
        "failing closed"
    ),
}


# --------------------------------------------------------------------------- #
# Repair integrity
# --------------------------------------------------------------------------- #

def verify_repair(
    original: Plan,
    repaired: Plan,
    cited: list[ValidationError],
) -> list[ValidationError]:
    """Diff a repaired plan against the original and reject dishonest fixes.

    This runs in addition to normal validation, not instead of it. Normal
    validation asks whether the new plan is valid; this asks whether it became
    valid honestly.
    """
    problems: list[ValidationError] = []
    problems += _check_status_not_upgraded(original, repaired)
    problems += _check_sources_not_downgraded(original, repaired)
    problems += _check_only_cited_steps_changed(original, repaired, cited)
    return problems


def _check_status_not_upgraded(original: Plan, repaired: Plan) -> list[ValidationError]:
    before = _STATUS_RANK.get(original.status, 0)
    after = _STATUS_RANK.get(repaired.status, 0)
    if after > before:
        return [
            ValidationError(
                code=ErrorCode.LAUNDERED_STATUS,
                field="status",
                detail=(
                    f"repair upgraded status from {original.status!r} to "
                    f"{repaired.status!r}; a repair may fix structure but may not "
                    f"decide the request is answerable after all"
                ),
            )
        ]
    return []


def _check_sources_not_downgraded(
    original: Plan, repaired: Plan
) -> list[ValidationError]:
    problems: list[ValidationError] = []
    for step in repaired.plan:
        before = original.step_by_number(step.step)
        if before is None or before.tool != step.tool:
            continue
        for name, arg in step.args.items():
            prior = before.args.get(name)
            if prior is None or prior.src == arg.src:
                continue
            if prior.src in GROUNDED_SOURCES and arg.src in INFERRED_SOURCES:
                problems.append(
                    ValidationError(
                        code=ErrorCode.LAUNDERED_SOURCE,
                        step=step.step,
                        field=f"args.{name}",
                        detail=(
                            f"repair changed src from {prior.src!r} to {arg.src!r}. "
                            f"If the evidence for a grounded value cannot be found, "
                            f"the fix is to ask the user, not to relabel the source"
                        ),
                    )
                )
    return problems


def _check_only_cited_steps_changed(
    original: Plan, repaired: Plan, cited: list[ValidationError]
) -> list[ValidationError]:
    """A repair fixes what was flagged. It does not quietly rewrite the rest."""
    cited_steps = {e.step for e in cited if e.step is not None}
    plan_level = any(e.step is None for e in cited)
    if plan_level:
        return []  # a plan-level fault legitimately touches the whole object

    problems: list[ValidationError] = []
    for step in repaired.plan:
        if step.step in cited_steps:
            continue
        before = original.step_by_number(step.step)
        if before is None:
            problems.append(
                ValidationError(
                    code=ErrorCode.UNCITED_MODIFICATION,
                    step=step.step,
                    detail="repair added a step that was not part of the cited fault",
                )
            )
        elif before.model_dump() != step.model_dump():
            problems.append(
                ValidationError(
                    code=ErrorCode.UNCITED_MODIFICATION,
                    step=step.step,
                    detail=(
                        "repair modified a step that was not flagged; only cited "
                        "fields may change"
                    ),
                )
            )

    for step in original.plan:
        if step.step in cited_steps:
            continue
        if repaired.step_by_number(step.step) is None:
            problems.append(
                ValidationError(
                    code=ErrorCode.UNCITED_MODIFICATION,
                    step=step.step,
                    detail="repair removed a step that was not part of the cited fault",
                )
            )
    return problems