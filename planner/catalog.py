"""The tool catalog, expressed as data.

The prompt describes these tools in prose so the model can read them; this module
is the machine-readable copy the validator checks against. Keeping one source of
truth here means a plan can never be accepted against a signature the prompt does
not actually describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Param:
    name: str
    type: type
    # Documented default, usable with src="default". None means no default exists.
    default: Any = None
    has_default: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    params: tuple[Param, ...]
    # A write tool has effects outside the system. Grounding rules are stricter.
    is_write: bool = False
    returns: str = ""

    @property
    def param_names(self) -> set[str]:
        return {p.name for p in self.params}

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None


CATALOG: dict[str, Tool] = {
    "get_my_user_id": Tool(
        name="get_my_user_id",
        params=(),
        returns="Dict with user_id",
    ),
    "get_personal_salary": Tool(
        name="get_personal_salary",
        params=(Param("user_id", str),),
        returns="Dict with 36 months of salary records",
    ),
    "calculator": Tool(
        name="calculator",
        params=(
            Param("int1", int),
            Param("int2", int),
            # operation has no documented default: it is always derived from the
            # request. See NOTE below.
            Param("operation", str),
        ),
        returns="Dict with result",
    ),
    "get_personal_expenditure": Tool(
        name="get_personal_expenditure",
        params=(
            Param("user_id", str),
            Param("number_of_days", int, default=30, has_default=True),
        ),
        returns="Dict with transactions",
    ),
    "save_my_money_to_bank": Tool(
        name="save_my_money_to_bank",
        params=(Param("amount", int),),
        is_write=True,
        returns="Dict with transfer confirmation",
    ),
}

# NOTE on calculator.operation:
# The prompt's worked examples label this src="default", which is loose - the
# operation is derived from intent, not defaulted. Rather than add a fifth src
# category for one field, operation is exempted from the default-must-be-
# documented check via OPERATION_EXEMPT below. This is a known wart, kept
# deliberately so the trade-off is visible rather than hidden.
OPERATION_EXEMPT = {("calculator", "operation")}

IDENTITY_TOOL = "get_my_user_id"

WRITE_TOOLS = {name for name, tool in CATALOG.items() if tool.is_write}

# Sources permitted for arguments to write tools. A transfer amount is either a
# number the user gave us or a number the tools computed. Never an inference.
WRITE_SAFE_SOURCES = {"user_literal", "step_ref"}

LEGAL_SOURCES = {"user_literal", "normalized", "step_ref", "default"}

# The only legal (evidence phrase, value) pairs for src="normalized".
# Anything outside this table must be asked about, not guessed.
TIME_NORMALIZATION: dict[str, int] = {
    "today": 1,
    "past day": 1,
    "this week": 7,
    "past week": 7,
    "last week": 7,
    "past two weeks": 14,
    "fortnight": 14,
    "this month": 30,
    "past month": 30,
    "last month": 30,
    "past quarter": 90,
    "last quarter": 90,
    "last 3 months": 90,
    "past 3 months": 90,
    "past 6 months": 180,
    "last 6 months": 180,
    "half year": 180,
    "this year": 365,
    "past year": 365,
    "last year": 365,
}


def normalize_phrase(phrase: str) -> str:
    """Lowercase and collapse whitespace so lookups are forgiving about spacing."""
    return " ".join(phrase.lower().split())


def lookup_normalization(phrase: str) -> int | None:
    return TIME_NORMALIZATION.get(normalize_phrase(phrase))