"""Validator tests.

Each test names a specific failure the design exists to catch. Read together they
are the argument for the design: the grounding tests in particular describe plans
that are perfectly well-formed and would execute cleanly while being wrong.
"""

from __future__ import annotations

import copy

import pytest

from planner.types import ErrorCode, ErrorFamily
from planner.validator import validate

MSG_SPEND = "How much did I spend in the last month?"
MSG_TRANSFER = "Move 500 into my bank account"
MSG_VAGUE_TRANSFER = "put my leftover money in the bank"


def _valid_spend_plan() -> dict:
    return {
        "understood_request": "Total expenditure over the trailing 30 days.",
        "status": "ok",
        "message": None,
        "assumptions": [],
        "clarification": None,
        "plan": [
            {
                "step": 1,
                "tool": "get_my_user_id",
                "args": {},
                "depends_on": [],
                "foreach": None,
                "why": "Only source of user_id.",
            },
            {
                "step": 2,
                "tool": "get_personal_expenditure",
                "args": {
                    "user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"},
                    "number_of_days": {"v": 30, "src": "normalized", "ev": "last month"},
                },
                "depends_on": [1],
                "foreach": None,
                "why": "Trailing 30-day window.",
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
                "why": "Reduce transactions to one figure.",
            },
        ],
        "requires_confirmation": False,
        "needs_replan": False,
        "confidence": 0.96,
    }


def _valid_transfer_plan() -> dict:
    return {
        "understood_request": "Transfer 500 to the bank.",
        "status": "ok",
        "message": None,
        "assumptions": [],
        "clarification": None,
        "plan": [
            {
                "step": 1,
                "tool": "save_my_money_to_bank",
                "args": {"amount": {"v": 50000, "src": "user_literal", "ev": "Move 500"}},
                "depends_on": [],
                "foreach": None,
                "why": "Amount supplied by the user.",
            }
        ],
        "requires_confirmation": True,
        "needs_replan": False,
        "confidence": 0.92,
    }


def codes(result) -> set[str]:
    return set(result.codes())


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #

def test_valid_read_plan_passes():
    result = validate(_valid_spend_plan(), MSG_SPEND)
    assert result.ok, result.codes()


def test_valid_transfer_plan_passes():
    result = validate(_valid_transfer_plan(), MSG_TRANSFER)
    assert result.ok, result.codes()


def test_clarification_plan_passes():
    plan = {
        "understood_request": "Possibly a transfer request.",
        "status": "needs_clarification",
        "message": None,
        "assumptions": [],
        "clarification": {
            "question": "How much would you like to move?",
            "options": ["My surplus from the last 30 days", "A specific amount"],
            "pending_slot": "amount",
        },
        "plan": [],
        "requires_confirmation": False,
        "needs_replan": False,
        "confidence": 0.31,
    }
    assert validate(plan, MSG_VAGUE_TRANSFER).ok


# --------------------------------------------------------------------------- #
# Grounding: the checks that catch invented values
# --------------------------------------------------------------------------- #

def test_fabricated_user_literal_is_rejected():
    """The model claims the user named an amount. They did not."""
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {
        "v": 50000,
        "src": "user_literal",
        "ev": "transfer 500 ringgit",
    }
    result = validate(plan, "put something in my savings")
    assert not result.ok
    assert ErrorCode.EVIDENCE_NOT_IN_MESSAGE.value in codes(result)


def test_evidence_without_a_number_cannot_ground_an_amount():
    """The E6 case: 'my leftover money' is real text but grounds no figure."""
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {
        "v": 50000,
        "src": "user_literal",
        "ev": "my leftover money",
    }
    result = validate(plan, MSG_VAGUE_TRANSFER)
    assert ErrorCode.EVIDENCE_CONTAINS_NO_NUMERIC_VALUE.value in codes(result)


