"""Advisory end-of-turn check. Never blocks a stop: it only names a verification or receipt gap for RSI diagnostics."""
from __future__ import annotations

import re

from honey import tasks

RECEIPT = re.compile(r"(?im)\bchecks run\b|^\s*skipped:|^\s*risk:")
DONE = re.compile(r'\b(done|fixed|completed?|implemented|removed|uninstalled|disabled|enabled|configured|all (?:tests|checks) pass(?:ed)?)\b', re.I)
UNRESOLVED = re.compile(r"\b(not (?:yet )?(?:done|fixed|complete)|(?:remains?|still) unverified|unable to|could not|couldn't|verification (?:failed|pending))\b", re.I)


def check(payload: dict, state: dict):
    """(kind, reason) for a gap in this turn's completion report, or None. Reports at most once per turn."""
    if state.get("off") or state.get("nagged"):
        return None
    msg = (payload.get("last_assistant_message") or "").strip()
    if not state.get("edited") and (state.get("task") or {}).get("operation") not in ("uninstall", "startup"):
        return None  # Q&A and step-by-step answers are never judged
    missing = tasks.missing(state)
    if msg and DONE.search(msg) and not UNRESOLVED.search(msg) and missing:
        state['nagged'] = True
        return "evidence", "completion claimed without fresh observed evidence for: " + ", ".join(missing)
    if not state.get("edited") or not msg or RECEIPT.search(msg) or msg.rstrip("*_ `").endswith("?"):
        return None
    state["nagged"] = True
    return "receipt", "edit turn ended without a `Checks run: … Skipped: … Risk: …` line"
