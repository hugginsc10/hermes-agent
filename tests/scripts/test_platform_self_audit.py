from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from hermes_cli import kanban_db as kb


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "platform-self-audit.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("platform_self_audit", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(kb.SCHEMA_SQL)
    conn.executescript(
        """
        INSERT INTO tasks (id, title, status, assignee, created_at) VALUES
          ('t_auth', 'auth', 'done', 'builder', 1000),
          ('t_skill', 'skill', 'done', 'reviewer', 1000),
          ('t_cwd', 'cwd', 'done', 'builder', 1000),
          ('t_exit1', 'exit1', 'done', 'qa', 1000),
          ('t_exit2', 'exit2', 'done', 'qa', 1000),
          ('t_exit3', 'exit3', 'done', 'qa', 1000),
          ('t_exit4', 'exit4', 'done', 'qa', 1000),
          ('t_exit5', 'exit5', 'done', 'qa', 1000),
          ('t_stale', 'stale', 'done', 'builder', 1000),
          ('t_unknown1', 'unknown1', 'done', 'ops', 1000),
          ('t_unknown2', 'unknown2', 'done', 'ops', 1000),
          ('t_unknown3', 'unknown3', 'done', 'ops', 1000),
          ('t_ok', 'ok', 'done', 'qa', 1000);
        INSERT INTO task_runs (
          task_id, profile, status, started_at, ended_at, outcome, error, error_detail,
          log_offset_start, log_offset_end
        ) VALUES
          ('t_auth', 'builder', 'crashed', 2000, 2010, 'crashed', 'exited code 1', 'Provider authentication required: provider=openai-codex', 10, 99),
          ('t_skill', 'reviewer', 'crashed', 2001, 2011, 'crashed', 'unknown skill: sdlc-review', NULL, 20, 29),
          ('t_cwd', 'builder', 'crashed', 2002, 2012, 'crashed', 'cwd missing: /tmp/nope', NULL, 30, 39),
          ('t_exit1', 'qa', 'crashed', 2003, 2013, 'crashed', 'worker exited code 1', NULL, 40, 49),
          ('t_exit2', 'qa', 'crashed', 2004, 2014, 'crashed', 'worker exited code 1', NULL, 50, 59),
          ('t_exit3', 'qa', 'crashed', 2005, 2015, 'crashed', 'worker exited code 1', NULL, 60, 69),
          ('t_exit4', 'qa', 'crashed', 2006, 2016, 'crashed', 'worker exited code 1', NULL, 70, 79),
          ('t_exit5', 'qa', 'crashed', 2007, 2017, 'crashed', 'worker exited code 1', NULL, 80, 89),
          ('t_stale', 'builder', 'crashed', 2008, 2018, 'crashed', 'pid 123 not alive', NULL, 90, 99),
          ('t_unknown1', 'ops', 'crashed', 2009, 2019, 'crashed', 'mystery one', NULL, 100, 109),
          ('t_unknown2', 'ops', 'crashed', 2010, 2020, 'crashed', 'mystery two', NULL, 110, 119),
          ('t_unknown3', 'ops', 'crashed', 2011, 2021, 'crashed', 'mystery three', NULL, 120, 129),
          ('t_ok', 'qa', 'done', 2012, 2022, 'completed', NULL, NULL, NULL, NULL);
        INSERT INTO task_events (task_id, kind, payload, created_at) VALUES
          ('t_auth', 'protocol_violation', '{"reason":"bad tool alternation"}', 2015);
        """
    )
    conn.commit()
    conn.close()


def test_kanban_task_runs_schema_includes_crash_detail_columns(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="crash-detail")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    with kb.connect(board="crash-detail") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}

    assert {"error_detail", "log_offset_start", "log_offset_end"}.issubset(cols)


def test_legacy_task_runs_migrates_crash_detail_columns(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="legacy-crash-detail")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    conn = sqlite3.connect(db_path)
    conn.executescript(kb.SCHEMA_SQL)
    conn.executescript(
        """
        DROP TABLE task_runs;
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            profile TEXT,
            status TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            ended_at INTEGER,
            outcome TEXT,
            error TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    with kb.connect(board="legacy-crash-detail") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}

    assert {"error_detail", "log_offset_start", "log_offset_end"}.issubset(cols)


def test_loop_telemetry_groups_actionable_crash_signatures_and_alert_policy(tmp_path, monkeypatch):
    audit = load_audit_module()
    db_path = tmp_path / "kanban.db"
    make_db(db_path)
    monkeypatch.setattr(audit, "KANBAN_DB", db_path)
    monkeypatch.setattr(audit, "NOW", datetime.fromtimestamp(3000, tz=timezone.utc))

    metrics, alerts = audit.check_loop_telemetry({})

    by_label = {item["label"]: item for item in metrics["crash_signatures_24h"]}
    assert set(by_label) >= {
        "provider-auth-required",
        "unknown-skill/config-error",
        "cwd-missing",
        "nonzero-exit",
        "protocol-violation",
        "stale-pid",
        "unknown-crash",
    }
    assert by_label["nonzero-exit"]["count"] == 5
    assert by_label["nonzero-exit"]["sample_task_ids"] == ["t_exit1", "t_exit2", "t_exit3"]
    assert by_label["nonzero-exit"]["affected_profiles"] == ["qa"]
    assert by_label["provider-auth-required"]["suggested_owner"] == "platform-auth-owner"
    assert metrics["provider_auth_required_24h"] == 1
    assert any("signature nonzero-exit" in alert for alert in alerts)
    assert any("unknown-crash" in alert for alert in alerts)


def test_signature_alerts_downgrades_deterministic_dominant_crashes():
    audit = load_audit_module()

    alerts = audit.signature_alerts(
        [
            {
                "label": "provider-auth-required",
                "count": 8,
                "sample_task_ids": ["t_auth1", "t_auth2"],
                "affected_profiles": ["builder"],
                "suggested_owner": "platform-auth-owner",
            },
            {
                "label": "unknown-crash",
                "count": 2,
                "sample_task_ids": ["t_unknown1"],
                "affected_profiles": ["ops"],
                "suggested_owner": "platform-reliability-owner",
            },
        ],
        total_runs_24h=20,
    )

    assert any("signature provider-auth-required" in alert for alert in alerts)
    assert any("action=engineering-ticket" in alert for alert in alerts)
    assert not any("unknown-crash" in alert for alert in alerts)


def test_signature_alerts_cooldown_suppresses_assigned_owner_signature():
    audit = load_audit_module()
    signature = {
        "label": "nonzero-exit",
        "count": 6,
        "sample_task_ids": ["t_exit1"],
        "affected_profiles": ["qa"],
        "suggested_owner": "assignee-profile-owner",
    }

    alerts = audit.signature_alerts(
        [signature],
        total_runs_24h=30,
        prev_signatures=[signature],
    )

    assert alerts == []