def test_evidence_number_must_match_the_value():
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {
        "v": 90000,
        "src": "user_literal",
        "ev": "Move 500",
    }
    result = validate(plan, MSG_TRANSFER)
    assert ErrorCode.EVIDENCE_VALUE_MISMATCH.value in codes(result)


@pytest.mark.parametrize("message,value,expected", [
    ("Move 500", 50000, True),
    ("Move 500", 500, False),
    ("Move 500.25 ringgit", 50025, True),
    ("Move 1,250.05", 125005, True),
    ("Move 50 sen", 50, True),
    ("Move 50 cents", 5000, False),
    ("Move 0.001", 0, False),
    ("Move 1,25", 12500, False),
    ("Move -500", 50000, False),
])
def test_transfer_literals_have_one_unit_conversion(message, value, expected):
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {
        "v": value, "src": "user_literal", "ev": message,
    }
    assert validate(plan, message).ok is expected


def test_non_monetary_integer_is_not_scaled():
    from planner.validator import evidence_values
    assert evidence_values("past 15 days", monetary=False) == {15}
    assert evidence_values("past 1.5 days", monetary=False) == set()


def test_user_literal_requires_evidence():
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {"v": 50000, "src": "user_literal"}
    assert ErrorCode.EVIDENCE_MISSING.value in codes(validate(plan, MSG_TRANSFER))


def test_normalization_must_come_from_the_table():
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["number_of_days"] = {
        "v": 45,
        "src": "normalized",
        "ev": "recently",
    }
    result = validate(plan, "how much did I spend recently")
    assert ErrorCode.NORMALIZATION_NOT_IN_TABLE.value in codes(result)


def test_normalization_value_must_match_the_table():
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["number_of_days"] = {
        "v": 31,
        "src": "normalized",
        "ev": "last month",
    }
    result = validate(plan, MSG_SPEND)
    assert ErrorCode.NORMALIZATION_VALUE_MISMATCH.value in codes(result)


def test_undocumented_default_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["user_id"] = {"v": "user_123", "src": "default"}
    result = validate(plan, MSG_SPEND)
    assert ErrorCode.UNDOCUMENTED_DEFAULT.value in codes(result)


def test_illegal_source_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["number_of_days"] = {"v": 30, "src": "inferred"}
    assert ErrorCode.ILLEGAL_SOURCE.value in codes(validate(plan, MSG_SPEND))


# --------------------------------------------------------------------------- #
# Write policy: the asymmetry
# --------------------------------------------------------------------------- #

def test_default_source_forbidden_on_write_tool():
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {"v": 50000, "src": "default"}
    result = validate(plan, MSG_TRANSFER)
    assert ErrorCode.DEFAULT_ON_WRITE_TOOL.value in codes(result)


def test_normalized_source_forbidden_on_write_tool():
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"] = {
        "v": 30,
        "src": "normalized",
        "ev": "last month",
    }
    result = validate(plan, MSG_TRANSFER)
    assert ErrorCode.DEFAULT_ON_WRITE_TOOL.value in codes(result)


def test_write_plan_must_set_confirmation_flag():
    plan = _valid_transfer_plan()
    plan["requires_confirmation"] = False
    assert ErrorCode.MISSING_CONFIRMATION_FLAG.value in codes(validate(plan, MSG_TRANSFER))


def test_write_plan_may_not_be_assumed_into():
    plan = _valid_transfer_plan()
    plan["status"] = "ok_with_assumptions"
    plan["assumptions"] = [
        {"field": "amount", "value": 50000, "basis": "seemed like a good amount"}
    ]
    result = validate(plan, MSG_TRANSFER)
    assert ErrorCode.ASSUMPTION_ON_WRITE_PLAN.value in codes(result)


