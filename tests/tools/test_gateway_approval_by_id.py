"""Unit tests for the Phase-1 gateway approval id surface.

Covers the two new approval.py helpers that back the REST routes
(``GET /v1/approvals`` and ``POST /v1/approvals/{id}/{choice}``):

* :func:`tools.approval.list_gateway_approvals`
* :func:`tools.approval.resolve_gateway_approval_by_id`

plus the ``approval_id`` / ``requested_at`` / ``expires_at`` stamping that
:func:`tools.approval._await_gateway_decision` now performs at enqueue.
"""

import threading
import time
import uuid
from unittest.mock import patch

import pytest

from tools import approval as mod


@pytest.fixture(autouse=True)
def _clear_approval_state():
    """Reset module-level approval state around every test (mirrors the
    pattern in tests/tools/test_approval_heartbeat.py)."""
    def _clear():
        mod._gateway_queues.clear()
        mod._gateway_notify_cbs.clear()
        mod._session_approved.clear()
        mod._permanent_approved.clear()
        mod._pending.clear()

    _clear()
    yield
    _clear()


def _enqueue(session_key: str, *, command: str = "rm -rf /tmp/x",
             description: str = "danger", allow_permanent: bool = True,
             pattern_keys=None) -> mod._ApprovalEntry:
    """Enqueue a pending approval the way _await_gateway_decision does, without
    driving the full blocking wait — for fast, deterministic list/resolve
    unit tests."""
    data = {
        "command": command,
        "description": description,
        "pattern_keys": pattern_keys or ["danger:rm"],
        "allow_permanent": allow_permanent,
    }
    entry = mod._ApprovalEntry(data)
    entry.approval_id = uuid.uuid4().hex
    entry.session_key = session_key
    entry.requested_at = time.time()
    entry.expires_at = entry.requested_at + 900
    with mod._lock:
        mod._gateway_queues.setdefault(session_key, []).append(entry)
    return entry


class TestListGatewayApprovals:
    def test_empty(self):
        assert mod.list_gateway_approvals() == []

    def test_lists_across_sessions_oldest_first(self):
        e1 = _enqueue("s1", command="cmd1")
        time.sleep(0.01)
        e2 = _enqueue("s2", command="cmd2")
        out = mod.list_gateway_approvals()
        assert [a["approval_id"] for a in out] == [e1.approval_id, e2.approval_id]
        first = out[0]
        assert first["session_key"] == "s1"
        assert first["command"] == "cmd1"
        assert first["allow_permanent"] is True
        assert first["expires_at"] > first["requested_at"]
        assert first["pattern_keys"] == ["danger:rm"]

    def test_returns_plain_serializable_dicts(self):
        _enqueue("s1")
        out = mod.list_gateway_approvals()
        assert all(isinstance(a, dict) for a in out)
        # No _ApprovalEntry internals (threading.Event) escape the lock.
        assert "event" not in out[0]
        assert "result" not in out[0]


class TestResolveById:
    def test_resolve_unblocks_and_sets_choice(self):
        e = _enqueue("s1")
        res = mod.resolve_gateway_approval_by_id(e.approval_id, "once")
        assert res == {"resolved": True, "session_key": "s1", "choice": "once"}
        assert e.result == "once"
        assert e.event.is_set()
        assert mod.list_gateway_approvals() == []

    def test_unknown_id_not_found(self):
        assert mod.resolve_gateway_approval_by_id("does-not-exist", "once") == {
            "resolved": False, "reason": "not_found"}

    def test_empty_id_not_found(self):
        assert mod.resolve_gateway_approval_by_id("", "once")["resolved"] is False

    def test_double_resolve_second_is_noop_first_writer_wins(self):
        e = _enqueue("s1")
        first = mod.resolve_gateway_approval_by_id(e.approval_id, "deny")
        second = mod.resolve_gateway_approval_by_id(e.approval_id, "once")
        assert first["resolved"] is True
        assert second == {"resolved": False, "reason": "not_found"}
        assert e.result == "deny"

    def test_resolve_one_leaves_others(self):
        e1 = _enqueue("s1", command="c1")
        e2 = _enqueue("s1", command="c2")
        mod.resolve_gateway_approval_by_id(e2.approval_id, "once")
        remaining = mod.list_gateway_approvals()
        assert [a["approval_id"] for a in remaining] == [e1.approval_id]


