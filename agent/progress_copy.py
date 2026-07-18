"""Production-safe copy filtering for periodic Kanban progress messages."""

from __future__ import annotations

import re

from agent.redact import redact_visible_text

DEFAULT_HUMAN_PROGRESS_TEXT = (
    "El trabajo sigue en curso; compartiré el próximo hito cuando esté disponible."
)
DEFAULT_HUMAN_PROGRESS_TITLE = "Tarea en curso"

# Progress copy is an owner-facing product surface, not a debug stream. These
# patterns cover closed technical formats and implementation vocabulary; they
# do not try to interpret human intent. Unsafe commentary is replaced by a
# generic progress sentence rather than partially redacted into misleading text.
_UNSAFE_PROGRESS_PATTERNS = (
    re.compile(r"```|`"),
    re.compile(
        r"(?<!\w)/(?:Users|home|private|tmp|var|etc|opt|usr|Volumes|workspace|srv|app)"
        r"(?:/[\w.@%+=:,~\-]+)+",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]+"),
    re.compile(r"\b(?:[\w.\-]+/)+(?:[\w.\-]+)\b"),
    re.compile(r"\b[\w.\-]+\.(?:py|js|ts|tsx|jsx|yaml|yml|json|toml|sql|sh|log)\b", re.IGNORECASE),
    re.compile(r"https?://\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:pytest|npm|pnpm|yarn|pip|uv|git|curl|wget|ssh|bash|zsh|powershell|"
        r"docker|kubectl|terraform)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:claude|codex|fable|opus|sonnet|openai|anthropic|oauth|api[ -]?key|"
        r"provider|model|tool[ -]?call|terminal|browser[ -]?tool)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:traceback|stack[ -]?trace|stdout|stderr|exit[ -]?code|exception|logs?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:task|run|job)[_-]?id\b|\bt_[0-9a-f]{8,}\b|\b[0-9a-f]{12,40}\b",
        re.IGNORECASE,
    ),
)


def sanitize_human_progress_text(value: object, *, max_chars: int = 600) -> str:
    """Return redacted human copy, or ``""`` when it contains internals."""
    try:
        text = redact_visible_text(value).strip()
    except Exception:
        return ""
    if not text:
        return ""
    bounded = text[: max(1, int(max_chars))].rstrip()
    if any(pattern.search(bounded) for pattern in _UNSAFE_PROGRESS_PATTERNS):
        return ""
    return bounded
