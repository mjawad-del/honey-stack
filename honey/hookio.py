"""Hook stdin/stdout. Same JSON shape for Claude Code and Codex (hookSpecificOutput)."""
from __future__ import annotations

import json
import os
import sys

MAX_CONTEXT = 10000  # both agents cap additionalContext; beyond this it is spilled to a file


def read_payload() -> dict:
    raw = sys.stdin.buffer.read(2 * 1024 * 1024).decode("utf-8", "replace").lstrip("﻿")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def emit(obj: dict) -> None:
    if obj:
        os.write(1, (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:MAX_CONTEXT]}}

