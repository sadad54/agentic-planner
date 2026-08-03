"""Orchestrator tests.

The confirmation-gate tests are the ones that matter: they assert that an
unconfirmed write plan performs no work at all, not merely that it skips the
transfer.
"""

from __future__ import annotations

import pytest

from planner.orchestrator import ExecutionOutcome, Orchestrator
from planner.tools import USER_ID, ToolRegistry
from planner.types import Plan

MSG_SPEND = "How much did I spend in the last month?"
MSG_TRANSFER = "Move 500 into my bank account"


def spend_plan() -> Plan:
    return Plan.model_validate(
        {
            "understood_request": "Total expenditure over 30 days.",
            "status": "ok",
            "plan": [
                {"step": 1, "tool": "get_my_user_id", "args": {}, "depends_on": []},
                {
                    "step": 2,
                    "tool": "get_personal_expenditure",
                    "args": {
                        "user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"},
                        "number_of_days": {
                            "v": 30, "src": "normalized", "ev": "last month"
                        },
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
    )


def transfer_plan() -> Plan:
    return Plan.model_validate(
        {
            "understood_request": "Transfer 500.",
            "status": "ok",
            "plan": [
                {
                    "step": 1,
                    "tool": "save_my_money_to_bank",
                    "args": {
                        "amount": {"v": 50000, "src": "user_literal", "ev": "Move 500"}
                    },
                    "depends_on": [],
                }
            ],
            "requires_confirmation": True,
            "confidence": 0.92,
        }
    )


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def test_plan_executes_and_resolves_references():
    result = Orchestrator().run(spend_plan(), MSG_SPEND)
    assert result.ok, result.detail
    assert result.steps[0].output is not None
    assert result.steps[0].output["user_id"] == USER_ID
    assert result.steps[1].resolved_args is not None
    assert result.steps[1].resolved_args["user_id"] == USER_ID


def test_fold_expands_into_real_calculator_calls():
    registry = ToolRegistry()
    result = Orchestrator(registry).run(spend_plan(), MSG_SPEND)
    assert result.ok, result.detail
    fold_step = result.steps[-1]
    assert result.steps[1].output is not None
    transactions = result.steps[1].output["transactions"]
    assert fold_step.expanded_calls == len(transactions)
    assert registry.calls["calculator"] == len(transactions)


def test_fold_result_matches_a_plain_sum():
    result = Orchestrator().run(spend_plan(), MSG_SPEND)
    assert result.ok, result.detail
    assert result.steps[1].output is not None
    assert result.final is not None
    transactions = result.steps[1].output["transactions"]
    expected = sum(t["amount"] for t in transactions)
    assert result.final["result"] == expected


def test_execution_is_deterministic_across_runs():
    a = Orchestrator(ToolRegistry()).run(spend_plan(), MSG_SPEND)
    b = Orchestrator(ToolRegistry()).run(spend_plan(), MSG_SPEND)
    assert a.final == b.final


def test_tool_call_count_is_reported():
    result = Orchestrator().run(spend_plan(), MSG_SPEND)
    assert result.tool_calls > 2


# --------------------------------------------------------------------------- #
# Confirmation gate
# --------------------------------------------------------------------------- #

def test_unconfirmed_write_plan_does_no_work_at_all():
    registry = ToolRegistry()
    result = Orchestrator(registry).run(transfer_plan(), MSG_TRANSFER)
    assert result.outcome == ExecutionOutcome.AWAITING_CONFIRMATION
    assert registry.total_calls == 0


def test_confirmed_write_plan_executes():
    registry = ToolRegistry()
    result = Orchestrator(registry).run(transfer_plan(), MSG_TRANSFER, confirmed=True)
    assert result.ok
    assert result.final is not None
    assert result.final["transferred"] == 50000


def test_gate_halts_reads_that_precede_a_write():
    """The whole plan stops, not just the transfer step."""
    plan = spend_plan()
    plan.plan.append(
        Plan.model_validate(
            {
                "understood_request": "x",
                "status": "ok",
                "plan": [
                    {
                        "step": 4,
                        "tool": "save_my_money_to_bank",
                        "args": {"amount": {"v": "{{step_3.result}}", "src": "step_ref"}},
                        "depends_on": [3],
                    }
                ],
                "confidence": 0.9,
            }
        ).plan[0]
    )
    plan.requires_confirmation = True
    registry = ToolRegistry()
    result = Orchestrator(registry).run(
        plan, "spend last month then move it across", revalidate=False
    )
    assert result.outcome == ExecutionOutcome.AWAITING_CONFIRMATION
    assert registry.total_calls == 0


# --------------------------------------------------------------------------- #
# Refusal to execute
# --------------------------------------------------------------------------- #

def test_invalid_plan_is_not_executed():
    plan = spend_plan()
    plan.plan[1].args["user_id"].v = "usr_made_up"
    plan.plan[1].args["user_id"].src = "user_literal"
    plan.plan[1].args["user_id"].ev = "my account"
    registry = ToolRegistry()
    result = Orchestrator(registry).run(plan, MSG_SPEND)
    assert result.outcome == ExecutionOutcome.NOT_EXECUTED
    assert registry.total_calls == 0
    assert result.validation is not None and not result.validation.ok


def test_clarification_status_is_not_executed():
    plan = Plan.model_validate(
        {
            "understood_request": "Ambiguous.",
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
    result = Orchestrator().run(plan, "move some money")
    assert result.outcome == ExecutionOutcome.NOT_EXECUTED


def test_tool_failure_is_reported_not_raised():
    plan = Plan.model_validate(
        {
            "understood_request": "Divide by zero.",
            "status": "ok",
            "plan": [
                {
                    "step": 1,
                    "tool": "calculator",
                    "args": {
                        "int1": {"v": 10, "src": "user_literal", "ev": "10"},
                        "int2": {"v": 0, "src": "user_literal", "ev": "0"},
                        "operation": {"v": "Divide", "src": "default"},
                    },
                    "depends_on": [],
                }
            ],
            "confidence": 0.9,
        }
    )
    result = Orchestrator().run(plan, "divide 10 by 0", revalidate=False)
    assert result.outcome == ExecutionOutcome.FAILED
    assert "division by zero" in result.detail


def test_missing_output_field_is_a_clean_error():
    plan = spend_plan()
    plan.plan[1].args["user_id"].v = "{{step_1.account_number}}"
    result = Orchestrator().run(plan, MSG_SPEND, revalidate=False)
    assert result.outcome == ExecutionOutcome.FAILED
    assert "account_number" in result.detail


# --------------------------------------------------------------------------- #
# Topological handling
# --------------------------------------------------------------------------- #

def test_independent_branches_are_reported_as_one_group():
    plan = Plan.model_validate(
        {
            "understood_request": "Income versus spending.",
            "status": "ok",
            "plan": [
                {"step": 1, "tool": "get_my_user_id", "args": {}, "depends_on": []},
                {
                    "step": 2,
                    "tool": "get_personal_salary",
                    "args": {"user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"}},
                    "depends_on": [1],
                },
                {
                    "step": 3,
                    "tool": "get_personal_expenditure",
                    "args": {
                        "user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"},
                        "number_of_days": {"v": 30, "src": "default"},
                    },
                    "depends_on": [1],
                },
            ],
            "confidence": 0.9,
        }
    )
    groups = Orchestrator.parallel_groups(plan)
    assert groups == [[1], [2, 3]]


def test_execution_order_respects_dependencies():
    plan = spend_plan()
    order = Orchestrator._topological_order(plan)
    assert order.index(1) < order.index(2) < order.index(3)


@pytest.mark.parametrize("days", [1, 7, 30, 90, 365])
def test_expenditure_window_narrows_monotonically(days):
    registry = ToolRegistry()
    smaller = registry.get_personal_expenditure(USER_ID, days)["count"]
    larger = registry.get_personal_expenditure(USER_ID, 365)["count"]
    assert smaller <= larger