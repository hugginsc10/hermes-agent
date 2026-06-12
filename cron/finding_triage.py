from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from hermes_cli import kanban_db as kb
from hermes_constants import get_hermes_home

ACTIONABLE_AUDIT_SECTIONS = {"security", "new_issues"}
DEFAULT_BOARD = "agent-workspace-ops"
DEFAULT_ASSIGNEE = "inbox-triage"
DEFAULT_PRIORITY = 5
DEFAULT_MAX_FILED = 5
DEFAULT_DEDUP_DAYS = 14
GENERIC_ALL_INTERFACE_PORT_TITLE_RE = re.compile(
    r"^SECURITY: new all-interface listening port\(s\): (?P<ports>[\d, ]+)$"
)


@dataclass(frozen=True)
class Finding:
    source_job_id: str
    source_job_name: str
    source_run_date: str
    source_output_path: str
    severity: str
    title: str
    summary: str
    details: str
    finding_key: str
    section: str | None = None


@dataclass(frozen=True)
class FilingOutcome:
    finding: Finding
    status: str
    task_id: str | None = None


@dataclass(frozen=True)
class SourceRun:
    source_job_id: str
    source_job_name: str
    output_path: Path
    markdown: str
    run_date: str


def slugify(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"t_[0-9a-f]{8,}", "task", text)
    text = re.sub(r"\b\d+\b", "n", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "finding"


def normalize_title(text: str) -> str:
    text = text.casefold().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\b\d+\b", "n", text)
    return text


def build_finding_key(source_job_name: str, title: str) -> str:
    return f"cron-finding:{slugify(source_job_name)}:{slugify(title)}"


def build_listener_finding_key(source_job_name: str, listener_identities: Sequence[str]) -> str:
    identity_blob = "|".join(sorted(identity for identity in listener_identities if identity.strip()))
    return f"cron-finding:{slugify(source_job_name)}:listener:{slugify(identity_blob)}"


def extract_latest_source_run(source_job_id: str, source_job_name: str, output_root: Path) -> SourceRun | None:
    job_dir = output_root / source_job_id
    if not job_dir.exists():
        return None
    output_files = sorted(job_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not output_files:
        return None
    latest = output_files[0]
    markdown = latest.read_text(encoding="utf-8")
    run_date = _extract_run_date(markdown) or latest.stem.split("_", 1)[0]
    return SourceRun(
        source_job_id=source_job_id,
        source_job_name=source_job_name,
        output_path=latest,
        markdown=markdown,
        run_date=run_date,
    )


def _extract_run_date(markdown: str) -> str | None:
    match = re.search(r"\*\*Run Time:\*\*\s*(\d{4}-\d{2}-\d{2})", markdown)
    if match:
        return match.group(1)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", markdown)
    return match.group(1) if match else None


def parse_findings(run: SourceRun) -> list[Finding]:
    source = run.source_job_name
    if source == "daily-grok-redteam-review":
        return parse_grok_report(run)
    if source == "platform-self-audit":
        return parse_platform_self_audit(run)
    raise ValueError(f"unsupported source job: {source}")


def parse_grok_report(run: SourceRun) -> list[Finding]:
    body = run.markdown
    if "## Response" in body:
        body = body.split("## Response", 1)[1]
    if "[SILENT]" in body:
        return []
    pattern = re.compile(
        r"\*\*(CRITICAL|HIGH|MEDIUM)\*\*\s*\n"
        r"\*\*(.+?)\*\*\s*\n"
        r"(.*?)(?=\n\*\*(?:CRITICAL|HIGH|MEDIUM)\*\*\s*\n\*\*|\n\*\*Verdict:\*\*|\Z)",
        re.S,
    )
    findings: list[Finding] = []
    for severity, title, block in pattern.findall(body):
        clean_title = _clean_inline_markdown(title)
        details = block.strip()
        summary = _extract_prefixed_block(details, "Recommended fix") or _extract_prefixed_block(details, "Observed evidence") or details
        findings.append(
            Finding(
                source_job_id=run.source_job_id,
                source_job_name=run.source_job_name,
                source_run_date=run.run_date,
                source_output_path=str(run.output_path),
                severity=severity,
                title=clean_title,
                summary=summary.strip(),
                details=details,
                finding_key=build_finding_key(run.source_job_name, clean_title),
                section="response",
            )
        )
    return findings


def parse_platform_self_audit(run: SourceRun) -> list[Finding]:
    if "[SILENT]" in run.markdown:
        return []
    findings: list[Finding] = []
    listener_identities_by_port = _platform_self_audit_listener_identities_by_port(run)
    section: str | None = None
    for raw_line in run.markdown.splitlines():
        line = raw_line.strip()
        if line == ":rotating_light: *Security:*":
            section = "security"
            continue
        if line == "*New issues:*":
            section = "new_issues"
            continue
        if line == "*Resolved since last run:*":
            section = "resolved"
            continue
        if not line.startswith("•") and not line.startswith("- ") and not line.startswith("* ") and not line.startswith("_Full report:"):
            continue
        if section not in ACTIONABLE_AUDIT_SECTIONS:
            continue
        bullet = re.sub(r"^[•\-*]\s*", "", line).strip()
        severity = "HIGH" if section == "security" else "MEDIUM"
        finding_key = _platform_self_audit_finding_key(
            run.source_job_name,
            bullet,
            section=section,
            listener_identities_by_port=listener_identities_by_port,
        )
        findings.append(
            Finding(
                source_job_id=run.source_job_id,
                source_job_name=run.source_job_name,
                source_run_date=run.run_date,
                source_output_path=str(run.output_path),
                severity=severity,
                title=bullet,
                summary=bullet,
                details=bullet,
                finding_key=finding_key,
                section=section,
            )
        )
    return findings


def _platform_self_audit_finding_key(
    source_job_name: str,
    bullet: str,
    *,
    section: str | None,
    listener_identities_by_port: dict[str, list[str]],
) -> str:
    generic_match = GENERIC_ALL_INTERFACE_PORT_TITLE_RE.match(bullet)
    if section == "security" and generic_match:
        ports = [port.strip() for port in generic_match.group("ports").split(",") if port.strip()]
        identities = [
            identity
            for port in ports
            for identity in listener_identities_by_port.get(port, [])
            if identity.strip()
        ]
        if identities:
            return build_listener_finding_key(source_job_name, identities)
    return build_finding_key(source_job_name, bullet)


def _platform_self_audit_listener_identities_by_port(run: SourceRun) -> dict[str, list[str]]:
    listeners = _extract_platform_self_audit_listeners(run.markdown)
    if not listeners:
        report_markdown = _read_platform_self_audit_full_report(run.markdown)
        if report_markdown:
            listeners = _extract_platform_self_audit_listeners(report_markdown)
    identities_by_port: dict[str, list[str]] = {}
    for listener in listeners:
        port = str(listener.get("port") or "").strip()
        identity = _listener_identity_string(listener)
        if not port or not identity:
            continue
        identities_by_port.setdefault(port, [])
        if identity not in identities_by_port[port]:
            identities_by_port[port].append(identity)
    return identities_by_port


def _extract_platform_self_audit_listeners(markdown: str) -> list[dict]:
    match = re.search(r"^- all-interface listeners:\s*(.+)$", markdown, re.MULTILINE)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1).strip())
    except (SyntaxError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _obsidian_daily_recap_root() -> Path:
    return Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Daily Recap"


def _read_platform_self_audit_full_report(markdown: str) -> str | None:
    for raw_line in markdown.splitlines():
        line = raw_line.strip().strip("_")
        if not line.startswith("Full report:"):
            continue
        location = line.split(":", 1)[1].strip()
        if location.startswith("Obsidian → "):
            relative = location.split("Obsidian → ", 1)[1].strip()
            report_path = _obsidian_daily_recap_root() / relative
        else:
            report_path = Path(location)
        try:
            return report_path.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _listener_identity_string(listener: dict) -> str:
    protocol = str(listener.get("protocol") or "tcp").lower()
    executable = str(listener.get("executable") or listener.get("command") or "")
    launchd_label = str(listener.get("launchd_label") or "")
    authorities = " | ".join(str(value) for value in (listener.get("codesign_authorities") or []))
    identity = "|".join((protocol, executable, authorities, launchd_label)).strip("|")
    return identity


def _clean_inline_markdown(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("  ", " ")).strip().strip("*")


def _extract_prefixed_block(text: str, label: str) -> str | None:
    match = re.search(rf"\*{label}:\*\s*(.+?)(?=\n\*[^\n]+:\*|\Z)", text, re.S)
    if not match:
        return None
    value = match.group(1).strip()
    value = re.sub(r"^[*\-•]\s*", "", value)
    return value.strip()


def process_findings(
    conn,
    findings: Sequence[Finding],
    *,
    board: str = DEFAULT_BOARD,
    assignee: str = DEFAULT_ASSIGNEE,
    priority: int = DEFAULT_PRIORITY,
    max_filed: int = DEFAULT_MAX_FILED,
    dedup_days: int = DEFAULT_DEDUP_DAYS,
) -> list[FilingOutcome]:
    outcomes: list[FilingOutcome] = []
    filed_count = 0
    for finding in findings:
        duplicate_id = find_duplicate_task_id(conn, finding, dedup_days=dedup_days)
        if duplicate_id:
            outcomes.append(FilingOutcome(finding=finding, status="duplicate", task_id=duplicate_id))
            continue
        if filed_count >= max_filed:
            outcomes.append(FilingOutcome(finding=finding, status="cap"))
            continue
        task_id = create_task_for_finding(
            conn,
            finding,
            assignee=assignee,
            priority=priority,
            board=board,
        )
        outcomes.append(FilingOutcome(finding=finding, status="filed", task_id=task_id))
        filed_count += 1
    return outcomes


def find_duplicate_task_id(conn, finding: Finding, *, dedup_days: int) -> str | None:
    cutoff = int((datetime.now().astimezone() - timedelta(days=dedup_days)).timestamp())
    rows = conn.execute(
        """
        SELECT id, title, body, status, completed_at, idempotency_key
        FROM tasks
        WHERE status != 'archived'
           OR completed_at IS NULL
           OR completed_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()
    wanted_title = normalize_title(finding.title)
    for row in rows:
        row_id = row[0]
        title = row[1] or ""
        body = row[2] or ""
        idempotency_key = row[5] or ""
        if idempotency_key == finding.finding_key:
            return row_id
        if f"Finding key: {finding.finding_key}" in body:
            return row_id
        if _skip_title_based_dedup(finding):
            continue
        if normalize_title(title) == wanted_title:
            return row_id
    return None


def _skip_title_based_dedup(finding: Finding) -> bool:
    return ":listener:" in finding.finding_key and bool(GENERIC_ALL_INTERFACE_PORT_TITLE_RE.match(finding.title))


def create_task_for_finding(
    conn,
    finding: Finding,
    *,
    assignee: str,
    priority: int,
    board: str,
) -> str:
    body = format_task_body(finding)
    return kb.create_task(
        conn,
        title=finding.title,
        body=body,
        assignee=assignee,
        created_by="cron-finding-triage",
        priority=priority,
        triage=True,
        idempotency_key=finding.finding_key,
        board=board,
    )


def format_task_body(finding: Finding) -> str:
    parts = [
        "Auto-filed actionable cron finding.",
        "",
        f"Source job: {finding.source_job_name}",
        f"Source job id: {finding.source_job_id}",
        f"Source run date: {finding.source_run_date}",
        f"Source output: {finding.source_output_path}",
        f"Finding key: {finding.finding_key}",
        f"Severity: {finding.severity}",
        "",
        "Summary:",
        finding.summary.strip(),
    ]
    if finding.details.strip() and finding.details.strip() != finding.summary.strip():
        parts.extend(["", "Source details:", finding.details.strip()])
    return "\n".join(parts).strip() + "\n"


def render_report(run: SourceRun, outcomes: Sequence[FilingOutcome]) -> str:
    if run.source_job_name == "daily-grok-redteam-review":
        return render_grok_report(run, outcomes)
    if run.source_job_name == "platform-self-audit":
        return render_platform_self_audit(run, outcomes)
    raise ValueError(f"unsupported source job: {run.source_job_name}")


def render_grok_report(run: SourceRun, outcomes: Sequence[FilingOutcome]) -> str:
    if not outcomes:
        return "[SILENT]"
    lines = [f"*daily-grok-redteam-review — {run.run_date}*", ""]
    for outcome in outcomes:
        lines.append(f"*{outcome.finding.severity}* — {outcome.finding.title} — {_status_label(outcome)}")
        observed = _extract_prefixed_block(outcome.finding.details, "Observed evidence")
        if observed:
            lines.append(f"  Observed: {observed}")
        recommended = _extract_prefixed_block(outcome.finding.details, "Recommended fix")
        if recommended:
            lines.append(f"  Fix: {recommended}")
        inference = _extract_prefixed_block(outcome.finding.details, "Inference")
        if inference:
            lines.append(f"  Inference: {inference}")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_platform_self_audit(run: SourceRun, outcomes: Sequence[FilingOutcome]) -> str:
    if not outcomes:
        return "[SILENT]"
    grouped: dict[str, list[FilingOutcome]] = {"security": [], "new_issues": []}
    for outcome in outcomes:
        grouped.setdefault(outcome.finding.section or "new_issues", []).append(outcome)
    title_line = next(
        (
            line.strip()
            for line in run.markdown.splitlines()
            if line.strip().startswith("*Hermes platform self-audit")
        ),
        f"*Hermes platform self-audit — {run.run_date}*",
    )
    lines = [title_line]
    if grouped.get("security"):
        lines.extend([":rotating_light: *Security:*"])
        for outcome in grouped["security"]:
            lines.append(f"  • {outcome.finding.title} — {_status_label(outcome)}")
    if grouped.get("new_issues"):
        lines.append("*New issues:*")
        for outcome in grouped["new_issues"]:
            lines.append(f"  • {outcome.finding.title} — {_status_label(outcome)}")
    resolved_block = _extract_audit_block(run.markdown, "*Resolved since last run:*")
    if resolved_block:
        lines.append("*Resolved since last run:*")
        lines.extend([f"  • {entry}" for entry in resolved_block])
    full_report_line = next((line.strip() for line in run.markdown.splitlines() if line.strip().startswith("_Full report:")), None)
    if full_report_line:
        lines.append(full_report_line)
    return "\n".join(lines)


def _extract_audit_block(markdown: str, header: str) -> list[str]:
    lines = markdown.splitlines()
    collecting = False
    entries: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line == header:
            collecting = True
            continue
        if collecting and line.startswith("*") and not line.startswith("* ") and line != header:
            break
        if collecting and line.startswith("•"):
            entries.append(re.sub(r"^[•\-*]\s*", "", line).strip())
    return entries


def _first_nonempty(lines: Iterable[str]) -> str | None:
    for line in lines:
        if line.strip():
            return line.strip()
    return None


def _status_label(outcome: FilingOutcome) -> str:
    if outcome.status == "filed" and outcome.task_id:
        return f"filed as {outcome.task_id}"
    if outcome.status == "duplicate" and outcome.task_id:
        return f"duplicate of {outcome.task_id}"
    return "not filed (cap)"


def run_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Triages actionable cron findings into kanban.")
    parser.add_argument("--source-job-id", required=True)
    parser.add_argument("--source-job-name", required=True)
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--assignee", default=DEFAULT_ASSIGNEE)
    parser.add_argument("--priority", type=int, default=DEFAULT_PRIORITY)
    parser.add_argument("--max-filed", type=int, default=DEFAULT_MAX_FILED)
    parser.add_argument("--dedup-days", type=int, default=DEFAULT_DEDUP_DAYS)
    parser.add_argument("--output-root", default=None)
    args = parser.parse_args(argv)

    output_root = Path(args.output_root) if args.output_root else get_hermes_home() / "cron" / "output"
    run = extract_latest_source_run(args.source_job_id, args.source_job_name, output_root)
    if run is None:
        print("[SILENT]")
        return 0
    findings = parse_findings(run)
    if not findings:
        print("[SILENT]")
        return 0
    with kb.connect(board=args.board) as conn:
        outcomes = process_findings(
            conn,
            findings,
            board=args.board,
            assignee=args.assignee,
            priority=args.priority,
            max_filed=args.max_filed,
            dedup_days=args.dedup_days,
        )
    print(render_report(run, outcomes))
    return 0


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
