"""Intent loop: deterministic, advisory hints that keep the agent on what the user actually asked.

- question-only prompts ("i'm just asking …") get an answer-don't-edit hint
- a prompt with two or more list items gets an `Asks:` reconcile hint; an edit turn that skips it gets one nudge next turn
- "are all the changes i asked done?" replays this session's ask log, which also survives compaction
- a stated term ("i told you ship means merged too") becomes a project learning through the `remember:` ledger

Nothing here blocks. Each hint is added only when its pattern matches, so a plain prompt costs no context.
"""
from __future__ import annotations

import re

from honey.rsi import curate, observe, text

ASK_LOG_MAX = 10
ASK_CHARS = 160

ITEM = re.compile(r"^[ \t]*(?:\d{1,2}[.)]|[-*•])[ \t]+(\S.{2,199})$", re.M)
# Pasted rendered output (bold, markdown links) and host notices carry lists that are not the user's asks.
PASTED = re.compile(r"\*\*|\]\(https?://|<pasted_content|^\s*(?:Update on pull request|You are in a side chat)\b", re.I)
ASKS_LINE = re.compile(r"(?im)^\W*asks?\s*:")
QUESTION_ONLY = re.compile(
    r"\b(?:just|only) (?:asking|curious|wondering|checking)\b"
    r"|\bdon'?t (?:change|edit|touch|implement|modify) anything\b|\bno (?:code )?changes(?: please)?\b", re.I)
STATUS = re.compile(
    r"\b(?:all|everything|every ?thing)\b[^.?!\n]{0,40}?\bi (?:asked|requested|wanted|said|mentioned)\b"
    r"|\bhave (?:these|those|the|all|my) (?:changes|things|fixes|items)\b[^.?!\n]{0,20}?\b(?:applied|done|implemented|made|in)\b",
    re.I)

_X = r"""(?P<t>["'`]?[\w./-]+(?:[ \t]+[\w./-]+){0,3}?["'`]?)"""
_END = r"(?P<m>[^.?!;\n]{2,120})(?P<end>[.?!;\n]|$)"
TERM_TOLD = re.compile(r"\b(?:i (?:\w+ ){0,2}told you|remember that|fyi|note that|for the record|just so you know)\b"
                       r"(?:[^.?!\n]{0,20}?\bthat\b)?[\s:,]+" + _X + r"\s+(?P<v>means|is|refers to|=)\s+" + _END, re.I)
TERM_MEAN = re.compile(r"\b(?:by|when i say)\s+" + _X + r",?\s+i mean\s+" + _END, re.I)
_NOT_TERM = {"to", "it", "this", "that", "they", "he", "she", "we", "you", "i", "there", "which", "what", "why", "how",
             "not", "if", "when", "me", "us", "so"}
_STATE = {"not", "broken", "fine", "done", "wrong", "working", "failing", "ok", "okay", "good", "bad", "ready", "still",
          "already", "down", "up", "live", "deployed", "missing", "empty", "slow", "fast", "correct", "right", "true",
          "false", "a", "an", "the", "now", "being", "too", "very", "also"}
# A saved term is loaded into every later session, so its meaning must read like a name, never an instruction.
_ORDER = re.compile(r"\b(?:ignore|disregard|override|always|never|must|should|instructions?|rules?|system|prompt|run|execute"
                    r"|curl|wget|sudo|rm|delete|push|deploy|install|token|password|secret|key|http|www)\b|[`$<>{}|;]", re.I)
TERM_MAX_PROMPT = 400
TERM_MAX_WORDS = 8
_TRAIL = {"which", "that", "so", "and", "but", "right", "ok", "okay", "bro", "please", "now", "btw"}


def items(prompt: str) -> list:
    """Explicit list items (numbered or bulleted lines) the user typed, only when there are two to nine."""
    p = prompt or ""
    if len(p) > 1500 or PASTED.search(p):
        return []
    found = [m.strip() for m in ITEM.findall(p)]
    return found if 2 <= len(found) <= 9 and all(len(f) <= 160 for f in found) else []


def question_only(prompt: str) -> bool:
    return bool(QUESTION_ONLY.search((prompt or "")[:2000]))


def status_check(prompt: str) -> bool:
    return bool(STATUS.search((prompt or "")[:600]))


