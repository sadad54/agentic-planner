"""Plan contract and error taxonomy.

Two things live here. The Pydantic models define what a plan is allowed to look
like structurally. The ErrorCode enum defines what can go wrong, grouped into
families, because the repair router needs to know the *kind* of failure to pick a
corrective action. A single boolean pass/fail would destroy exactly the
information the router runs on.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #

class ErrorFamily(str, Enum):
    """Families map to corrective actions, not to severity."""

    STRUCTURAL = "structural"    # malformed output or wrong wiring -> regenerate
    GROUNDING = "grounding"      # a value has no honest source -> ask the user
    CONSISTENCY = "consistency"  # fields contradict each other -> regenerate
    POLICY = "policy"            # a safety rule was broken -> refuse outright
    REPAIR = "repair"            # the repair itself cheated -> fail closed


class ErrorCode(str, Enum):
    # --- structural -------------------------------------------------------- #
    SCHEMA_MALFORMED = "schema_malformed"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_ARGUMENT = "unknown_argument"
    MISSING_ARGUMENT = "missing_argument"
    ARGUMENT_TYPE_MISMATCH = "argument_type_mismatch"
    CALCULATOR_ARITY = "calculator_arity"
    DANGLING_REFERENCE = "dangling_reference"
    FORWARD_REFERENCE = "forward_reference"
    UNDECLARED_DEPENDENCY = "undeclared_dependency"
    DEPENDENCY_CYCLE = "dependency_cycle"
    NON_SEQUENTIAL_STEPS = "non_sequential_steps"
    DUPLICATE_IDENTITY_CALL = "duplicate_identity_call"
    IDENTITY_NOT_FIRST = "identity_not_first"

    # --- grounding --------------------------------------------------------- #
    ILLEGAL_SOURCE = "illegal_source"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_NOT_IN_MESSAGE = "evidence_not_found_in_user_message"
    EVIDENCE_CONTAINS_NO_NUMERIC_VALUE = "evidence_contains_no_numeric_value"
    EVIDENCE_VALUE_MISMATCH = "evidence_value_mismatch"
    NORMALIZATION_NOT_IN_TABLE = "normalization_not_in_table"
    NORMALIZATION_VALUE_MISMATCH = "normalization_value_mismatch"
    UNDOCUMENTED_DEFAULT = "undocumented_default"

    # --- policy ------------------------------------------------------------ #
    WRITE_ARGUMENT_NOT_GROUNDED = "write_argument_not_grounded"
    DEFAULT_ON_WRITE_TOOL = "default_on_write_tool"
    MISSING_CONFIRMATION_FLAG = "missing_confirmation_flag"

    # --- consistency ------------------------------------------------------- #
    PLAN_NOT_EMPTY_FOR_STATUS = "plan_not_empty_for_status"
    PLAN_EMPTY_FOR_STATUS = "plan_empty_for_status"
    CLARIFICATION_MISSING = "clarification_missing"
    CLARIFICATION_UNEXPECTED = "clarification_unexpected"
    ASSUMPTIONS_MISSING = "assumptions_missing"
    ASSUMPTIONS_UNEXPECTED = "assumptions_unexpected"
    MESSAGE_MISSING = "message_missing"
    ASSUMPTION_ON_WRITE_PLAN = "assumption_on_write_plan"

    # --- repair integrity -------------------------------------------------- #
    LAUNDERED_SOURCE = "laundered_source"
    LAUNDERED_STATUS = "laundered_status"
    UNCITED_MODIFICATION = "uncited_modification"


_FAMILY_BY_CODE: dict[ErrorCode, ErrorFamily] = {}


def _register(family: ErrorFamily, *codes: ErrorCode) -> None:
    for code in codes:
        _FAMILY_BY_CODE[code] = family


_register(
    ErrorFamily.STRUCTURAL,
    ErrorCode.SCHEMA_MALFORMED,
    ErrorCode.UNKNOWN_TOOL,
    ErrorCode.UNKNOWN_ARGUMENT,
    ErrorCode.MISSING_ARGUMENT,
    ErrorCode.ARGUMENT_TYPE_MISMATCH,
    ErrorCode.CALCULATOR_ARITY,
    ErrorCode.DANGLING_REFERENCE,
    ErrorCode.FORWARD_REFERENCE,
    ErrorCode.UNDECLARED_DEPENDENCY,
    ErrorCode.DEPENDENCY_CYCLE,
    ErrorCode.NON_SEQUENTIAL_STEPS,
    ErrorCode.DUPLICATE_IDENTITY_CALL,
    ErrorCode.IDENTITY_NOT_FIRST,
)
_register(
    ErrorFamily.GROUNDING,
    ErrorCode.ILLEGAL_SOURCE,
    ErrorCode.EVIDENCE_MISSING,
    ErrorCode.EVIDENCE_NOT_IN_MESSAGE,
    ErrorCode.EVIDENCE_CONTAINS_NO_NUMERIC_VALUE,
    ErrorCode.EVIDENCE_VALUE_MISMATCH,
    ErrorCode.NORMALIZATION_NOT_IN_TABLE,
    ErrorCode.NORMALIZATION_VALUE_MISMATCH,
    ErrorCode.UNDOCUMENTED_DEFAULT,
)
_register(
    ErrorFamily.POLICY,
    ErrorCode.WRITE_ARGUMENT_NOT_GROUNDED,
    ErrorCode.DEFAULT_ON_WRITE_TOOL,
    ErrorCode.MISSING_CONFIRMATION_FLAG,
)
_register(
    ErrorFamily.CONSISTENCY,
    ErrorCode.PLAN_NOT_EMPTY_FOR_STATUS,
    ErrorCode.PLAN_EMPTY_FOR_STATUS,
    ErrorCode.CLARIFICATION_MISSING,
    ErrorCode.CLARIFICATION_UNEXPECTED,
    ErrorCode.ASSUMPTIONS_MISSING,
    ErrorCode.ASSUMPTIONS_UNEXPECTED,
    ErrorCode.MESSAGE_MISSING,
    ErrorCode.ASSUMPTION_ON_WRITE_PLAN,
)
_register(
    ErrorFamily.REPAIR,
    ErrorCode.LAUNDERED_SOURCE,
    ErrorCode.LAUNDERED_STATUS,
    ErrorCode.UNCITED_MODIFICATION,
)


def family_of(code: ErrorCode) -> ErrorFamily:
    return _FAMILY_BY_CODE[code]


class ValidationError(BaseModel):
    """One failed assertion, precise enough to hand back to the model verbatim."""

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    step: int | None = None
    field: str | None = None
    detail: str = ""

    @property
    def family(self) -> ErrorFamily:
        return family_of(self.code)

    def as_feedback(self) -> dict[str, Any]:
        """The repair prompt gets this, not a prose description."""
        payload: dict[str, Any] = {"error": self.code.value, "detail": self.detail}
        if self.step is not None:
            payload["step"] = self.step
        if self.field is not None:
            payload["field"] = self.field
        return payload

    def __str__(self) -> str:  # pragma: no cover - convenience only
        where = f"step {self.step}" if self.step is not None else "plan"
        field = f".{self.field}" if self.field else ""
        return f"[{self.code.value}] {where}{field}: {self.detail}"


# --------------------------------------------------------------------------- #
# Plan contract
# --------------------------------------------------------------------------- #

Source = Literal["user_literal", "normalized", "step_ref", "default"]
Status = Literal["ok", "ok_with_assumptions", "needs_clarification", "unsupported"]


class Argument(BaseModel):
    """An argument carries its own provenance.

    This is the core of the design. `v` is the value, `src` says where it came
    from, and `ev` is the evidence for that claim. The model is not trusted to be
    honest about `src`; the validator checks the evidence.
    """

    model_config = ConfigDict(extra="forbid")

    v: Any
    src: str
    ev: str | None = None


class Foreach(BaseModel):
    """A declarative fold, expanded by the orchestrator into a calculator chain."""

    model_config = ConfigDict(extra="forbid")

    over: str
    item_field: str
    reduce: Literal["Add", "Subtract", "Multiply", "Divide"]
    initial: int = 0


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int
    tool: str
    args: dict[str, Argument] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)
    foreach: Foreach | None = None
    why: str = ""


class Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: Any
    basis: str


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    options: list[str] = Field(default_factory=list)
    pending_slot: str


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    understood_request: str
    status: Status
    message: str | None = None
    assumptions: list[Assumption] = Field(default_factory=list)
    clarification: Clarification | None = None
    plan: list[Step] = Field(default_factory=list)
    requires_confirmation: bool = False
    needs_replan: bool = False
    confidence: float = 0.0

    def step_by_number(self, n: int) -> Step | None:
        for s in self.plan:
            if s.step == n:
                return s
        return None

    @property
    def touches_write_tool(self) -> bool:
        from .catalog import WRITE_TOOLS

        return any(s.tool in WRITE_TOOLS for s in self.plan)


class ConversationState(BaseModel):
    """Carried across turns so a bare answer to a pending question can be read."""

    model_config = ConfigDict(extra="forbid")

    pending_slot: str | None = None
    pending_question: str | None = None
    resolved_slots: dict[str, Any] = Field(default_factory=dict)
    turn_index: int = 1


class ValidationResult(BaseModel):
    ok: bool
    errors: list[ValidationError] = Field(default_factory=list)

    @property
    def families(self) -> set[ErrorFamily]:
        return {e.family for e in self.errors}

    def codes(self) -> list[str]:
        return [e.code.value for e in self.errors]

    def feedback(self) -> list[dict[str, Any]]:
        return [e.as_feedback() for e in self.errors]