"""Router tests.

The laundering tests are the important ones. They describe a repaired plan that
passes ordinary validation cleanly and is still rejected, because it became valid
by relabelling the thing being checked rather than by fixing the fault.
"""

from __future__ import annotations

import copy

from planner.router import RepairAction, route, verify_repair
from planner.types import ErrorCode, ErrorFamily, Plan
from planner.validator import validate, validate_plan

MSG_SPEND = "How much did I spend in the last month?"
MSG_VAGUE = "put my leftover money in the bank"


def spend_plan_dict() -> dict:
    return {
        "understood_request": "Total expenditure over 30 days.",
        "status": "ok",
        "plan": [
            {"step": 1, "tool": "get_my_user_id", "args": {}, "depends_on": []},
            {
                "step": 2,
                "tool": "get_personal_expenditure",
                "args": {
                    "user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"},
                    "number_of_days": {"v": 30, "src": "normalized", "ev": "last month"},
                },
                "depends_on": [1],
            },
            {
                "step": 3,
                "tool": "calculator",
                "args": {"operation": {"v": "Add", "src": "default"}},
                "depends_on": [2],
                "foreach": {
                    "over": "{{step_2.transactions}}",
                    "item_field": "amount",
                    "reduce": "Add",
                    "initial": 0,
                },
            },
        ],
        "confidence": 0.95,
    }


def ungrounded_transfer_dict() -> dict:
    return {
        "understood_request": "Transfer leftover money.",
        "status": "ok",
        "plan": [
            {
                "step": 1,
                "tool": "save_my_money_to_bank",
                "args": {
                    "amount": {
                        "v": 50000,
                        "src": "user_literal",
                        "ev": "my leftover money",
                    }
                },
                "depends_on": [],
            }
        ],
        "requires_confirmation": True,
        "confidence": 0.7,
    }


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_valid_plan_is_accepted():
    decision = route(validate(spend_plan_dict(), MSG_SPEND))
    assert decision.action is RepairAction.ACCEPT
    assert decision.is_terminal


def test_structural_fault_routes_to_regenerate():
    """The evidence was fine; only the wiring was wrong."""
    plan = spend_plan_dict()
    plan["plan"][1]["depends_on"] = []
    decision = route(validate(plan, MSG_SPEND))
    assert decision.action is RepairAction.REGENERATE
    assert decision.family is ErrorFamily.STRUCTURAL


def test_grounding_fault_routes_to_clarify_not_regenerate():
    """Regeneration cannot invent a value the user never supplied."""
    decision = route(validate(ungrounded_transfer_dict(), MSG_VAGUE))
    assert decision.action is RepairAction.CLARIFY
    assert decision.family is ErrorFamily.GROUNDING


def test_policy_breach_routes_to_refuse():
    plan = ungrounded_transfer_dict()
    plan["plan"][0]["args"]["amount"] = {"v": 50000, "src": "default"}
    decision = route(validate(plan, MSG_VAGUE))
    assert decision.action is RepairAction.REFUSE
    assert decision.family is ErrorFamily.POLICY


def test_policy_outranks_structural_when_both_fire():
    plan = ungrounded_transfer_dict()
    plan["plan"][0]["args"]["amount"] = {"v": 50000, "src": "default"}
    plan["plan"][0]["tool"] = "save_my_money_to_bank"
    plan["plan"].append(
        {"step": 2, "tool": "get_account_balance", "args": {}, "depends_on": []}
    )
    decision = route(validate(plan, MSG_VAGUE))
    assert decision.family is ErrorFamily.POLICY


def test_exhausted_attempts_route_to_abstain():
    plan = spend_plan_dict()
    plan["plan"][1]["depends_on"] = []
    decision = route(validate(plan, MSG_SPEND), attempt=1, max_attempts=1)
    assert decision.action is RepairAction.ABSTAIN
    assert decision.is_terminal


def test_policy_breach_refuses_even_before_attempts_run_out():
    plan = ungrounded_transfer_dict()
    plan["plan"][0]["args"]["amount"] = {"v": 50000, "src": "default"}
    decision = route(validate(plan, MSG_VAGUE), attempt=5, max_attempts=1)
    assert decision.action is RepairAction.REFUSE


def test_feedback_is_scoped_to_the_dominant_family():
    plan = spend_plan_dict()
    plan["plan"][1]["depends_on"] = []
    decision = route(validate(plan, MSG_SPEND))
    assert decision.feedback()
    assert all("error" in item for item in decision.feedback())


# --------------------------------------------------------------------------- #
# Repair integrity: the laundering guard
# --------------------------------------------------------------------------- #

