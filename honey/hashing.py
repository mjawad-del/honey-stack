"""sha256 without loading OpenSSL. `hashlib` imports `_hashlib` (~3 ms), paid on every tool-call hook just to
fingerprint a command; CPython's builtin implementation gives the same digest."""
from __future__ import annotations

try:
    from _sha2 import sha256  # CPython 3.12+
except ImportError:
    try:
        from _sha256 import sha256  # CPython 3.9-3.11
    except ImportError:
        from hashlib import sha256  # other interpreters

__all__ = ["sha256"]
