"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory
(design #19214/#19242: `terminal.cwd` is bridged once to `TERMINAL_CWD` at
gateway/cron startup). The local-CLI backend deliberately leaves it unset and
relies on the launch dir. Reading it in one place keeps the system prompt, the
tool surfaces, and context-file discovery agreeing on where the agent lives.

Multi-session gateways can pin a logical cwd via the `_SESSION_CWD`
contextvar; CLI/cron fall through to `TERMINAL_CWD`/launch cwd.
"""

import os
import sys
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)
_WARNED_DELETED_CWD = False


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def _session_cwd_override() -> str:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return ""
    return str(value).strip()


def safe_getcwd() -> str:
    """Return the process cwd, tolerating a deleted launch directory.

    ``os.getcwd()`` raises ``FileNotFoundError`` when the process was launched
    from a directory that has since been removed. Kanban workers have an
    authoritative fallback in ``HERMES_KANBAN_WORKSPACE``; otherwise use
    ``$HOME`` so CLI/config startup can continue far enough to report useful
    errors instead of crashing during import-time cwd resolution.
    """
    global _WARNED_DELETED_CWD
    try:
        return os.getcwd()
    except FileNotFoundError:
        fallback = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
        if not fallback or not Path(fallback).expanduser().is_dir():
            fallback = os.environ.get("HOME") or str(Path.home())
        if not _WARNED_DELETED_CWD:
            print(
                "warning: current working directory no longer exists; "
                f"falling back to {fallback}",
                file=sys.stderr,
            )
            _WARNED_DELETED_CWD = True
        return fallback


def resolve_agent_cwd() -> Path:
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
    return Path(safe_getcwd())


def resolve_context_cwd() -> Path | None:
    # None means "no configured cwd": build_context_files_prompt then falls back
    # to the launch dir (os.getcwd()) — correct for the local CLI. The gateway
    # avoids slurping its install dir by setting TERMINAL_CWD (see system_prompt.py)
    # or, per session, the _SESSION_CWD contextvar above.
    override = _session_cwd_override()
    if override:
        return Path(override).expanduser()
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    return Path(raw).expanduser() if raw else None
