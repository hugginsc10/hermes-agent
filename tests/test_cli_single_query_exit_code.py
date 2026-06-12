"""Single-query (-q) exit-code propagation (2026-06-12 forensics fix).

A fatal pre-turn abort (provider 401 before the first model turn, agent
init failure) previously exited 0 from the -q path, so kanban workers were
recorded as rc=0 protocol violations instead of infra failures — 90/110
crashed runs in the 7-day window traced to one expired Codex OAuth token
burning retries under the wrong label.
"""
from __future__ import annotations

import pytest

from cli import _single_query_exit_code
from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE


@pytest.mark.parametrize("failed,reason,kanban,expected", [
    # Clean run: 0 regardless of context.
    (False, None, False, 0),
    (False, None, True, 0),
    # Terminal failure: plain 1 for non-kanban automation wrappers.
    (True, None, False, 1),
    (True, "auth", False, 1),
    (True, "rate_limit", False, 1),
    # Kanban worker, infra failure: 1 so the dispatcher records an infra
    # crash (and the provider-auth log-tail classifier can fire).
    (True, None, True, 1),
    (True, "auth", True, 1),
    # Kanban worker, quota wall: EX_TEMPFAIL sentinel so the dispatcher
    # requeues without counting a failure (mirrors the -Q contract).
    (True, "rate_limit", True, KANBAN_RATE_LIMIT_EXIT_CODE),
    (True, "billing", True, KANBAN_RATE_LIMIT_EXIT_CODE),
])
def test_single_query_exit_code(failed, reason, kanban, expected):
    assert _single_query_exit_code(failed, reason, kanban) == expected
