import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any


@dataclass
class StateStore:
    filename: str

    def _default_state(self) -> Dict[str, Any]:
        return {
            "tweets": {},
            "source_tweets": {},
            "alerts_sent": {},
            "trend_accounts": {},
            "last_run_at": None,
        }

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.filename):
            return self._default_state()
        with open(self.filename, "r", encoding="utf-8") as f:
            state = json.load(f)
        default_state = self._default_state()
        for key, value in default_state.items():
            state.setdefault(key, value)
        return state

    def save(self, state: Dict[str, Any]) -> None:
        state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.filename, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
