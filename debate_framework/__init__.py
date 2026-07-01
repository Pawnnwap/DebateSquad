"""Runtime primitives used by compiled DebateSquad."""

from .opencode_runner import (
    OPENCODE_BIN,
    OpenCodeError,
    OpenCodeParseError,
    OpenCodeRunner,
    OpenCodeTimeoutError,
    generate_session_id,
    truncate_at_sentence,
)
from .utils import fix_stdout_encoding, setup_logging

__all__ = [
    "OpenCodeRunner",
    "OPENCODE_BIN",
    "OpenCodeError",
    "OpenCodeTimeoutError",
    "OpenCodeParseError",
    "generate_session_id",
    "truncate_at_sentence",
    "setup_logging",
    "fix_stdout_encoding",
]