def test_computed_amount_is_acceptable_for_a_transfer():
    """step_ref is legal on a write: the tools computed it, nobody invented it."""
    plan = _valid_spend_plan()
    plan["understood_request"] = "Transfer the computed surplus."
    plan["plan"].append(
        {
            "step": 4,
            "tool": "save_my_money_to_bank",
            "args": {"amount": {"v": "{{step_3.result}}", "src": "step_ref"}},
            "depends_on": [3],
            "foreach": None,
            "why": "Transfer explicitly requested.",
        }
    )
    plan["requires_confirmation"] = True
    assert validate(plan, "spend last month then move it to the bank").ok


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def test_hallucinated_tool_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][1]["tool"] = "get_account_balance"
    assert ErrorCode.UNKNOWN_TOOL.value in codes(validate(plan, MSG_SPEND))


def test_invented_parameter_is_rejected():
    """The classic: adding a date range to a tool that has none."""
    plan = _valid_spend_plan()
    plan["plan"].insert(
        1,
        {
            "step": 2,
            "tool": "get_personal_salary",
            "args": {
                "user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"},
                "months": {"v": 3, "src": "user_literal", "ev": "last month"},
            },
            "depends_on": [1],
            "foreach": None,
            "why": "Salary for the period.",
        },
    )
    for i, step in enumerate(plan["plan"], start=1):
        step["step"] = i
    plan["plan"][2]["depends_on"] = [1]
    plan["plan"][3]["depends_on"] = [3]
    plan["plan"][3]["foreach"]["over"] = "{{step_3.transactions}}"
    result = validate(plan, MSG_SPEND)
    assert ErrorCode.UNKNOWN_ARGUMENT.value in codes(result)


def test_missing_required_argument_is_rejected():
    plan = _valid_spend_plan()
    del plan["plan"][1]["args"]["user_id"]
    assert ErrorCode.MISSING_ARGUMENT.value in codes(validate(plan, MSG_SPEND))


def test_argument_type_mismatch_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["number_of_days"] = {
        "v": "thirty",
        "src": "normalized",
        "ev": "last month",
    }
    assert ErrorCode.ARGUMENT_TYPE_MISMATCH.value in codes(validate(plan, MSG_SPEND))


def test_identity_tool_may_not_be_called_twice():
    plan = _valid_spend_plan()
    plan["plan"].append(
        {
            "step": 4,
            "tool": "get_my_user_id",
            "args": {},
            "depends_on": [],
            "foreach": None,
            "why": "Redundant.",
        }
    )
    assert ErrorCode.DUPLICATE_IDENTITY_CALL.value in codes(validate(plan, MSG_SPEND))


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #

def test_dangling_reference_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["user_id"] = {"v": "{{step_9.user_id}}", "src": "step_ref"}
    plan["plan"][1]["depends_on"] = [9]
    assert ErrorCode.DANGLING_REFERENCE.value in codes(validate(plan, MSG_SPEND))


def test_forward_reference_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][0] = {
        "step": 1,
        "tool": "get_personal_expenditure",
        "args": {
            "user_id": {"v": "{{step_2.user_id}}", "src": "step_ref"},
            "number_of_days": {"v": 30, "src": "default"},
        },
        "depends_on": [2],
        "foreach": None,
        "why": "Backwards.",
    }
    plan["plan"][1] = {
        "step": 2,
        "tool": "get_my_user_id",
        "args": {},
        "depends_on": [],
        "foreach": None,
        "why": "Too late.",
    }
    plan["plan"][2]["foreach"]["over"] = "{{step_1.transactions}}"
    plan["plan"][2]["depends_on"] = [1]
    assert ErrorCode.FORWARD_REFERENCE.value in codes(validate(plan, MSG_SPEND))


def test_undeclared_dependency_is_rejected():
    plan = _valid_spend_plan()
    plan["plan"][1]["depends_on"] = []
    assert ErrorCode.UNDECLARED_DEPENDENCY.value in codes(validate(plan, MSG_SPEND))


