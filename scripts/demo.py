"""End-to-end demo. Run with: python -m scripts.demo

Four scenarios, chosen because each one fails a different way. No API key
required: these are hand-written plans of the kind the planner prompt produces,
run through the real validator, router and orchestrator.
"""

from __future__ import annotations

import json

from planner.orchestrator import Orchestrator
from planner.router import route, verify_repair
from planner.tools import ToolRegistry
from planner.types import Plan
from planner.validator import validate, validate_plan

RULE = "-" * 78


def show(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def spend_plan() -> dict:
    return {
        "understood_request": "Total expenditure over the trailing 30 days.",
        "status": "ok",
        "plan": [
            {"step": 1, "tool": "get_my_user_id", "args": {}, "depends_on": [],
             "why": "Only source of user_id."},
            {"step": 2, "tool": "get_personal_expenditure",
             "args": {"user_id": {"v": "{{step_1.user_id}}", "src": "step_ref"},
                      "number_of_days": {"v": 30, "src": "normalized",
                                         "ev": "last month"}},
             "depends_on": [1], "why": "Trailing 30-day window."},
            {"step": 3, "tool": "calculator",
             "args": {"operation": {"v": "Add", "src": "default"}},
             "depends_on": [2],
             "foreach": {"over": "{{step_2.transactions}}", "item_field": "amount",
                         "reduce": "Add", "initial": 0},
             "why": "calculator is binary; the count is unknown at plan time."},
        ],
        "confidence": 0.96,
    }


def main() -> None:
    message = "How much did I spend in the last month?"

    # ------------------------------------------------------------------ #
    show("1. A valid plan validates, then executes")
    registry = ToolRegistry()
    result = validate(spend_plan(), message)
    print(f"validation ok: {result.ok}")
    execution = Orchestrator(registry).run(Plan.model_validate(spend_plan()), message)
    print(f"outcome:       {execution.outcome}")
    print(f"tool calls:    {execution.tool_calls} "
          f"(the fold expanded into {execution.steps[-1].expanded_calls} calculator calls)")
    assert execution.final is not None, "Expected a final result from execution"
    print(f"total spend:   {execution.final['result']:,} minor units")

    # ------------------------------------------------------------------ #
    show("2. A structurally perfect plan built on an invented number")
    invented = {
        "understood_request": "Move the user's spare money to the bank.",
        "status": "ok",
        "plan": [{"step": 1, "tool": "save_my_money_to_bank",
                  "args": {"amount": {"v": 250000, "src": "user_literal",
                                      "ev": "transfer 2500 to savings"}},
                  "depends_on": [], "why": "User asked."}],
        "requires_confirmation": True,
        "confidence": 0.94,
    }
    said = "i've got too much money lying around"
    print(f'user actually said: "{said}"')
    outcome = validate(invented, said)
    print(f"validation ok: {outcome.ok}")
    for error in outcome.errors:
        print(f"  {error}")
    decision = route(outcome)
    print(f"routed to:     {decision.action.value}")
    print(f"rationale:     {decision.rationale}")

    # ------------------------------------------------------------------ #
    show("3. A write plan performs no work until it is confirmed")
    transfer = Plan.model_validate({
        "understood_request": "Transfer 500.",
        "status": "ok",
        "plan": [{"step": 1, "tool": "save_my_money_to_bank",
                  "args": {"amount": {"v": 50000, "src": "user_literal",
                                      "ev": "move 500"}},
                  "depends_on": [], "why": "Amount supplied by the user."}],
        "requires_confirmation": True,
        "confidence": 0.92,
    })
    gated_registry = ToolRegistry()
    gated = Orchestrator(gated_registry).run(transfer, "please move 500 to my bank")
    print(f"outcome:       {gated.outcome}")
    print(f"tool calls:    {gated_registry.total_calls}")
    confirmed_registry = ToolRegistry()
    confirmed = Orchestrator(confirmed_registry).run(
        transfer, "please move 500 to my bank", confirmed=True
    )
    if confirmed.final is not None:
        transferred_str = f"{confirmed.final['transferred']:,}"
    else:
        transferred_str = "N/A"
    print(f"after confirm: {confirmed.outcome}, transferred {transferred_str}")

    # ------------------------------------------------------------------ #
    show("4. A repair that would launder a grounding failure is refused")
    original = Plan.model_validate({
        "understood_request": "Transfer leftover money.",
        "status": "ok",
        "plan": [{"step": 1, "tool": "save_my_money_to_bank",
                  "args": {"amount": {"v": 50000, "src": "user_literal",
                                      "ev": "my leftover money"}},
                  "depends_on": [], "why": "User asked."}],
        "requires_confirmation": True,
        "confidence": 0.7,
    })
    vague = "put my leftover money in the bank"
    cited = validate_plan(original, vague).errors
    print("cited fault:")
    for error in cited:
        print(f"  {error}")

    laundered = Plan.model_validate({
        "understood_request": "Transfer leftover money.",
        "status": "ok",
        "plan": [{"step": 1, "tool": "save_my_money_to_bank",
                  "args": {"amount": {"v": 30, "src": "normalized",
                                      "ev": "last month"}},
                  "depends_on": [], "why": "Repaired."}],
        "requires_confirmation": True,
        "confidence": 0.7,
    })
    problems = verify_repair(original, laundered, cited)
    print("\nrepair diff:")
    for problem in problems:
        print(f"  {problem}")
    print("\nfeedback handed back to the model:")
    print(json.dumps([p.as_feedback() for p in problems], indent=2))


if __name__ == "__main__":
    main()