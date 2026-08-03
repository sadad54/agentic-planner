"""Plan execution.

The orchestrator is deliberately dumb about intent and strict about contract. It
refuses to run anything that has not validated, resolves references itself,
expands folds into real calculator calls, and halts the entire plan when a write
is present and unconfirmed.

Halting the whole plan rather than just the write step is a deliberate choice: by
the time execution reaches a transfer, the reads have already been paid for, and
a user who declines at that point has had work done on their behalf they never
authorised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .tools import ToolError, ToolRegistry
from .types import Plan, Step, ValidationError, ValidationResult
from .validator import validate_plan

STEP_REF = re.compile(r"^\{\{step_(\d+)\.([A-Za-z_][A-Za-z0-9_]*)\}\}$")


class ExecutionOutcome:
    COMPLETED = "completed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    NOT_EXECUTED = "not_executed"          # status was not ok / plan invalid
    FAILED = "failed"                      # a tool raised


@dataclass
class StepResult:
    step: int
    tool: str
    resolved_args: dict[str, Any]
    output: dict[str, Any] | None = None
    error: str | None = None
    expanded_calls: int = 1


@dataclass
class ExecutionResult:
    outcome: str
    steps: list[StepResult] = field(default_factory=list)
    final: dict[str, Any] | None = None
    validation: ValidationResult | None = None
    detail: str = ""
    tool_calls: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == ExecutionOutcome.COMPLETED


class Orchestrator:
    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self.registry = registry or ToolRegistry()

    # ------------------------------------------------------------------ #

    def run(
        self,
        plan: Plan,
        user_message: str,
        *,
        confirmed: bool = False,
        revalidate: bool = True,
    ) -> ExecutionResult:
        """Execute a plan. Validation runs again here by default.

        Re-validating at the execution boundary means a plan cannot reach a tool
        by any route that skipped the checks, including a caller that forgot.
        """
        if revalidate:
            result = validate_plan(plan, user_message)
            if not result.ok:
                return ExecutionResult(
                    outcome=ExecutionOutcome.NOT_EXECUTED,
                    validation=result,
                    detail="plan failed validation",
                )

        if plan.status in {"needs_clarification", "unsupported"}:
            return ExecutionResult(
                outcome=ExecutionOutcome.NOT_EXECUTED,
                detail=f"status {plan.status!r} carries no executable plan",
            )

        if plan.requires_confirmation and not confirmed:
            return ExecutionResult(
                outcome=ExecutionOutcome.AWAITING_CONFIRMATION,
                detail="plan contains an irreversible action and is not confirmed",
            )

        return self._execute(plan)

    # ------------------------------------------------------------------ #

    def _execute(self, plan: Plan) -> ExecutionResult:
        order = self._topological_order(plan)
        outputs: dict[int, dict[str, Any]] = {}
        results: list[StepResult] = []
        before = self.registry.total_calls

        for number in order:
            step = plan.step_by_number(number)
            assert step is not None
            try:
                record = self._run_step(step, outputs)
            except (ToolError, KeyError, TypeError, ValueError) as exc:
                results.append(
                    StepResult(
                        step=step.step,
                        tool=step.tool,
                        resolved_args={},
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                return ExecutionResult(
                    outcome=ExecutionOutcome.FAILED,
                    steps=results,
                    detail=f"step {step.step} ({step.tool}) failed: {exc}",
                    tool_calls=self.registry.total_calls - before,
                )
            outputs[step.step] = record.output or {}
            results.append(record)

        return ExecutionResult(
            outcome=ExecutionOutcome.COMPLETED,
            steps=results,
            final=results[-1].output if results else None,
            tool_calls=self.registry.total_calls - before,
        )

    def _run_step(self, step: Step, outputs: dict[int, dict[str, Any]]) -> StepResult:
        if step.foreach is not None:
            return self._run_fold(step, outputs)

        kwargs = {
            name: self._resolve(arg.v, outputs) for name, arg in step.args.items()
        }
        output = self.registry.dispatch(step.tool, kwargs)
        return StepResult(
            step=step.step, tool=step.tool, resolved_args=kwargs, output=output
        )

    def _run_fold(self, step: Step, outputs: dict[int, dict[str, Any]]) -> StepResult:
        """Expand a declarative fold into real calculator calls.

        This is the piece that lets a static plan aggregate a list whose length
        is unknown at planning time. The model describes the reduction; ordinary
        code performs it.
        """
        fold = step.foreach
        assert fold is not None
        items = self._resolve(fold.over, outputs)
        if not isinstance(items, list):
            raise ToolError(
                f"foreach.over resolved to {type(items).__name__}, expected list"
            )

        accumulator = fold.initial
        calls = 0
        for item in items:
            if not isinstance(item, dict) or fold.item_field not in item:
                raise ToolError(
                    f"item is missing field {fold.item_field!r} required by the fold"
                )
            outcome = self.registry.calculator(
                int1=accumulator,
                int2=item[fold.item_field],
                operation=fold.reduce,
            )
            accumulator = outcome["result"]
            calls += 1

        return StepResult(
            step=step.step,
            tool=step.tool,
            resolved_args={"foreach": fold.model_dump(), "items": len(items)},
            output={"result": accumulator, "folded_over": len(items)},
            expanded_calls=calls,
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve(value: Any, outputs: dict[int, dict[str, Any]]) -> Any:
        if not isinstance(value, str):
            return value
        match = STEP_REF.match(value)
        if match is None:
            return value
        step_number, field_name = int(match.group(1)), match.group(2)
        if step_number not in outputs:
            raise KeyError(f"step {step_number} has not produced output yet")
        payload = outputs[step_number]
        if field_name not in payload:
            raise KeyError(
                f"step {step_number} output has no field {field_name!r}; "
                f"available: {sorted(payload)}"
            )
        return payload[field_name]

    @staticmethod
    def _topological_order(plan: Plan) -> list[int]:
        indegree = {s.step: 0 for s in plan.plan}
        adjacency: dict[int, list[int]] = {s.step: [] for s in plan.plan}
        for step in plan.plan:
            for dep in step.depends_on:
                if dep in indegree:
                    adjacency[dep].append(step.step)
                    indegree[step.step] += 1

        ready = sorted(n for n, d in indegree.items() if d == 0)
        order: list[int] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for nxt in sorted(adjacency[node]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
                    ready.sort()
        if len(order) != len(plan.plan):
            raise ToolError("plan contains a dependency cycle")
        return order

    @staticmethod
    def parallel_groups(plan: Plan) -> list[list[int]]:
        """Steps that could run concurrently, by dependency depth.

        Used by the eval harness to compare plans up to topological equivalence
        rather than by exact ordering, since two orderings of independent
        branches are the same plan.
        """
        depth: dict[int, int] = {}
        for step in sorted(plan.plan, key=lambda s: s.step):
            deps = [depth.get(d, 0) for d in step.depends_on if d in depth]
            depth[step.step] = max(deps, default=-1) + 1
        groups: dict[int, list[int]] = {}
        for number, level in depth.items():
            groups.setdefault(level, []).append(number)
        return [sorted(groups[k]) for k in sorted(groups)]