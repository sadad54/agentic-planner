"""Mock implementations of the five tools.

Deterministic and seeded, so the eval harness produces the same numbers on every
run. Nothing here talks to a network. The point of this module is to prove that
validated plans actually execute, not to model a real bank.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

USER_ID = "usr_8f21c4"


class ToolError(RuntimeError):
    """Raised when a tool is called in a way its signature does not allow."""


@dataclass
class ToolRegistry:
    """Holds the callables and counts how often each is invoked.

    The call count is not bookkeeping for its own sake: repair efficiency is one
    of the metrics the ablation reports, and a system that only improves accuracy
    by retrying many times is not actually better.
    """

    seed: int = 20260803
    calls: dict[str, int] = field(default_factory=dict)
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._salaries = self._make_salaries()
        self._transactions = self._make_transactions()

    # -- seeded fixtures --------------------------------------------------- #

    def _make_salaries(self) -> list[dict[str, Any]]:
        base = 850_000  # 8,500.00 in minor units
        out = []
        for month in range(36):
            drift = self._rng.randint(-15_000, 25_000)
            out.append({"month_offset": 35 - month, "amount": base + drift})
        return sorted(out, key=lambda r: r["month_offset"], reverse=True)

    def _make_transactions(self) -> list[dict[str, Any]]:
        categories = ["groceries", "transport", "rent", "dining", "utilities"]
        out = []
        for day in range(365):
            for _ in range(self._rng.randint(0, 3)):
                out.append(
                    {
                        "days_ago": day,
                        "amount": self._rng.randint(500, 45_000),
                        "category": self._rng.choice(categories),
                    }
                )
        return out

    # -- the tools --------------------------------------------------------- #

    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def get_my_user_id(self) -> dict[str, Any]:
        self._record("get_my_user_id")
        return {"user_id": USER_ID}

    def get_personal_salary(self, user_id: str) -> dict[str, Any]:
        self._record("get_personal_salary")
        if user_id != USER_ID:
            raise ToolError(f"unknown user_id {user_id!r}")
        return {
            "user_id": user_id,
            "salaries": self._salaries,
            "latest_salary": self._salaries[0]["amount"],
            "months": len(self._salaries),
        }

    def get_personal_expenditure(self, user_id: str, number_of_days: int) -> dict[str, Any]:
        self._record("get_personal_expenditure")
        if user_id != USER_ID:
            raise ToolError(f"unknown user_id {user_id!r}")
        if not isinstance(number_of_days, int) or number_of_days <= 0:
            raise ToolError("number_of_days must be a positive integer")
        rows = [t for t in self._transactions if t["days_ago"] < number_of_days]
        return {
            "user_id": user_id,
            "number_of_days": number_of_days,
            "transactions": rows,
            "count": len(rows),
        }

    def calculator(self, int1: int, int2: int, operation: str) -> dict[str, Any]:
        self._record("calculator")
        for name, value in (("int1", int1), ("int2", int2)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"{name} must be an int, got {type(value).__name__}")
        ops: dict[str, Callable[[int, int], int]] = {
            "Add": lambda a, b: a + b,
            "Subtract": lambda a, b: a - b,
            "Multiply": lambda a, b: a * b,
        }
        if operation == "Divide":
            if int2 == 0:
                raise ToolError("division by zero")
            return {"result": int1 // int2, "operation": operation}
        if operation not in ops:
            raise ToolError(f"unsupported operation {operation!r}")
        return {"result": ops[operation](int1, int2), "operation": operation}

    def save_my_money_to_bank(self, amount: int) -> dict[str, Any]:
        self._record("save_my_money_to_bank")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ToolError("amount must be an int in minor units")
        if amount <= 0:
            raise ToolError("amount must be positive")
        return {"transferred": amount, "status": "completed"}

    def dispatch(self, name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_"):
            raise ToolError(f"no such tool: {name}")
        return fn(**kwargs)

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())