"""Keeps secrets out of everything kiri-gate writes down or shows: the decision log, the ledger file,
the terminal prompt and the approval page. The tool itself still gets the real values.

What gets hidden:
  - any argument whose name looks secret (password, token, api_key, secret, card_number, cvv, ...),
    at any depth
  - any string that looks like a well-known credential (sk_live_..., ghp_..., AKIA..., "Bearer ...")
  - any names you add yourself: @gate.tool(EXTERNAL, sensitive={"note"})

This is a name and pattern check, not a guarantee. A secret in a field called "body" that doesn't look
like a known key format will not be caught. Mark fields like that as sensitive yourself.
"""

from __future__ import annotations

import re
from typing import Any, FrozenSet, Iterable

REDACTED = "[REDACTED]"

_NAME = re.compile(
    r"(password|passwd|passphrase|secret|token|api_?key|access_?key|private_?key|client_?secret|"
    r"authorization|auth_?header|cookie|session_?id|credential|card_?number|card_?no|cvv|cvc|"
    r"\bssn\b|\biban\b|\bpin\b|\botp\b)",
    re.I,
)
_NOT_SECRET = {"idempotency_key", "token_count", "max_tokens", "tokens"}
_VALUE = re.compile(
    r"^(sk_(live|test)_|rk_(live|test)_|pk_live_|ghp_|gho_|github_pat_|xox[abpr]-|AKIA[0-9A-Z]{12}|"
    r"sk-[A-Za-z0-9_-]{16,}|Bearer\s|eyJ[A-Za-z0-9_-]{10,}\.)"
)


def sensitive_name(name: str, extra: Iterable[str] = ()) -> bool:
    n = str(name)
    if n.lower() in _NOT_SECRET:
        return False
    if n in extra or n.lower() in {e.lower() for e in extra}:
        return True
    return bool(_NAME.search(n.replace("-", "_")))


def redact(value: Any, extra: FrozenSet[str] = frozenset()) -> Any:
    """A copy with secret-looking fields replaced by [REDACTED]. Never changes the input."""
    if isinstance(value, dict):
        return {k: (REDACTED if sensitive_name(k, extra) and value[k] not in (None, "")
                    else redact(value[k], extra)) for k in value}
    if isinstance(value, (list, tuple)):
        return [redact(v, extra) for v in value]
    if isinstance(value, str) and _VALUE.match(value.strip()):
        return REDACTED
    return value


def restore(edited: Any, original: Any) -> Any:
    """After a human edits redacted arguments, put the real values back wherever [REDACTED] was left as is."""
    if edited == REDACTED:
        return original
    if isinstance(edited, dict) and isinstance(original, dict):
        return {k: restore(v, original.get(k)) for k, v in edited.items()}
    if isinstance(edited, list) and isinstance(original, list) and len(edited) == len(original):
        return [restore(e, o) for e, o in zip(edited, original)]
    return edited
