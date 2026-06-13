from __future__ import annotations

from pathlib import Path

import pytest

from cron.finding_triage import (
    SourceRun,
    build_finding_key,
    parse_findings,
    process_findings,
    render_report,
)
from hermes_cli import kanban_db as kb


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_process_findings_dedups_recent_done_and_caps_new_tasks(hermes_home):
    run = SourceRun(
        source_job_id="4d4f48cc5ac2",
        source_job_name="daily-grok-redteam-review",
        output_path=hermes_home / "cron" / "output" / "4d4f48cc5ac2" / "2026-06-10_17-06-11.md",
        run_date="2026-06-10",
        markdown=(
            "## Response\n\n"
            "**HIGH**\n"
            "**Persistent non-atomic FS write in kanban notifier.**\n"
            "*Observed evidence:* `file:1`\n"
            "*Recommended fix:* make writes atomic\n\n"
            "**MEDIUM**\n"
            "**Credit pause suppression risks silent quota degradation.**\n"
            "*Observed evidence:* `file:2`\n"
            "*Recommended fix:* tighten the free-tier gate\n\n"
            "**MEDIUM**\n"
            "**Third finding.**\n"
            "*Observed evidence:* `file:3`\n"
            "*Recommended fix:* fix three\n\n"
            "**MEDIUM**\n"
            "**Fourth finding.**\n"
            "*Observed evidence:* `file:4`\n"
            "*Recommended fix:* fix four\n"
        ),
    )
    findings = parse_findings(run)

    with kb.connect() as conn:
        duplicate_id = kb.create_task(
            conn,
            title=findings[0].title,
            body="manually created duplicate",
            assignee="inbox-triage",
        )
        done_id = kb.create_task(
            conn,
            title=findings[1].title,
            body=f"Finding key: {findings[1].finding_key}\n",
            assignee="inbox-triage",
        )
        kb.complete_task(conn, done_id, summary="done")

        outcomes = process_findings(
            conn,
            findings,
            board="default",
            assignee="inbox-triage",
            priority=5,
            max_filed=1,
            dedup_days=14,
        )

        tasks = kb.list_tasks(conn, include_archived=True)

    assert [outcome.status for outcome in outcomes] == ["duplicate", "duplicate", "filed", "cap"]
    assert outcomes[0].task_id == duplicate_id
    assert outcomes[1].task_id == done_id
    assert outcomes[2].task_id is not None
    assert outcomes[3].task_id is None

    created = {task.id: task for task in tasks}
    filed_task = created[outcomes[2].task_id]
    assert filed_task.priority == 5
    assert filed_task.assignee == "inbox-triage"
    assert filed_task.status == "triage"


def test_render_report_marks_cap_and_duplicates_for_grok():
    run = SourceRun(
        source_job_id="4d4f48cc5ac2",
        source_job_name="daily-grok-redteam-review",
        output_path=Path("/tmp/report.md"),
        run_date="2026-06-10",
        markdown=(
            "## Response\n\n"
            "**HIGH**\n"
            "**Duplicate finding.**\n"
            "*Observed evidence:* `a`\n"
            "*Recommended fix:* fix a\n\n"
            "**MEDIUM**\n"
            "**Overflow finding.**\n"
            "*Observed evidence:* `b`\n"
            "*Recommended fix:* fix b\n"
        ),
    )
    findings = parse_findings(run)
    outcomes = [
        type("Outcome", (), {"finding": findings[0], "status": "duplicate", "task_id": "t_dup"})(),
        type("Outcome", (), {"finding": findings[1], "status": "cap", "task_id": None})(),
    ]

    rendered = render_report(run, outcomes)

    assert "duplicate of t_dup" in rendered
    assert "not filed (cap)" in rendered
    assert "Observed:" in rendered
    assert "Fix:" in rendered


def test_parse_platform_self_audit_only_keeps_actionable_sections(tmp_path):
    run = SourceRun(
        source_job_id="05be82ffe21e",
        source_job_name="platform-self-audit",
        output_path=tmp_path / "audit.md",
        run_date="2026-06-11",
        markdown=(
            "*Hermes platform self-audit — 2026-06-11 02:30 EDT*\n"
            ":rotating_light: *Security:*\n"
            "  • SECURITY: new all-interface listening port(s): 49643\n"
            "*New issues:*\n"
            "  • memory buffer rejecting writes (11 hits in recent log) — consolidate user profile\n"
            "*Resolved since last run:*\n"
            "  • old issue resolved\n"
            "_Full report: somewhere_\n"
        ),
    )

    findings = parse_findings(run)

    assert [f.section for f in findings] == ["security", "new_issues"]
    assert findings[0].severity == "HIGH"
    assert findings[1].severity == "MEDIUM"
    assert findings[0].finding_key == build_finding_key(run.source_job_name, findings[0].title)


def test_platform_self_audit_listener_identity_dedups_port_churn_but_not_new_identity(hermes_home):
    report_dir = (
        Path.home()
        / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Daily Recap/Hermes/Self-Audit"
    )
    report_dir.mkdir(parents=True)

    def make_run(*, run_date: str, report_name: str, port: str, executable: str, label: str) -> SourceRun:
        report_path = report_dir / report_name
        listener_blob = repr(
            [
                {
                    "protocol": "tcp",
                    "port": port,
                    "executable": executable,
                    "codesign_authorities": ["Apple Root CA"],
                    "launchd_label": label,
                }
            ]
        )
        report_path.write_text(
            (
                f"# Hermes Platform Self-Audit — {run_date}\n\n"
                "## Raw signals\n"
                f"- all-interface listeners: {listener_blob}\n"
            ),
            encoding="utf-8",
        )
        return SourceRun(
            source_job_id="05be82ffe21e",
            source_job_name="platform-self-audit",
            output_path=hermes_home / "cron" / "output" / "05be82ffe21e" / f"{run_date}_02-30-06.md",
            run_date=run_date,
            markdown=(
                f"*Hermes platform self-audit — {run_date} 02:30 EDT*\n"
                ":rotating_light: *Security:*\n"
                f"  • SECURITY: new all-interface listening port(s): {port}\n"
                f"_Full report: Obsidian → Hermes/Self-Audit/{report_name}_\n"
            ),
        )

    run_one = make_run(
        run_date="2026-06-10",
        report_name="2026-06-10 - Platform Self-Audit.md",
        port="49152",
        executable="/usr/libexec/rapportd",
        label="com.apple.rapportd",
    )
    run_two = make_run(
        run_date="2026-06-11",
        report_name="2026-06-11 - Platform Self-Audit.md",
        port="49643",
        executable="/usr/libexec/rapportd",
        label="com.apple.rapportd",
    )
    run_three = make_run(
        run_date="2026-06-12",
        report_name="2026-06-12 - Platform Self-Audit.md",
        port="51000",
        executable="/opt/custom/bin/daemon",
        label="com.example.daemon",
    )

    findings_one = parse_findings(run_one)
    findings_two = parse_findings(run_two)
    findings_three = parse_findings(run_three)

    assert findings_one[0].finding_key == findings_two[0].finding_key
    assert findings_three[0].finding_key != findings_one[0].finding_key

    with kb.connect() as conn:
        first_outcome = process_findings(conn, findings_one, board="default", assignee="inbox-triage")
        second_outcome = process_findings(conn, findings_two, board="default", assignee="inbox-triage")
        third_outcome = process_findings(conn, findings_three, board="default", assignee="inbox-triage")

    assert first_outcome[0].status == "filed"
    assert second_outcome[0].status == "duplicate"
    assert second_outcome[0].task_id == first_outcome[0].task_id
    assert third_outcome[0].status == "filed"
