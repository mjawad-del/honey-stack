"""Advisory end-of-turn check. Never blocks a stop: it only names a verification or receipt gap for RSI diagnostics."""
from __future__ import annotations

import re

from honey import tasks

RECEIPT = re.compile(r"(?im)\bchecks run\b|^\W*skipped:|^\W*risk:|^\W*verdict:")
VERDICT = re.compile(r"(?im)^\W*verdict:\W*(not verified|verified|inconclusive)\b")
STATUS_LINE = re.compile(r"(?im)^\W*(?:verdict|checks run|skipped|risk|shipped|asks?)\W*:.*$")
ASKS_DONE = re.compile(r"(?im)^\W*asks?\W*:.*\bdone\b")
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
    if not state.get("edited") or not msg or msg.rstrip("*_ `").endswith("?"):
        return None
    if RECEIPT.search(msg):
        verdict = VERDICT.search(msg)
        body = STATUS_LINE.sub("", msg)  # the block's own words ("Risk: removed …") are not claims
        claimed = ASKS_DONE.search(msg) or (DONE.search(body) and not UNRESOLVED.search(body))
        if verdict and verdict.group(1).lower() != "verified" and claimed:
            state["nagged"] = True
            return "verdict", f"claimed done while the status block says Verdict: {verdict.group(1).upper()}"
        return None
    state["nagged"] = True
    return "receipt", "edit turn ended without the status block (`Verdict: …`, `Checks run: … Skipped: … Risk: …`)"
