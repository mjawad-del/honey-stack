"""Redaction, injection scan, keywords and similarity. Everything RSI stores passes through redact()."""
from __future__ import annotations

import hashlib
import os
import re
import time

_SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8000}?-----END [A-Z ]*PRIVATE KEY-----"), "<private-key>"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), "<secret>"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "<secret>"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "<secret>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<secret>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<secret>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<secret>"),
    (re.compile(r"\bsk_live_[A-Za-z0-9]{10,}"), "<secret>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<jwt>"),
    # Keyword may follow "_" (OPENAI_API_KEY=), take "is" ("password is ..."), or carry a Bearer scheme.
    (re.compile(r"(?i)((?:\b|_)(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization|credentials?|auth))"
                r"(\s*[:=]\s*|\s+(?:is\s+)?)(['\"]?)(?:bearer\s+)?[^\s'\"]{8,256}\3"), r"\1\2<secret>"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1<secret>"),
    (re.compile(r"(://)[^/\s:@]{1,64}:[^/\s@]{1,128}@"), r"\1<creds>@"),
    (re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<blob>"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}"), "<blob>"),
]
_HOME = os.path.expanduser("~")


def redact(s: str, limit: int = 300) -> str:
    if not s:
        return ""
    s = str(s)[:20000]
    for rx, rep in _SECRETS:
        s = rx.sub(rep, s)
    if _HOME and _HOME != "/":
        s = s.replace(_HOME, "~")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]", "", s)
    return s[:limit]


_INJECTION = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)|disregard (?:the )?(?:system|previous)"
    r"|you are now|new instructions:|</?(?:system|instructions?|tool_call|function_calls|antml:[a-z_]+)>"
    r"|(?:curl|wget)[^\n|]{0,200}\|\s*(?:ba|z)?sh|base64\s+-d|exfiltrat|send (?:it|this|the \w+) to|https?://)"
)


def injection_hit(s: str) -> bool:
    """True if text looks like an instruction-injection attempt. Such text never becomes a rule."""
    return bool(_INJECTION.search(s or ""))


_STOP = set("""a an the and or but if then else when while of to in on at by for with from into onto over under is are
was were be been being it its this that these those i you we they he she me my your our their them us do does did
done have has had not no yes so as just only also very can could should would will shall may might must about
please pls ok okay now here there what which who how why where all any some more most less same than too again
""".split())


def keywords(s: str, n: int = 8) -> list:
    words = re.findall(r"[a-z][a-z0-9_+.-]{2,30}", (s or "").lower())
    out = []
    for w in words:
        w = w.strip(".-")
        if w and w not in _STOP and w not in out:
            out.append(w)
        if len(out) >= n:
            break
    return out


def tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in _STOP and len(w) > 1}


def jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def short_id(prefix: str, seed: str) -> str:
    return prefix + "-" + hashlib.sha1(f"{seed}{time.time_ns()}".encode()).hexdigest()[:6]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