class TestAlwaysDowngrade:
    def test_always_downgraded_when_not_permanent(self):
        e = _enqueue("s1", allow_permanent=False)
        res = mod.resolve_gateway_approval_by_id(e.approval_id, "always")
        assert res["choice"] == "session"
        assert e.result == "session"

    def test_always_kept_when_permanent(self):
        e = _enqueue("s1", allow_permanent=True)
        res = mod.resolve_gateway_approval_by_id(e.approval_id, "always")
        assert res["choice"] == "always"
        assert e.result == "always"

    def test_other_choices_untouched_when_not_permanent(self):
        e = _enqueue("s1", allow_permanent=False)
        res = mod.resolve_gateway_approval_by_id(e.approval_id, "once")
        assert res["choice"] == "once"
        assert e.result == "once"

    def test_always_kept_when_allow_permanent_absent(self):
        # execute_code approvals omit allow_permanent entirely (no tirith
        # concept); "always" must persist, NOT be silently downgraded.
        entry = mod._ApprovalEntry({
            "command": "print(1)", "description": "exec",
            "pattern_keys": ["exec:code"],  # note: no "allow_permanent" key
        })
        entry.approval_id = uuid.uuid4().hex
        entry.session_key = "s1"
        with mod._lock:
            mod._gateway_queues.setdefault("s1", []).append(entry)
        res = mod.resolve_gateway_approval_by_id(entry.approval_id, "always")
        assert res["choice"] == "always"
        assert entry.result == "always"


class TestTeardownGhost:
    def test_unregister_drops_entry_then_resolve_not_found(self):
        e = _enqueue("s1")
        mod.unregister_gateway_notify("s1")
        assert mod.list_gateway_approvals() == []
        assert mod.resolve_gateway_approval_by_id(e.approval_id, "once") == {
            "resolved": False, "reason": "not_found"}

    def test_clear_session_drops_entry(self):
        e = _enqueue("s1")
        mod.clear_session("s1")
        assert mod.list_gateway_approvals() == []
        assert mod.resolve_gateway_approval_by_id(e.approval_id, "once")["resolved"] is False


class TestConcurrentResolveRace:
    def test_by_id_vs_fifo_resolve_same_entry_exactly_once(self):
        """A web resolve-by-id racing a FIFO (Telegram) resolve on the SAME
        single entry must produce exactly one winner; the other is a no-op."""
        e = _enqueue("s1")
        results: dict = {}
        barrier = threading.Barrier(2)

        def web():
            barrier.wait()
            results["web"] = mod.resolve_gateway_approval_by_id(e.approval_id, "once")

        def fifo():
            barrier.wait()
            results["fifo"] = mod.resolve_gateway_approval("s1", "deny")

        t1 = threading.Thread(target=web)
        t2 = threading.Thread(target=fifo)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        web_ok = results["web"].get("resolved") is True
        fifo_ok = results["fifo"] >= 1  # FIFO returns count of entries resolved
        assert web_ok ^ fifo_ok, (results["web"], results["fifo"])
        assert mod.list_gateway_approvals() == []
        assert e.event.is_set()


class TestAwaitGatewayDecisionStamping:
    """The real enqueue path (_await_gateway_decision) must stamp a uuid id +
    wall-clock expiry, surface them on the notify payload, and unblock the
    waiting agent thread when resolved by id."""

    def test_enqueue_stamps_id_and_expiry_and_resolve_by_id_unblocks(self):
        session_key = "integ-s1"
        notified: dict = {}
        result_box: dict = {}

        def notify_cb(data):
            notified.update(data)

        with patch.object(mod, "_fire_approval_hook", lambda *a, **k: None), \
                patch.object(mod, "_get_approval_config",
                             return_value={"gateway_timeout": 900}):

            def run():
                result_box["decision"] = mod._await_gateway_decision(
                    session_key, notify_cb,
                    {"command": "rm -rf /tmp/y", "description": "d",
                     "pattern_keys": ["danger:rm"], "allow_permanent": False},
                    surface="gateway",
                )

            t = threading.Thread(target=run)
            t.start()

            deadline = time.time() + 5
            listing: list = []
            while time.time() < deadline:
                listing = mod.list_gateway_approvals()
                if listing:
                    break
                time.sleep(0.01)
            assert listing, "approval was never enqueued"

            item = listing[0]
            approval_id = item["approval_id"]
            assert approval_id and len(approval_id) == 32  # uuid4().hex
            # expires_at ~= requested_at + the (patched) 900s gateway timeout
            assert abs((item["expires_at"] - item["requested_at"]) - 900) < 2
            # The notify payload carried the id + expiry to every channel.
            assert notified.get("approval_id") == approval_id
            assert "expires_at" in notified

            # Resolve by id; allow_permanent=False downgrades always -> session.
            res = mod.resolve_gateway_approval_by_id(approval_id, "always")
            assert res["resolved"] is True
            assert res["choice"] == "session"

            t.join(timeout=5)
            assert not t.is_alive()
            assert result_box["decision"]["resolved"] is True
            assert result_box["decision"]["choice"] == "session"
