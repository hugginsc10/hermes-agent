"""Regression tests: gateway runtime-status writes must never leave test sandboxes.

A kanban worker's run of ``tests/gateway/test_feishu.py`` once merged its own
pid/argv into the operator's real ``~/.hermes/gateway_state.json``:
``gateway.status.write_runtime_status`` does a read-modify-write of
``{HERMES_HOME}/gateway_state.json``, and a write fired outside the per-test
env-redirect window (the ``_hermetic_environment`` monkeypatch is restored
between tests, so leaked background machinery resolved the real home).

These tests pin both defense layers:

1. per-test isolation — the writer resolves into the per-test ``HERMES_HOME``;
2. the session backstop — between-test env still points into a session
   sandbox (armed in ``tests/conftest.py::pytest_configure``), so even
   out-of-window writes land in a throwaway directory.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

from tests.conftest import (
    HERMES_TEST_REAL_DEFAULT_HOME_ENV,
    HERMES_TEST_SESSION_HOME_ENV,
)

# Captured by the session backstop before the default fallback is patched.
_REAL_DEFAULT_HOME = os.environ.get(HERMES_TEST_REAL_DEFAULT_HOME_ENV, "")


def _assert_outside_real_home(path: Path) -> None:
    assert _REAL_DEFAULT_HOME, "session backstop did not record the real default home"
    real_home = Path(_REAL_DEFAULT_HOME).resolve()
    resolved = path.resolve()
    assert resolved != real_home, f"{path} is the real Hermes home"
    assert real_home not in resolved.parents, f"{path} is inside the real Hermes home"


def test_write_runtime_status_lands_in_per_test_home():
    from gateway.status import write_runtime_status

    test_home = Path(os.environ["HERMES_HOME"])
    _assert_outside_real_home(test_home)

    write_runtime_status(gateway_state="running")

    state_path = test_home / "gateway_state.json"
    assert state_path.exists(), "runtime status was not written to the per-test home"
    payload = json.loads(state_path.read_text())
    assert payload["pid"] == os.getpid()


def test_cleared_environ_cannot_reach_the_real_home():
    """The exact observed clobber: a ``patch.dict(clear=True)`` block wipes
    HERMES_HOME, and a runtime-status write inside it used to fall through to
    the real ~/.hermes. The session-patched default fallback must catch it."""
    from gateway.status import _get_runtime_status_path, write_runtime_status

    session_home = Path(os.environ[HERMES_TEST_SESSION_HOME_ENV])

    with patch.dict(os.environ, {}, clear=True):
        resolved = _get_runtime_status_path().resolve()
        _assert_outside_real_home(resolved)
        assert resolved == (session_home / "gateway_state.json").resolve()
        write_runtime_status(gateway_state="running", platform="feishu", platform_state="connected")
        assert resolved.exists()
        payload = json.loads(resolved.read_text())
        assert payload["pid"] == os.getpid()


def test_session_backstop_catches_out_of_test_writes(monkeypatch):
    session_home = os.environ.get(HERMES_TEST_SESSION_HOME_ENV)
    assert session_home, (
        "session HERMES_HOME backstop is not armed — see "
        "tests/conftest.py::_install_session_home_backstop"
    )
    session_path = Path(session_home)
    assert session_path.is_dir()
    _assert_outside_real_home(session_path)

    # Simulate the leak window: between tests, monkeypatch has restored
    # HERMES_HOME to its outer value — which must be the session sandbox,
    # so a late write from leaked machinery stays inside it.
    monkeypatch.setenv("HERMES_HOME", session_home)

    from gateway.status import _get_runtime_status_path, write_runtime_status

    resolved = _get_runtime_status_path().resolve()
    assert resolved == (session_path / "gateway_state.json").resolve()
    _assert_outside_real_home(resolved)

    write_runtime_status(gateway_state="running")
    assert resolved.exists()
    payload = json.loads(resolved.read_text())
    assert payload["pid"] == os.getpid()