def term(prompt: str):
    """(term, meaning) when the user states what a word means, else None. Questions never count.

    Only short, typed prompts qualify: pasted documents, rendered output and code can't plant a saved term.
    """
    p = prompt or ""
    if len(p) > TERM_MAX_PROMPT or PASTED.search(p) or "```" in p:
        return None
    for rx in (TERM_MEAN, TERM_TOLD):
        m = rx.search(p)
        if not m or m.group("end") == "?":
            continue
        t = m.group("t").strip("\"'` ").strip()
        if t.split()[0].lower() in _NOT_TERM:
            continue
        if t.lower().startswith("the "):
            t = t[4:]
        words = m.group("m").strip().split()
        while words and words[-1].lower().strip(",") in _TRAIL:
            words.pop()
        meaning = " ".join(words).strip(" ,")
        if not meaning or len(words) > TERM_MAX_WORDS or _ORDER.search(meaning) or text.injection_hit(meaning):
            continue
        if m.groupdict().get("v", "").lower() == "is" and (words[0].lower() in _STATE or len(words) > 6):
            continue  # "X is broken" describes a state, not a name
        return t, meaning
    return None


def ledger(state: dict) -> str:
    """The session's ask log, oldest first, or ''."""
    asks = state.get("asks") or []
    return "\n".join(f"{n}. {a}" for n, a in enumerate(asks, 1))


def _log(state: dict, prompt: str) -> None:
    clean = " ".join(text.redact(prompt, 400).split())
    if len(clean) < 12:
        return
    if len(clean) > ASK_CHARS:
        clean = clean[:ASK_CHARS - 1] + "…"
    state["asks"] = ((state.get("asks") or []) + [clean])[-ASK_LOG_MAX:]


def on_prompt(ctx, prompt: str, sig: list) -> list:
    """Context lines for a user prompt. Updates the ask log and turn state on ctx.state."""
    st, out = ctx.state, []
    nudge = st.pop("ask_nudge", 0)
    if nudge:
        out.append(f"honey intent: your last reply did not report status for the {nudge} numbered asks before it. "
                   "If any are unfinished, say which in one line first.")
    st["ask_items"] = 0
    if status_check(prompt):
        log = ledger(st)
        observe.record(ctx, "intent", kind="status", n=len(st.get("asks") or []))
        if log:
            out.append("honey intent: the user wants to know if their asks are done. Answer item by item from this "
                       "session's ask log: done (with proof), partial, or not done. Skip items that were only questions.\n" + log)
        return out
    listed = items(prompt)
    st["ask_items"] = len(listed)
    if not set(sig) & {"note", "accept", "continue"}:
        _log(st, prompt)
    if question_only(prompt):
        observe.record(ctx, "intent", kind="question_only")
        out.append("honey intent: the user says they are only asking. Answer the question; change nothing unless they ask.")
    if listed:
        observe.record(ctx, "intent", kind="items", n=len(listed))
        out.append(f"honey intent: this request lists {len(listed)} asks. Do or answer each one; end with "
                   "`Asks: 1 done · 2 partial (why) · 3 skipped (why)`.")
    found = None if "note" in sig else term(prompt)
    if found:
        out.append(_save_term(ctx, *found))
    return out


def _save_term(ctx, t: str, meaning: str) -> str:
    if ctx.state.get("rsi_off"):
        return f'honey intent: in this session "{t}" means {meaning}. (RSI is off, so it is not stored.)'
    b, merged = curate.add_note(f'when the user says "{t}", it means {meaning}', "project", ctx.agent, ctx.sid,
                                ctx.state.get("project", ""))
    if not b:
        return f'honey intent: in this session "{t}" means {meaning}. (Not stored: {merged}.)'
    observe.record(ctx, "note", id=b["id"], scope=b["scope"], term=True)
    return (f'honey RSI saved term {b["id"]} [{b["scope"]}]: "{t}" means {meaning}. Use it from now on. '
            f'Acknowledge in one short line; undo with `honey rsi forget {b["id"]}`.')


def on_stop(ctx, msg: str) -> None:
    """Edit turn that listed asks but reported none → one nudge on the next prompt. Never blocks."""
    st = ctx.state
    n = st.pop("ask_items", 0)
    if n >= 2 and st.get("edited") and not ASKS_LINE.search(msg or ""):
        st["ask_nudge"] = n
        observe.record(ctx, "ask_gap", n=n)
