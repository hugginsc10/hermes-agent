"""Tests for agent/runtime_cwd.py — the single source of truth for the agent working directory."""

import os
from pathlib import Path

import agent.runtime_cwd as rt
from agent.runtime_cwd import (
    clear_session_cwd,
    resolve_agent_cwd,
    resolve_context_cwd,
    set_session_cwd,
)



class TestResolveAgentCwd:
    def test_prefers_terminal_cwd_over_getcwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.chdir(os.path.expanduser("~"))
        assert resolve_agent_cwd() == tmp_path

    def test_falls_back_to_getcwd_when_unset(self, monkeypatch, tmp_path):
        # The #19242 local-CLI contract: TERMINAL_CWD is unset, so the launch dir wins.
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_cwd() == tmp_path

    def test_skips_nonexistent_terminal_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "gone"))
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_cwd() == tmp_path

    def test_expands_leading_tilde(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", "~")
        assert resolve_agent_cwd() == Path(os.path.expanduser("~"))

    def test_whitespace_only_terminal_cwd_falls_back_to_getcwd(self, monkeypatch, tmp_path):
        # "   ".strip() → "" → falsy, so the launch dir wins (not a "   " path).
        monkeypatch.setenv("TERMINAL_CWD", "   ")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_cwd() == tmp_path

    def test_deleted_cwd_falls_back_to_kanban_workspace(self, monkeypatch, tmp_path, capsys):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
        monkeypatch.setattr(rt.os, "getcwd", lambda: (_ for _ in ()).throw(FileNotFoundError()))
        monkeypatch.setattr(rt, "_WARNED_DELETED_CWD", False)

        assert resolve_agent_cwd() == workspace
        assert "current working directory no longer exists" in capsys.readouterr().err

    def test_deleted_cwd_falls_back_to_home_without_kanban_workspace(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "gone"))
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setattr(rt.os, "getcwd", lambda: (_ for _ in ()).throw(FileNotFoundError()))
        monkeypatch.setattr(rt, "_WARNED_DELETED_CWD", False)

        assert resolve_agent_cwd() == home


class TestResolveContextCwd:
    def test_returns_dir_when_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert resolve_context_cwd() == tmp_path

    def test_returns_none_when_unset(self, monkeypatch):
        # Unset → None; the caller (build_context_files_prompt) then getcwds —
        # the local-CLI #19242 contract. Discovery still runs; it is NOT skipped.
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert resolve_context_cwd() is None

    def test_returns_nonexistent_dir_unguarded(self, monkeypatch, tmp_path):
        # Deliberate asymmetry vs resolve_agent_cwd: context discovery has no isdir
        # guard, so a missing dir is returned (not None) — discovery just finds nothing.
        missing = tmp_path / "gone"
        monkeypatch.setenv("TERMINAL_CWD", str(missing))
        assert resolve_context_cwd() == missing

    def test_expands_leading_tilde(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_CWD", "~")
        assert resolve_context_cwd() == Path(os.path.expanduser("~"))

    def test_whitespace_only_terminal_cwd_returns_none(self, monkeypatch):
        # "   ".strip() → "" → None, so the caller getcwds for discovery rather
        # than building Path("   ") and resolving garbage under the launch dir.
        monkeypatch.setenv("TERMINAL_CWD", "   ")
        assert resolve_context_cwd() is None


class TestSessionCwdOverride:
    """The #29531 per-session arm: a contextvar cwd wins over TERMINAL_CWD so a
    multi-session gateway can pin each session to its own folder."""

    def test_session_cwd_overrides_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            assert resolve_agent_cwd() == other
            assert resolve_context_cwd() == other
        finally:
            rt._SESSION_CWD.reset(token)

    def test_empty_session_cwd_falls_back_to_terminal_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd("")
        try:
            assert resolve_agent_cwd() == tmp_path
            assert resolve_context_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)

    def test_clear_session_cwd_restores_terminal_cwd(self, monkeypatch, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(other))
        try:
            clear_session_cwd()
            assert resolve_agent_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)

    def test_nonexistent_session_cwd_falls_back(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        token = set_session_cwd(str(tmp_path / "gone"))
        try:
            # resolve_agent_cwd guards on isdir; a missing session cwd must not win.
            assert resolve_agent_cwd() == tmp_path
        finally:
            rt._SESSION_CWD.reset(token)
