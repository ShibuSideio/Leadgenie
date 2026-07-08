"""Simple daily budget guard for expensive discovery actions.

This module keeps the MVP lightweight and deterministic: it tracks daily spend
in a local JSON state file and blocks further expensive actions once the daily
spend reaches the configured limit.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


class BudgetGuard:
    """Simple budget guard for discovery spend."""

    def __init__(self, daily_limit: int = 0, state_path: Optional[str] = None) -> None:
        self.daily_limit = max(0, int(daily_limit))
        self.state_path = Path(state_path or "budget_state.json")
        self._state = self._load_state()

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"day": self._today(), "spent": 0}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"day": self._today(), "spent": 0}
        if data.get("day") != self._today():
            return {"day": self._today(), "spent": 0}
        return {"day": data.get("day", self._today()), "spent": int(data.get("spent", 0))}

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _persist(self) -> None:
        self.state_path.write_text(json.dumps(self._state), encoding="utf-8")

    def can_spend(self, amount: int = 1) -> bool:
        if self.daily_limit <= 0:
            return True
        return self._state.get("spent", 0) + max(0, int(amount)) <= self.daily_limit

    def record_spend(self, amount: int = 1) -> bool:
        if not self.can_spend(amount):
            return False
        self._state["spent"] = self._state.get("spent", 0) + max(0, int(amount))
        self._persist()
        return True

    def remaining(self) -> int:
        if self.daily_limit <= 0:
            return 0
        return max(0, self.daily_limit - self._state.get("spent", 0))
