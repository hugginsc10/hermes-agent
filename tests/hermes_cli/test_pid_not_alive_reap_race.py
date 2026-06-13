"""Regression tests for the 'pid N not alive' worker reap-race.

Ported from the 2026-06-11 reliability overhaul (commit 2726dd1ac), whose
dispatcher-side fix had not yet landed in the live gateway. Root cause: a
dispatcher-spawned worker dies, an unrelated ``subprocess.Popen`` (e.g. the
``ps`` probe in ``_pid_alive``) triggers ``subprocess._cleanup()`` which reaps
the abandoned child and steals its exit status, so ``_classify_worker_exit``
returns ``"unknown"`` and the dispatcher logs a generic ``pid N not alive``
crash. The fix keeps each worker's ``Popen`` handle in a per-spawn daemon
waiter thread (out of ``subprocess._active``, so ``_cleanup()`` can't steal it)
that records the real returncode, and gates ``reap_worker_zombies``' global
``waitpid(-1)`` sweep to fallback-only while any waiter is live.

These cover only the kanban_db.py waiter/exit-status mechanism; the overhaul's
worker-side exit-code and protocol-epilogue pieces landed separately via PR #7/#8.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def _clean_exit_registry():
    """Each test starts with an empty reap registry / waiter set."""
    kb._recent_worker_exits.clear()
    kb._worker_waiter_pids.clear()
    yield
    kb._recent_worker_exits.clear()
    kb._worker_waiter_pids.clear()


@pytest.mark.skipif(os.name == "nt", reason="POSIX wait-status semantics")
class TestExitStatusRecording:
    def test_rc_to_raw_status_round_trips_exit_codes(self):
        for rc, kind in ((0, "clean_exit"), (1, "nonzero_exit"),
                         (kb.KANBAN_RATE_LIMIT_EXIT_CODE, "rate_limited")):
            kb._recent_worker_exits.clear()
            kb._record_worker_exit_rc(4242, rc)
            got_kind, got_code = kb._classify_worker_exit(4242)
            assert got_kind == kind
            assert got_code == rc

    def test_rc_to_raw_status_round_trips_signals(self):
        kb._record_worker_exit_rc(4243, -9)  # Popen reports SIGKILL as -9
        kind, code = kb._classify_worker_exit(4243)
        assert kind == "signaled"
        assert code == 9

    def test_record_rc_never_overwrites_reaped_status(self):
        # reap_worker_zombies consumed the real status (rc=1) first; the
        # waiter's Popen.wait() then reports the synthetic ECHILD rc=0,
        # which must NOT clobber the authoritative entry.
        kb._record_worker_exit(4244, kb._rc_to_raw_status(1))
        kb._record_worker_exit_rc(4244, 0)
        kind, code = kb._classify_worker_exit(4244)
        assert (kind, code) == ("nonzero_exit", 1)

    def test_waiter_thread_records_exit_despite_unrelated_subprocess(self):
        # The historical failure mode: worker dies, an unrelated Popen
        # (e.g. the `ps` probe in _pid_alive) triggers subprocess._cleanup()
        # which steals the abandoned child's status, and
        # _classify_worker_exit returns "unknown". With the waiter thread
        # holding the handle, the status is recorded no matter what else
        # spawns in between.
        proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
        pid = proc.pid
        kb._spawn_worker_exit_waiter(proc)
        # Unrelated subprocess churn while the worker dies.
        for _ in range(3):
            subprocess.run([sys.executable, "-c", "pass"], check=False)
        deadline = time.time() + 10
        while time.time() < deadline and pid not in kb._recent_worker_exits:
            time.sleep(0.05)
        kind, code = kb._classify_worker_exit(pid)
        assert (kind, code) == ("nonzero_exit", 7)
        assert pid not in kb._worker_waiter_pids  # waiter cleaned up

    def test_reap_sweep_is_skipped_while_waiters_live(self, monkeypatch):
        calls = []

        def _fake_waitpid(pid, flags):
            calls.append((pid, flags))
            raise ChildProcessError

        monkeypatch.setattr(kb.os, "waitpid", _fake_waitpid)
        kb._worker_waiter_pids.add(999999)
        assert kb.reap_worker_zombies() == []
        assert calls == []  # waitpid(-1) sweep must not run
        kb._worker_waiter_pids.clear()
        kb.reap_worker_zombies()
        assert calls  # fallback sweep resumes with no waiters