def test_relabelling_a_source_to_pass_a_check_is_rejected():
    """The plan below validates cleanly. It is still rejected."""
    original = Plan.model_validate(ungrounded_transfer_dict())
    cited = validate_plan(original, MSG_VAGUE).errors

    repaired_dict = ungrounded_transfer_dict()
    repaired_dict["plan"][0]["args"]["amount"] = {"v": 50000, "src": "step_ref"}
    repaired_dict["plan"][0]["args"]["amount"] = {
        "v": 30,
        "src": "normalized",
        "ev": "last month",
    }
    repaired = Plan.model_validate(repaired_dict)

    problems = verify_repair(original, repaired, cited)
    assert any(p.code is ErrorCode.LAUNDERED_SOURCE for p in problems)


def test_downgrade_from_step_ref_to_default_is_rejected():
    original = Plan.model_validate(spend_plan_dict())
    cited = [e for e in validate_plan(original, MSG_SPEND).errors]
    repaired_dict = spend_plan_dict()
    repaired_dict["plan"][1]["args"]["user_id"] = {"v": "usr_x", "src": "default"}
    repaired = Plan.model_validate(repaired_dict)
    problems = verify_repair(original, repaired, cited or [])
    assert any(p.code is ErrorCode.LAUNDERED_SOURCE for p in problems)


def test_upgrading_status_during_repair_is_rejected():
    original = Plan.model_validate(
        {
            "understood_request": "Ambiguous transfer.",
            "status": "needs_clarification",
            "clarification": {
                "question": "How much?",
                "options": ["Surplus", "A specific amount"],
                "pending_slot": "amount",
            },
            "plan": [],
            "confidence": 0.3,
        }
    )
    repaired = Plan.model_validate(ungrounded_transfer_dict())
    problems = verify_repair(original, repaired, [])
    assert any(p.code is ErrorCode.LAUNDERED_STATUS for p in problems)


def test_honest_structural_repair_is_accepted():
    """Fixing depends_on changes nothing about provenance, so it passes."""
    broken = spend_plan_dict()
    broken["plan"][1]["depends_on"] = []
    original = Plan.model_validate(broken)
    cited = validate_plan(original, MSG_SPEND).errors

    repaired = Plan.model_validate(spend_plan_dict())
    assert verify_repair(original, repaired, cited) == []
    assert validate_plan(repaired, MSG_SPEND).ok


def test_modifying_an_uncited_step_is_rejected():
    broken = spend_plan_dict()
    broken["plan"][1]["depends_on"] = []
    original = Plan.model_validate(broken)
    cited = validate_plan(original, MSG_SPEND).errors

    meddled = spend_plan_dict()
    meddled["plan"][2]["foreach"]["initial"] = 999
    repaired = Plan.model_validate(meddled)

    problems = verify_repair(original, repaired, cited)
    assert any(p.code is ErrorCode.UNCITED_MODIFICATION for p in problems)


def test_removing_an_uncited_step_is_rejected():
    broken = spend_plan_dict()
    broken["plan"][1]["depends_on"] = []
    original = Plan.model_validate(broken)
    cited = validate_plan(original, MSG_SPEND).errors

    trimmed = spend_plan_dict()
    trimmed["plan"] = trimmed["plan"][:2]
    repaired = Plan.model_validate(trimmed)

    problems = verify_repair(original, repaired, cited)
    assert any(p.code is ErrorCode.UNCITED_MODIFICATION for p in problems)


def test_upgrading_source_is_allowed():
    """Moving toward stronger grounding is a real fix, not laundering."""
    weak = spend_plan_dict()
    # 45 is not the documented default, so this step is genuinely faulty and
    # will be cited. Repairing it to a table-backed normalized value is honest.
    weak["plan"][1]["args"]["number_of_days"] = {"v": 45, "src": "default"}
    original = Plan.model_validate(weak)
    cited = validate_plan(original, MSG_SPEND).errors
    assert any(e.step == 2 for e in cited)

    repaired = Plan.model_validate(spend_plan_dict())
    assert verify_repair(original, repaired, cited) == []
    assert validate_plan(repaired, MSG_SPEND).ok


def test_laundering_family_routes_to_refuse():
    original = Plan.model_validate(ungrounded_transfer_dict())
    cited = validate_plan(original, MSG_VAGUE).errors
    laundered_dict = ungrounded_transfer_dict()
    laundered_dict["plan"][0]["args"]["amount"] = {
        "v": 30,
        "src": "normalized",
        "ev": "last month",
    }
    laundered = Plan.model_validate(laundered_dict)

    problems = verify_repair(original, laundered, cited)
    from planner.types import ValidationResult

    decision = route(ValidationResult(ok=False, errors=problems))
    assert decision.action is RepairAction.REFUSE
    assert decision.family is ErrorFamily.REPAIR


def test_verify_repair_does_not_mutate_inputs():
    original = Plan.model_validate(spend_plan_dict())
    repaired = Plan.model_validate(spend_plan_dict())
    before = copy.deepcopy(original.model_dump())
    verify_repair(original, repaired, [])
    assert original.model_dump() == before