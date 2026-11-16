# Intent loop plan

**Goal:** fewer misread requests and "is everything I asked done?" follow-ups, by checking intent before acting and reconciling every ask at the end.
**Approach:** HOLD. Always-on rule text plus deterministic, per-prompt hints from the existing UserPromptSubmit, Stop and compact hooks. Reuse the `remember:` note ledger for terms. Every hint is advisory, and a stop is never blocked.
**Design:** chat grill on 2026-11-16, based on private local session evidence (not published): the agent almost never asked a clarifying question, most corrections were misread requests rather than bugs, and "is everything I asked done?" follow-ups were common. The user delegated every choice: all three parts, ask only when a redo is likely, advisory nudge.

## Global constraints
- The rendered bootstrap stays under 3100 chars for both agents (`tests/test_hooks.py`).
- A hint goes out only when its pattern matches. A plain prompt gets no added context.
- `rsi off` stops term saving. `honey off` stops every intent hint.
- Harness or system prompts never feed the ask log.

## File map
- `honey/intent.py`: prompt hints, the session ask log, term detection and the stop gap (create)
- `honey/dispatch.py`: call intent from user_prompt, stop and compact session_start (modify)
- `honey/bootstrap.md`, `honey/modes.py`: intent-check and ask-ledger rule, with an `{ask}` placeholder per agent (modify)
- `tests/test_intent.py`: unit tests and hook scenarios (create)
- `README.md`, `CHANGELOG.md`, `docs/skills.md`: document the behavior (modify)

## Review focus
1. False positives: "what does X mean?", "can you fix 1 thing" and a single list item must not trigger.
2. A term phrased as a question ("does ship mean merged?") must not be saved.
3. Compaction must restore the ask log so asks made before it survive.
4. The nudge fires at most once and never on a Q&A turn.

### Task 1: intent module
**Interfaces:** `items(p) -> list[str]`, `question_only(p) -> bool`, `status_check(p) -> bool`, `term(p) -> tuple[str, str] | None`, `on_prompt(ctx, prompt, sig) -> list[str]`, `on_stop(ctx, msg) -> None`, `ledger(state) -> str`
- [ ] Tests: two or more numbered or bulleted lines → items; "i am just asking whether we can deploy" → question_only; "so all of the changes i asked implemented?" → status_check; "i told you before that ship means merged too" → ("ship", "merged too"); "what does ship mean?" → None
- [ ] Verify: `python3 tests/test_intent.py` → `intent ok`

### Task 2: wire hooks and rules
- [ ] user_prompt adds the hints; stop records `ask_gap` and sets a one-shot nudge; a compact SessionStart appends the ask log
- [ ] Bootstrap rule names the right question tool for each agent (AskUserQuestion or request_user_input)
- [ ] Verify: `python3 tests/run.py` → all passed