def test_calculator_cannot_take_a_list_as_an_operand():
    plan = _valid_spend_plan()
    plan["plan"][2]["foreach"] = None
    plan["plan"][2]["args"] = {
        "int1": {"v": "{{step_2.transactions}}", "src": "step_ref"},
        "operation": {"v": "Add", "src": "default"},
    }
    assert ErrorCode.CALCULATOR_ARITY.value in codes(validate(plan, MSG_SPEND))


def test_folding_step_may_not_also_supply_operands():
    plan = _valid_spend_plan()
    plan["plan"][2]["args"]["int1"] = {"v": 0, "src": "default"}
    plan["plan"][2]["args"]["int2"] = {"v": 0, "src": "default"}
    assert ErrorCode.CALCULATOR_ARITY.value in codes(validate(plan, MSG_SPEND))


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #

def test_clarification_may_not_carry_a_plan():
    plan = _valid_spend_plan()
    plan["status"] = "needs_clarification"
    plan["clarification"] = {
        "question": "Which period?",
        "options": ["Last week", "Last month"],
        "pending_slot": "number_of_days",
    }
    assert ErrorCode.PLAN_NOT_EMPTY_FOR_STATUS.value in codes(validate(plan, MSG_SPEND))


def test_unsupported_requires_a_message():
    plan = {
        "understood_request": "Retrieve a credit score.",
        "status": "unsupported",
        "message": None,
        "assumptions": [],
        "clarification": None,
        "plan": [],
        "requires_confirmation": False,
        "needs_replan": False,
        "confidence": 0.9,
    }
    assert ErrorCode.MESSAGE_MISSING.value in codes(validate(plan, "what is my credit score"))


def test_assumptions_required_for_that_status():
    plan = _valid_spend_plan()
    plan["status"] = "ok_with_assumptions"
    assert ErrorCode.ASSUMPTIONS_MISSING.value in codes(validate(plan, MSG_SPEND))


# --------------------------------------------------------------------------- #
# Schema parsing
# --------------------------------------------------------------------------- #

def test_bare_scalar_argument_fails_to_parse():
    """Pre-provenance output, where args were plain values, is now malformed."""
    plan = _valid_spend_plan()
    plan["plan"][1]["args"]["number_of_days"] = 30
    assert ErrorCode.SCHEMA_MALFORMED.value in codes(validate(plan, MSG_SPEND))


def test_unknown_top_level_field_fails_to_parse():
    plan = _valid_spend_plan()
    plan["extra_field"] = "surprise"
    assert ErrorCode.SCHEMA_MALFORMED.value in codes(validate(plan, MSG_SPEND))


def test_garbage_input_fails_to_parse():
    assert not validate({"nonsense": True}, MSG_SPEND).ok


# --------------------------------------------------------------------------- #
# Error families drive routing later
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "mutate, expected_family",
    [
        (lambda p: p["plan"][1].update(tool="get_account_balance"), ErrorFamily.STRUCTURAL),
        (
            lambda p: p["plan"][1]["args"].update(
                number_of_days={"v": 45, "src": "normalized", "ev": "recently"}
            ),
            ErrorFamily.GROUNDING,
        ),
        (lambda p: p.update(status="ok_with_assumptions"), ErrorFamily.CONSISTENCY),
    ],
)
def test_errors_carry_a_routable_family(mutate, expected_family):
    plan = _valid_spend_plan()
    mutate(plan)
    result = validate(plan, MSG_SPEND)
    assert expected_family in result.families


def test_feedback_is_structured_not_prose():
    plan = _valid_transfer_plan()
    plan["plan"][0]["args"]["amount"]["ev"] = "give me a million"
    feedback = validate(plan, MSG_TRANSFER).feedback()
    assert feedback and all(isinstance(item, dict) for item in feedback)
    assert "error" in feedback[0] and "step" in feedback[0]


def test_validation_does_not_mutate_input():
    plan = _valid_spend_plan()
    before = copy.deepcopy(plan)
    validate(plan, MSG_SPEND)
    assert plan == before