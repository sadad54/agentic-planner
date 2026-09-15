"""Plan validation.

Six families of check, run in order, each producing typed errors. Nothing here
calls a language model: every check is deterministic, so the same plan always
produces the same verdict and the test suite runs in milliseconds.

The check that matters most is grounding. A malformed plan announces itself. A
well-formed plan containing one invented number executes perfectly and returns a
wrong answer with no trace of where the number came from. Requiring every
argument to declare and evidence its origin turns that into a string comparison.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from .catalog import (
    CATALOG,
    IDENTITY_TOOL,
    LEGAL_SOURCES,
    OPERATION_EXEMPT,
    WRITE_SAFE_SOURCES,
    lookup_normalization,
    normalize_phrase,
)
from .types import (
    ErrorCode,
    Plan,
    Step,
    ValidationError,
    ValidationResult,
)

STEP_REF = re.compile(r"^\{\{step_(\d+)\.([A-Za-z_][A-Za-z0-9_]*)\}\}$")
NUMBER = re.compile(
    r"(?<![\w.,+-])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w.,])"
)


def evidence_values(evidence: str, *, monetary: bool) -> set[int]:
    """Normalize evidence at the validation boundary, without float rounding.

    Transfer literals use major units unless explicitly followed by cents/sen.
    Other integer parameters (days, calculator operands) are unscaled scalars.
    Fractional minor units and malformed comma grouping never produce a value.
    """
    values: set[int] = set()
    for match in NUMBER.finditer(evidence):
        value = Decimal(match.group().replace(",", ""))
        minor = re.match(r"\s*(?:cents?|sen)\b", evidence[match.end():])
        if monetary and not minor:
            value *= 100
        if value == value.to_integral_value():
            values.add(int(value))
    return values


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def parse_plan(raw: Any) -> tuple[Plan | None, list[ValidationError]]:
    """Parse untrusted model output into a Plan, or report why it will not parse."""
    try:
        return Plan.model_validate(raw), []
    except PydanticValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(
                ValidationError(
                    code=ErrorCode.SCHEMA_MALFORMED,
                    field=loc or None,
                    detail=err["msg"],
                )
            )
        return None, errors


def validate(raw: Any, user_message: str) -> ValidationResult:
    """Validate raw model output against the contract.

    `user_message` is required because grounding cannot be checked without it: a
    user_literal claim is verified by looking for its evidence in what the user
    actually said.
    """
    plan, parse_errors = parse_plan(raw)
    if plan is None:
        return ValidationResult(ok=False, errors=parse_errors)
    return validate_plan(plan, user_message)


def validate_plan(plan: Plan, user_message: str) -> ValidationResult:
    errors: list[ValidationError] = []
    errors += check_consistency(plan)
    errors += check_catalog(plan)
    errors += check_graph(plan)
    errors += check_grounding(plan, user_message)
    errors += check_write_policy(plan)
    return ValidationResult(ok=not errors, errors=errors)


# --------------------------------------------------------------------------- #
# 1. Consistency: do the top-level fields contradict each other?
# --------------------------------------------------------------------------- #

def check_consistency(plan: Plan) -> list[ValidationError]:
    errors: list[ValidationError] = []
    empty_statuses = {"needs_clarification", "unsupported"}

    if plan.status in empty_statuses and plan.plan:
        errors.append(
            ValidationError(
                code=ErrorCode.PLAN_NOT_EMPTY_FOR_STATUS,
                field="plan",
                detail=f"status {plan.status!r} requires an empty plan, got "
                       f"{len(plan.plan)} steps",
            )
        )
    if plan.status not in empty_statuses and not plan.plan and not plan.needs_replan:
        errors.append(
            ValidationError(
                code=ErrorCode.PLAN_EMPTY_FOR_STATUS,
                field="plan",
                detail=f"status {plan.status!r} requires a non-empty plan",
            )
        )
    if plan.status == "needs_clarification" and plan.clarification is None:
        errors.append(
            ValidationError(
                code=ErrorCode.CLARIFICATION_MISSING,
                field="clarification",
                detail="needs_clarification requires a clarification object",
            )
        )
    if plan.status != "needs_clarification" and plan.clarification is not None:
        errors.append(
            ValidationError(
                code=ErrorCode.CLARIFICATION_UNEXPECTED,
                field="clarification",
                detail=f"clarification is only valid for needs_clarification, "
                       f"status is {plan.status!r}",
            )
        )
    if plan.status == "ok_with_assumptions" and not plan.assumptions:
        errors.append(
            ValidationError(
                code=ErrorCode.ASSUMPTIONS_MISSING,
                field="assumptions",
                detail="ok_with_assumptions requires at least one assumption",
            )
        )
    if plan.status != "ok_with_assumptions" and plan.assumptions:
        errors.append(
            ValidationError(
                code=ErrorCode.ASSUMPTIONS_UNEXPECTED,
                field="assumptions",
                detail=f"assumptions are only valid for ok_with_assumptions, "
                       f"status is {plan.status!r}",
            )
        )
    if plan.status == "unsupported" and not plan.message:
        errors.append(
            ValidationError(
                code=ErrorCode.MESSAGE_MISSING,
                field="message",
                detail="unsupported requires a message naming the missing capability",
            )
        )

    # A plan may never assume its way into a write. Assumptions are the
    # read-only branch of the inference policy.
    if plan.status == "ok_with_assumptions" and plan.touches_write_tool:
        errors.append(
            ValidationError(
                code=ErrorCode.ASSUMPTION_ON_WRITE_PLAN,
                field="status",
                detail="ok_with_assumptions is not permitted for a plan containing "
                       "a write tool; ask instead",
            )
        )
    if plan.touches_write_tool and not plan.requires_confirmation:
        errors.append(
            ValidationError(
                code=ErrorCode.MISSING_CONFIRMATION_FLAG,
                field="requires_confirmation",
                detail="a plan containing a write tool must set requires_confirmation",
            )
        )
    return errors


# --------------------------------------------------------------------------- #
# 2. Catalog: does every tool and argument exist with the right type?
# --------------------------------------------------------------------------- #

def check_catalog(plan: Plan) -> list[ValidationError]:
    errors: list[ValidationError] = []

    for step in plan.plan:
        tool = CATALOG.get(step.tool)
        if tool is None:
            errors.append(
                ValidationError(
                    code=ErrorCode.UNKNOWN_TOOL,
                    step=step.step,
                    field="tool",
                    detail=f"{step.tool!r} is not in the catalog",
                )
            )
            continue

        for name in step.args:
            if name not in tool.param_names:
                errors.append(
                    ValidationError(
                        code=ErrorCode.UNKNOWN_ARGUMENT,
                        step=step.step,
                        field=f"args.{name}",
                        detail=f"{step.tool} has no parameter {name!r}; "
                               f"expected one of {sorted(tool.param_names)}",
                    )
                )

        errors += _check_required_args(step, tool)
        errors += _check_arg_types(step, tool)

    errors += _check_identity_tool(plan)
    return errors


def _check_required_args(step: Step, tool: Any) -> list[ValidationError]:
    errors: list[ValidationError] = []
    # A calculator step carrying a fold supplies its operands at runtime, so
    # int1/int2 are legitimately absent.
    folding = step.foreach is not None
    for param in tool.params:
        if param.name in step.args:
            continue
        if folding and param.name in {"int1", "int2"}:
            continue
        if param.has_default:
            continue
        errors.append(
            ValidationError(
                code=ErrorCode.MISSING_ARGUMENT,
                step=step.step,
                field=f"args.{param.name}",
                detail=f"{tool.name} requires {param.name!r}",
            )
        )
    return errors


def _check_arg_types(step: Step, tool: Any) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for name, arg in step.args.items():
        param = tool.param(name)
        if param is None:
            continue  # already reported as UNKNOWN_ARGUMENT
        if isinstance(arg.v, str) and STEP_REF.match(arg.v):
            continue  # resolved at runtime; type checked by the orchestrator
        if param.type is int and isinstance(arg.v, bool):
            errors.append(
                ValidationError(
                    code=ErrorCode.ARGUMENT_TYPE_MISMATCH,
                    step=step.step,
                    field=f"args.{name}",
                    detail=f"{name!r} must be int, got bool",
                )
            )
        elif not isinstance(arg.v, param.type):
            errors.append(
                ValidationError(
                    code=ErrorCode.ARGUMENT_TYPE_MISMATCH,
                    step=step.step,
                    field=f"args.{name}",
                    detail=f"{name!r} must be {param.type.__name__}, got "
                           f"{type(arg.v).__name__}",
                )
            )
        elif param.type is int and isinstance(arg.v, float):
            errors.append(
                ValidationError(
                    code=ErrorCode.ARGUMENT_TYPE_MISMATCH,
                    step=step.step,
                    field=f"args.{name}",
                    detail="monetary values are integers in minor units; no decimals",
                )
            )
    return errors


def _check_identity_tool(plan: Plan) -> list[ValidationError]:
    errors: list[ValidationError] = []
    identity_steps = [s for s in plan.plan if s.tool == IDENTITY_TOOL]
    if len(identity_steps) > 1:
        errors.append(
            ValidationError(
                code=ErrorCode.DUPLICATE_IDENTITY_CALL,
                step=identity_steps[1].step,
                field="tool",
                detail=f"{IDENTITY_TOOL} may be called at most once per plan",
            )
        )
    needs_identity = any(
        "user_id" in CATALOG[s.tool].param_names
        for s in plan.plan
        if s.tool in CATALOG
    )
    if needs_identity and identity_steps and identity_steps[0].step != min(
        s.step for s in plan.plan
    ):
        errors.append(
            ValidationError(
                code=ErrorCode.IDENTITY_NOT_FIRST,
                step=identity_steps[0].step,
                field="step",
                detail=f"{IDENTITY_TOOL} must be the first step when user_id is needed",
            )
        )
    return errors


# --------------------------------------------------------------------------- #
# 3. Graph: do references resolve, and does the plan topologically sort?
# --------------------------------------------------------------------------- #

def check_graph(plan: Plan) -> list[ValidationError]:
    errors: list[ValidationError] = []
    numbers = [s.step for s in plan.plan]

    if numbers and numbers != list(range(1, len(numbers) + 1)):
        errors.append(
            ValidationError(
                code=ErrorCode.NON_SEQUENTIAL_STEPS,
                field="plan",
                detail=f"step numbers must run 1..N in order, got {numbers}",
            )
        )

    known = set(numbers)
    for step in plan.plan:
        referenced: set[int] = set()

        for name, arg in step.args.items():
            if not isinstance(arg.v, str):
                continue
            match = STEP_REF.match(arg.v)
            if not match:
                continue
            target = int(match.group(1))
            referenced.add(target)
            errors += _check_reference(step, f"args.{name}", target, known)

        if step.foreach is not None:
            match = STEP_REF.match(step.foreach.over)
            if match is None:
                errors.append(
                    ValidationError(
                        code=ErrorCode.DANGLING_REFERENCE,
                        step=step.step,
                        field="foreach.over",
                        detail=f"{step.foreach.over!r} is not a step reference",
                    )
                )
            else:
                target = int(match.group(1))
                referenced.add(target)
                errors += _check_reference(step, "foreach.over", target, known)

        for target in sorted(referenced - set(step.depends_on)):
            errors.append(
                ValidationError(
                    code=ErrorCode.UNDECLARED_DEPENDENCY,
                    step=step.step,
                    field="depends_on",
                    detail=f"step {target} is referenced but not declared in depends_on",
                )
            )

        for dep in step.depends_on:
            if dep not in known:
                errors.append(
                    ValidationError(
                        code=ErrorCode.DANGLING_REFERENCE,
                        step=step.step,
                        field="depends_on",
                        detail=f"depends_on names step {dep}, which does not exist",
                    )
                )

    errors += _check_acyclic(plan)
    errors += _check_calculator_arity(plan)
    return errors


def _check_reference(
    step: Step, field: str, target: int, known: set[int]
) -> list[ValidationError]:
    if target not in known:
        return [
            ValidationError(
                code=ErrorCode.DANGLING_REFERENCE,
                step=step.step,
                field=field,
                detail=f"references step {target}, which does not exist",
            )
        ]
    if target >= step.step:
        return [
            ValidationError(
                code=ErrorCode.FORWARD_REFERENCE,
                step=step.step,
                field=field,
                detail=f"references step {target}; references must point backwards",
            )
        ]
    return []


def _check_acyclic(plan: Plan) -> list[ValidationError]:
    """Kahn's algorithm. Sequential numbering makes cycles rare, but depends_on is
    model-generated and a cycle would hang the orchestrator rather than fail it."""
    indegree = {s.step: 0 for s in plan.plan}
    adjacency: dict[int, list[int]] = {s.step: [] for s in plan.plan}
    for step in plan.plan:
        for dep in step.depends_on:
            if dep in indegree:
                adjacency[dep].append(step.step)
                indegree[step.step] += 1

    queue = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for nxt in adjacency[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)

    if seen != len(plan.plan):
        stuck = sorted(n for n, d in indegree.items() if d > 0)
        return [
            ValidationError(
                code=ErrorCode.DEPENDENCY_CYCLE,
                field="depends_on",
                detail=f"dependency cycle involving steps {stuck}",
            )
        ]
    return []


def _check_calculator_arity(plan: Plan) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for step in plan.plan:
        if step.tool != "calculator":
            continue
        operands = {k for k in step.args if k in {"int1", "int2"}}
        if step.foreach is not None and operands:
            errors.append(
                ValidationError(
                    code=ErrorCode.CALCULATOR_ARITY,
                    step=step.step,
                    field="args",
                    detail="a folding calculator step must not also supply int1/int2",
                )
            )
        if step.foreach is None and len(operands) != 2:
            errors.append(
                ValidationError(
                    code=ErrorCode.CALCULATOR_ARITY,
                    step=step.step,
                    field="args",
                    detail="calculator takes exactly two operands; use foreach to "
                           "reduce a list of unknown length",
                )
            )
    return errors


# --------------------------------------------------------------------------- #
# 4. Grounding: can every value be traced to a legal source?
# --------------------------------------------------------------------------- #

def check_grounding(plan: Plan, user_message: str) -> list[ValidationError]:
    errors: list[ValidationError] = []
    haystack = normalize_phrase(user_message)

    for step in plan.plan:
        tool = CATALOG.get(step.tool)
        for name, arg in step.args.items():
            field = f"args.{name}"

            if arg.src not in LEGAL_SOURCES:
                errors.append(
                    ValidationError(
                        code=ErrorCode.ILLEGAL_SOURCE,
                        step=step.step,
                        field=field,
                        detail=f"{arg.src!r} is not a legal source; expected one of "
                               f"{sorted(LEGAL_SOURCES)}",
                    )
                )
                continue

            if arg.src == "user_literal":
                errors += _check_user_literal(step, name, arg, haystack)
            elif arg.src == "normalized":
                errors += _check_normalized(step, name, arg)
            elif arg.src == "step_ref":
                errors += _check_step_ref(step, name, arg)
            elif arg.src == "default":
                errors += _check_default(step, name, arg, tool)

    return errors


def _check_user_literal(step: Step, name: str, arg: Any, haystack: str) -> list[ValidationError]:
    """The load-bearing check.

    A user_literal claims the user said this. We look for the quoted evidence in
    what they actually said. If it is not there, the value was invented, and no
    amount of confident phrasing changes that.
    """
    if not arg.ev:
        return [
            ValidationError(
                code=ErrorCode.EVIDENCE_MISSING,
                step=step.step,
                field=f"args.{name}",
                detail="src user_literal requires ev quoting the user's message",
            )
        ]

    needle = normalize_phrase(arg.ev)
    if needle not in haystack:
        return [
            ValidationError(
                code=ErrorCode.EVIDENCE_NOT_IN_MESSAGE,
                step=step.step,
                field=f"args.{name}",
                detail=f"evidence {arg.ev!r} does not appear in the user's message",
            )
        ]

    if isinstance(arg.v, int) and not isinstance(arg.v, bool):
        found = NUMBER.findall(needle)
        if not found:
            return [
                ValidationError(
                    code=ErrorCode.EVIDENCE_CONTAINS_NO_NUMERIC_VALUE,
                    step=step.step,
                    field=f"args.{name}",
                    detail=f"evidence {arg.ev!r} contains no number, so it cannot "
                           f"ground the value {arg.v!r}",
                )
            ]
        candidates = evidence_values(
            needle, monetary=(step.tool == "save_my_money_to_bank" and name == "amount")
        )
        if arg.v not in candidates:
            return [
                ValidationError(
                    code=ErrorCode.EVIDENCE_VALUE_MISMATCH,
                    step=step.step,
                    field=f"args.{name}",
                    detail=f"value {arg.v!r} does not match any number in evidence "
                           f"{arg.ev!r} (accepted: {sorted(candidates)})",
                )
            ]
    return []


def _check_normalized(step: Step, name: str, arg: Any) -> list[ValidationError]:
    if not arg.ev:
        return [
            ValidationError(
                code=ErrorCode.EVIDENCE_MISSING,
                step=step.step,
                field=f"args.{name}",
                detail="src normalized requires ev naming the phrase that was mapped",
            )
        ]
    expected = lookup_normalization(arg.ev)
    if expected is None:
        return [
            ValidationError(
                code=ErrorCode.NORMALIZATION_NOT_IN_TABLE,
                step=step.step,
                field=f"args.{name}",
                detail=f"{arg.ev!r} is not in the normalization table",
            )
        ]
    if arg.v != expected:
        return [
            ValidationError(
                code=ErrorCode.NORMALIZATION_VALUE_MISMATCH,
                step=step.step,
                field=f"args.{name}",
                detail=f"the table maps {arg.ev!r} to {expected}, not {arg.v!r}",
            )
        ]
    return []


def _check_step_ref(step: Step, name: str, arg: Any) -> list[ValidationError]:
    if not isinstance(arg.v, str) or not STEP_REF.match(arg.v):
        return [
            ValidationError(
                code=ErrorCode.DANGLING_REFERENCE,
                step=step.step,
                field=f"args.{name}",
                detail=f"src step_ref requires a value of the form {{{{step_N.field}}}}, "
                       f"got {arg.v!r}",
            )
        ]
    return []  # resolution itself is checked in check_graph


def _check_default(step: Step, name: str, arg: Any, tool: Any) -> list[ValidationError]:
    if (step.tool, name) in OPERATION_EXEMPT:
        return []
    param = tool.param(name) if tool else None
    if param is None:
        return []  # already reported as UNKNOWN_ARGUMENT
    if not param.has_default:
        return [
            ValidationError(
                code=ErrorCode.UNDOCUMENTED_DEFAULT,
                step=step.step,
                field=f"args.{name}",
                detail=f"{step.tool}.{name} has no documented default",
            )
        ]
    if arg.v != param.default:
        return [
            ValidationError(
                code=ErrorCode.UNDOCUMENTED_DEFAULT,
                step=step.step,
                field=f"args.{name}",
                detail=f"documented default for {name!r} is {param.default!r}, "
                       f"got {arg.v!r}",
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# 5. Write policy: the asymmetry, enforced
# --------------------------------------------------------------------------- #

def check_write_policy(plan: Plan) -> list[ValidationError]:
    """A transfer amount is either a number the user said or a number the tools
    computed. Never a default, never an inference. One rule, mechanically checked."""
    errors: list[ValidationError] = []
    for step in plan.plan:
        tool = CATALOG.get(step.tool)
        if tool is None or not tool.is_write:
            continue
        for name, arg in step.args.items():
            if arg.src in WRITE_SAFE_SOURCES:
                continue
            code = (
                ErrorCode.DEFAULT_ON_WRITE_TOOL
                if arg.src in {"default", "normalized"}
                else ErrorCode.WRITE_ARGUMENT_NOT_GROUNDED
            )
            errors.append(
                ValidationError(
                    code=code,
                    step=step.step,
                    field=f"args.{name}",
                    detail=f"{step.tool} is a write tool; {name!r} may only use "
                           f"{sorted(WRITE_SAFE_SOURCES)}, got {arg.src!r}",
                )
            )
    return errors
