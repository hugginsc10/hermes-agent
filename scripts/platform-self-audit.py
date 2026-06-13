#!/usr/bin/env python3
"""Nightly Hermes platform self-audit (no-agent cron).

Always writes a dated report to Obsidian + a state snapshot. Performs known-safe
auto-remediation (caps oversized logs). Prints a concise summary to stdout ONLY
when something changed vs the last run, a hard threshold is breached, or a
remediation ran — so the cron stays silent on clean, unchanged nights.

Best-effort feeds significant findings into Hindsight (the shared brain).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

def resolve_operator_home(path_home: Path | None = None, script_path: Path | None = None) -> Path:
    """Return the operator home, not a Hermes profile sandbox home.

    Kanban/Hermes worker environments can set HOME to
    ``~/.hermes/profiles/<profile>/home``. This audit is deployed under the
    operator's ``~/.hermes/scripts`` tree and must read the operator-wide
    kanban DB/state, otherwise full live runs can silently inspect an empty
    profile sandbox and report zero/null loop telemetry.
    """
    home = (path_home or Path.home()).resolve()
    script = (script_path or Path(__file__)).resolve()

    if script.parent.name == "scripts" and script.parent.parent.name == ".hermes":
        return script.parent.parent.parent

    parts = home.parts
    if ".hermes" in parts:
        idx = parts.index(".hermes")
        if len(parts) > idx + 2 and parts[idx + 1] == "profiles":
            return Path(*parts[:idx]) if idx else Path("/")
    return home


HOME = resolve_operator_home()
HERMES = HOME / ".hermes"
LOGS = HERMES / "logs"
STATE_PATH = HERMES / "state" / "platform-self-audit.json"
KANBAN_DB = HERMES / "kanban/boards/agent-workspace-ops/kanban.db"
VAULT = HOME / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Daily Recap"
REPORT_DIR = VAULT / "Hermes" / "Self-Audit"
HINDSIGHT_MCP = "http://127.0.0.1:8888/mcp/hermes-swarm-shared/"
TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
SOCKETFW = "/usr/libexec/ApplicationFirewall/socketfilterfw"

# Thresholds
LOG_ALERT_MB = 100          # report a log this big
LOG_CAP_MB = 50             # auto-cap (copy-truncate) logs over this size
LOG_CAP_KEEP_LINES = 5000
COMMITS_BEHIND_ALERT = 150
DISK_FREE_ALERT_GB = 15
# Loop-telemetry alert thresholds (2026-06-11 reliability overhaul
# baseline for reference: crash rate 33.4% 7d, controller share 18.9%/day,
# runs-per-completion 2.04, blocked resolution 11/64).
LOOP_CRASH_RATE_ALERT = 0.30        # 7d crash share of runs
LOOP_CONTROLLER_SHARE_ALERT = 0.15  # 24h drain-controller share of creates
LOOP_CONTROLLER_CONTEXT_SECONDS = 3600  # recent-create context for 24h share alerts
LOOP_RUNS_PER_COMPLETION_ALERT = 2.5
# Failure-share telemetry (2026-06-12 worker-reliability plan): crash_rate
# alone undercounts failure (timed_out/gave_up/spawn_failed are invisible),
# and the rolling 7d window kept reporting 28-30% for days after the
# overhaul because pre-overhaul storm days dominated it while reality was
# 14-18% and falling. Report a fixed-baseline window alongside the rolling
# ones so post-overhaul progress is visible the night it happens.
LOOP_OVERHAUL_BASELINE_ISO = "2026-06-11T00:00:00+00:00"
LOOP_FAILURE_OUTCOMES = ("crashed", "timed_out", "gave_up", "spawn_failed")
SIGNATURE_VOLUME_MIN_CRASHES = 5
SIGNATURE_VOLUME_MIN_SHARE = 0.10
UNKNOWN_CRASH_PAGE_MIN = 2
DETERMINISTIC_SIGNATURE_DOWNGRADE_SHARE = 0.80
CRASH_SIGNATURE_OWNERS = {
    "provider-auth-required": "platform-auth-owner",
    "unknown-skill/config-error": "skills/config-owner",
    "cwd-missing": "dispatcher/workspace-owner",
    "nonzero-exit": "assignee-profile-owner",
    "protocol-violation": "agent-protocol-owner",
    "stale-pid": "dispatcher-liveness-owner",
    "unknown-crash": "platform-reliability-owner",
}
DETERMINISTIC_CRASH_SIGNATURES = {
    "provider-auth-required",
    "unknown-skill/config-error",
    "cwd-missing",
    "nonzero-exit",
    "protocol-violation",
    "stale-pid",
}
# Loop-yield origin classification (2026-06-12). Small, stable
# operator-side sets; any OTHER non-empty creator is an agent/machine
# source and counts as autonomous, so new agent names need no edit here.
LOOP_HISTORY_PATH = HERMES / "state" / "loop-telemetry-history.jsonl"
LOOP_TREND_WINDOW = 14              # audit runs considered for the trend
OPERATOR_CREATORS = {"dashboard", "user", "operator", "workspace-operator"}
OPERATOR_CREATOR_PREFIXES = ("operator-", "chas")
DELEGATE_CREATORS = {"claude-mcp"}  # Claude filing on the operator's behalf
# Chat-surface relays: operator-initiated content, agent hands.
DELEGATE_CREATOR_MARKERS = ("slack", "telegram")
# Placeholder creators carry no origin signal.
UNATTRIBUTED_CREATORS = {"(none)", "default"}
NOW = datetime.now().astimezone()


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001 - best-effort probe
        return 1, str(exc)


# ---------------------------------------------------------------- checks
def check_health() -> list[str]:
    """Read the authoritative health snapshot the 5-min monitor maintains."""
    issues: list[str] = []
    f = HERMES / "state" / "platform-health.json"
    try:
        data = json.loads(f.read_text())
    except Exception:
        return ["health: platform-health.json unreadable"]
    for name, svc in (data.get("services") or {}).items():
        required = str(svc.get("required", "1")) != "0"
        http = svc.get("http")
        optional = http == "optional" or svc.get("reason") == "optional_not_running"
        if optional or not required:
            continue
        if svc.get("listener") != "up" or (http not in ("up", None) and http != "optional"):
            issues.append(f"service:{name} degraded ({svc.get('reason', '?')})")
    ts = (data.get("tailscale") or {}).get("status")
    if ts and ts != "up":
        issues.append(f"tailscale {ts}")
    return issues


def check_crons() -> dict:
    f = HERMES / "cron" / "jobs.json"
    failing: list[str] = []
    paused: list[str] = []
    try:
        d = json.loads(f.read_text())
        jobs = d if isinstance(d, list) else d.get("jobs", list(d.values()) if isinstance(d, dict) else [])
    except Exception:
        return {"failing": [], "paused": [], "error": "jobs.json unreadable"}
    for j in jobs:
        name = j.get("name", j.get("id", "?"))
        if j.get("enabled") and j.get("last_status") == "error":
            failing.append(name)
        if j.get("state") == "paused" or j.get("enabled") is False:
            paused.append(name)
    return {"failing": sorted(failing), "paused": sorted(paused)}


def scan_logs() -> tuple[list[str], list[str]]:
    """Return (alerts, remediations). Auto-cap oversized logs in place."""
    alerts: list[str] = []
    remediations: list[str] = []
    for log in sorted(LOGS.glob("*.log")):
        try:
            mb = log.stat().st_size / (1024 * 1024)
        except Exception:
            continue
        if mb >= LOG_CAP_MB:
            try:
                with log.open("r", errors="replace") as fh:
                    tail = fh.readlines()[-LOG_CAP_KEEP_LINES:]
                with log.open("w") as fh:  # copy-truncate: same inode, writer continues
                    fh.writelines(tail)
                remediations.append(f"capped {log.name} ({mb:.0f}MB -> last {LOG_CAP_KEEP_LINES} lines)")
            except Exception as exc:  # noqa: BLE001
                alerts.append(f"log {log.name} is {mb:.0f}MB and could not be capped: {exc}")
        elif mb >= LOG_ALERT_MB:
            alerts.append(f"log {log.name} is {mb:.0f}MB")
    return alerts, remediations


def check_upstream() -> list[str]:
    code, out = run(["git", "-C", str(HERMES / "hermes-agent"), "rev-list", "--count", "HEAD..@{u}"])
    if code != 0:
        return []
    try:
        n = int(out.strip())
    except ValueError:
        return []
    if n >= COMMITS_BEHIND_ALERT:
        return [f"hermes-agent is {n} commits behind upstream"]
    return []


def check_memory_pressure() -> list[str]:
    """Count recent 'memory buffer full' rejections in the gateway log."""
    cutoff = NOW - timedelta(hours=24)
    hits = 0
    for name in ("gateway.log", "errors.log"):
        f = LOGS / name
        if not f.exists():
            continue
        try:
            with f.open(errors="replace") as fh:
                for line in fh.readlines()[-4000:]:
                    if "Memory at" in line and "would exceed" in line:
                        hits += 1
        except Exception:
            continue
    if hits:
        return [f"memory buffer rejecting writes ({hits} hits in recent log) — consolidate user profile"]
    return []


def parse_codesign_authorities(output: str) -> list[str]:
    return [ln.split("=", 1)[1].strip() for ln in output.splitlines() if ln.startswith("Authority=")]



def is_apple_signed(authorities: list[str]) -> bool:
    joined = " | ".join(authorities).lower()
    return "apple" in joined



def get_executable_path(pid: str) -> str | None:
    code, out = run(["ps", "-p", pid, "-o", "command="], timeout=10)
    if code != 0:
        return None
    command = out.strip()
    if not command:
        return None
    return command.split()[0]



def get_launchd_label(pid: str) -> str | None:
    code, out = run(["launchctl", "procinfo", pid], timeout=10)
    if code != 0:
        return None
    for line in out.splitlines():
        if "label =" in line:
            return line.split("label =", 1)[1].strip()
    return None



def build_listener_entry(command: str, pid: str, protocol: str, port: str) -> dict:
    entry = {
        "command": command,
        "pid": pid,
        "protocol": protocol.lower(),
        "port": port,
        "executable": None,
        "codesign_authorities": [],
        "apple_signed": False,
        "launchd_label": None,
    }
    executable = get_executable_path(pid)
    if executable:
        entry["executable"] = executable
        if executable.startswith("/") and Path(executable).exists():
            _, cs_out = run(["codesign", "-dv", "--verbose=4", executable], timeout=15)
            authorities = parse_codesign_authorities(cs_out)
            entry["codesign_authorities"] = authorities
            entry["apple_signed"] = is_apple_signed(authorities)
    launchd_label = get_launchd_label(pid)
    if launchd_label:
        entry["launchd_label"] = launchd_label
    return entry



def expected_apple_continuity_listener(listener: dict) -> bool:
    executable = listener.get("executable")
    port = str(listener.get("port", ""))
    return (
        listener.get("protocol") == "tcp"
        and executable == "/usr/libexec/rapportd"
        and listener.get("apple_signed") is True
        and port.isdigit()
        and int(port) >= 49152
    )



def listener_identity_key(listener: dict) -> tuple[str, str, str, str]:
    authorities = tuple(listener.get("codesign_authorities") or [])
    return (
        str(listener.get("protocol") or "").lower(),
        str(listener.get("executable") or listener.get("command") or ""),
        " | ".join(authorities),
        str(listener.get("launchd_label") or ""),
    )



def describe_listener(listener: dict) -> str:
    executable = listener.get("executable") or listener.get("command") or "unknown"
    protocol = str(listener.get("protocol") or "tcp").upper()
    port = listener.get("port") or "?"
    label = listener.get("launchd_label")
    if label:
        return f"{executable} ({label}) {protocol}/{port}"
    return f"{executable} {protocol}/{port}"



def compute_listener_drift(prev_security: dict, current_security: dict) -> tuple[list[str], list[str]]:
    prev_binds = {str(p) for p in prev_security.get("all_iface_ports", [])}
    prev_listeners = prev_security.get("all_iface_listeners") or []
    cur_listeners = current_security.get("all_iface_listeners") or []

    prev_by_key: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for listener in prev_listeners:
        prev_by_key[listener_identity_key(listener)].append(listener)

    alerts: list[str] = []
    notes: list[str] = []
    for listener in cur_listeners:
        port = str(listener.get("port") or "")
        key = listener_identity_key(listener)
        prev_matches = prev_by_key.get(key, [])
        had_same_port = any(str(prev.get("port")) == port for prev in prev_matches)
        if had_same_port:
            continue
        if not prev_listeners and port in prev_binds:
            continue
        if expected_apple_continuity_listener(listener):
            prior_ports = sorted({str(prev.get("port")) for prev in prev_matches if prev.get("port")}, key=int)
            if prior_ports:
                notes.append(
                    f"INFO: expected Apple Continuity listener churned port for /usr/libexec/rapportd ({', '.join(prior_ports)} -> {port})"
                )
            else:
                notes.append(
                    f"INFO: expected Apple Continuity listener present: /usr/libexec/rapportd TCP/{port}"
                )
            continue
        if prev_matches:
            prior_ports = sorted({str(prev.get("port")) for prev in prev_matches if prev.get("port")}, key=int)
            notes.append(
                f"INFO: stable all-interface listener changed port for {describe_listener(listener)} (previous: {', '.join(prior_ports)})"
            )
            continue
        if prev_listeners:
            alerts.append(f"SECURITY: new all-interface listener identity: {describe_listener(listener)}")
            continue
        alerts.append(f"SECURITY: new all-interface listening port: {port}")
    return alerts, notes



def check_security() -> tuple[list[str], dict, list[str]]:
    """Return (drift_alerts, snapshot, informational_notes). Snapshot is compared next run."""
    alerts: list[str] = []
    notes: list[str] = []
    snap: dict = {}

    code, out = run([SOCKETFW, "--getglobalstate"])
    fw_on = "enabled" in out.lower()
    snap["firewall"] = fw_on
    if not fw_on:
        alerts.append("SECURITY: application firewall is DISABLED")

    if Path(TAILSCALE).exists():
        _, fout = run([TAILSCALE, "funnel", "status"], timeout=15)
        # Funnel exposes to public internet when an entry is NOT marked "tailnet only".
        public = any(
            ("https://" in ln or "tcp://" in ln) and "tailnet only" not in ln and "-->" not in ln and "|--" not in ln
            for ln in fout.splitlines()
        )
        # The header line carries the (tailnet only) marker; treat explicit "Funnel on" as exposure.
        public = public or bool(re.search(r"funnel on|\(funnel\)", fout, re.I))
        snap["funnel_public"] = public
        if public:
            alerts.append("SECURITY: Tailscale Funnel appears to expose a service to the PUBLIC internet")

    # All-interface (0.0.0.0 / *) listening sockets — track stable identity alongside port.
    _, lout = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=15)
    binds = set()
    listeners = []
    for ln in lout.splitlines():
        m = re.search(r"^(\S+)\s+(\d+)\s+.+?\sTCP\s+(\*|0\.0\.0\.0):(\d+)\s+\(LISTEN\)", ln)
        if m:
            command, pid, _host, port = m.groups()
            binds.add(port)
            listeners.append(build_listener_entry(command, pid, "tcp", port))
    snap["all_iface_ports"] = sorted(binds, key=int)
    snap["all_iface_listeners"] = sorted(
        listeners,
        key=lambda item: (
            str(item.get("protocol")),
            int(item.get("port") or 0),
            str(item.get("executable") or item.get("command") or ""),
        ),
    )

    # Sensitive file permissions
    perms = {}
    for rel, want in ((".env", "600"), ("config.yaml", "600"), ("auth", "700")):
        p = HERMES / rel
        try:
            mode = oct(p.stat().st_mode)[-3:]
            perms[rel] = mode
            if mode != want:
                alerts.append(f"SECURITY: {rel} perms are {mode}, expected {want}")
        except Exception:
            pass
    snap["perms"] = perms
    return alerts, snap, notes


def check_disk() -> list[str]:
    code, out = run(["df", "-g", "/"])
    if code != 0:
        return []
    try:
        avail = int(out.splitlines()[1].split()[3])
    except Exception:
        return []
    if avail < DISK_FREE_ALERT_GB:
        return [f"disk: only {avail}GB free on /"]
    return []


# ---------------------------------------------------------------- hindsight
def classify_creator(created_by: str | None) -> str:
    """Bucket a tasks.created_by value by origin.

    'autonomous' = the loop discovered/filed it (any agent or machine
    source); 'operator' = a human filed it; 'delegate' = Claude filed it
    on the operator's behalf in a session (operator intent, agent hands);
    'unattributed' = no creator recorded.
    """
    value = (created_by or "").strip()
    lowered = value.lower()
    if not value or lowered in UNATTRIBUTED_CREATORS:
        return "unattributed"
    if lowered in OPERATOR_CREATORS or lowered.startswith(OPERATOR_CREATOR_PREFIXES):
        return "operator"
    if lowered in DELEGATE_CREATORS or any(m in lowered for m in DELEGATE_CREATOR_MARKERS):
        return "delegate"
    return "autonomous"


def compute_loop_yield(con, cutoff: int) -> dict:
    """Loop yield: who originated the work the loop COMPLETED since
    ``cutoff`` (and, secondarily, the work CREATED since then).

    The headline number is autonomous_share_completed — the fraction of
    finished work the platform discovered and filed for itself.
    """
    out: dict = {}
    for label, sql in (
        ("completed", "select coalesce(created_by,'') v, count(*) n from tasks "
                      "where status='done' and completed_at >= ? group by 1"),
        ("created", "select coalesce(created_by,'') v, count(*) n from tasks "
                    "where created_at >= ? group by 1"),
    ):
        buckets = {"autonomous": 0, "operator": 0, "delegate": 0, "unattributed": 0}
        total = 0
        for row in con.execute(sql, (cutoff,)):
            buckets[classify_creator(row["v"])] += row["n"]
            total += row["n"]
        out[f"{label}_total"] = total
        for bucket, n in buckets.items():
            out[f"{label}_{bucket}"] = n
        out[f"autonomous_share_{label}"] = round(buckets["autonomous"] / total, 3) if total else None
    return out


def update_yield_history(metrics: dict, path: Path | None = None) -> dict:
    """Append tonight's headline loop metrics to the JSONL history and
    return the multi-run trend over the last LOOP_TREND_WINDOW entries.

    The state snapshot only ever holds ONE previous run, so before this
    history existed the audit could report deltas but never a trend.
    Append-only, one line per audit run; failures degrade to an 'error'
    key rather than aborting the audit.
    """
    if path is None:
        path = LOOP_HISTORY_PATH
    entry = {
        "ts": NOW.isoformat(),
        "autonomous_share_completed": (metrics.get("loop_yield_7d") or {}).get("autonomous_share_completed"),
        "autonomous_share_created": (metrics.get("loop_yield_7d") or {}).get("autonomous_share_created"),
        "crash_rate_7d": metrics.get("crash_rate_7d"),
        "crash_rate_since_overhaul": metrics.get("crash_rate_since_overhaul"),
        "failure_share_7d": metrics.get("failure_share_7d"),
        "failure_share_since_overhaul": metrics.get("failure_share_since_overhaul"),
        "controller_share_24h": metrics.get("controller_share_24h"),
        "runs_per_completion_7d": metrics.get("runs_per_completion_7d"),
        "blocked_resolution_avg_hours": metrics.get("blocked_resolution_avg_hours"),
    }
    trend: dict = {}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        lines = path.read_text(encoding="utf-8").splitlines()[-LOOP_TREND_WINDOW:]
        series: dict[str, list] = {}
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            for key in ("autonomous_share_completed", "crash_rate_7d",
                        "crash_rate_since_overhaul", "failure_share_7d",
                        "controller_share_24h", "runs_per_completion_7d"):
                if isinstance(row.get(key), (int, float)):
                    series.setdefault(key, []).append(row[key])
        for key, values in series.items():
            trend[key] = {
                "series": values,
                "delta": round(values[-1] - values[0], 3) if len(values) >= 2 else None,
                "runs": len(values),
            }
    except Exception as exc:  # noqa: BLE001 - history must never kill the audit
        trend["error"] = str(exc)[:200]
    return trend



def classify_crash_signature(error: str | None, error_detail: str | None = None) -> str:
    """Return the actionable crash bucket for a task_run failure."""
    haystack = f"{error_detail or ''}\n{error or ''}".lower()
    if any(token in haystack for token in (
        "provider authentication", "auth required", "authentication required",
        "missing credential", "no api key", "api key", "oauth", "login required",
    )):
        return "provider-auth-required"
    if any(token in haystack for token in (
        "unknown skill", "missing skill", "skill not found", "setup_needed",
        "config error", "configuration", "config.yaml", "invalid config",
    )):
        return "unknown-skill/config-error"
    if any(token in haystack for token in (
        "cwd missing", "working directory", "no such file or directory", "chdir",
    )):
        return "cwd-missing"
    if "not alive" in haystack or "stale_lock" in haystack or "stale pid" in haystack:
        return "stale-pid"
    if any(token in haystack for token in (
        "exited code", "exit code", "non-zero", "nonzero", "return code",
    )):
        return "nonzero-exit"
    return "unknown-crash"


def _task_run_columns(con) -> set[str]:
    try:
        return {row["name"] for row in con.execute("PRAGMA table_info(task_runs)")}
    except Exception:
        return set()


def _signature_payload(label: str, rows: list[dict]) -> dict:
    task_ids = []
    profiles = []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        profile = str(row.get("profile") or "")
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
        if profile and profile not in profiles:
            profiles.append(profile)
    return {
        "label": label,
        "count": len(rows),
        "sample_task_ids": task_ids[:3],
        "affected_profiles": sorted(profiles),
        "suggested_owner": CRASH_SIGNATURE_OWNERS.get(label, "platform-reliability-owner"),
    }


def summarize_crash_signatures(con, cutoff: int) -> list[dict]:
    """Return grouped 24h crash signatures with samples and owner hints."""
    cols = _task_run_columns(con)
    detail_expr = "error_detail" if "error_detail" in cols else "NULL"
    start_expr = "log_offset_start" if "log_offset_start" in cols else "NULL"
    end_expr = "log_offset_end" if "log_offset_end" in cols else "NULL"
    rows = con.execute(
        f"""
        SELECT task_id, profile, error, {detail_expr} AS error_detail,
               {start_expr} AS log_offset_start, {end_expr} AS log_offset_end
        FROM task_runs
        WHERE started_at >= ? AND outcome = 'crashed'
        ORDER BY started_at, id
        """,
        (cutoff,),
    ).fetchall()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        as_dict = {key: row[key] for key in row.keys()}
        grouped[classify_crash_signature(as_dict.get("error"), as_dict.get("error_detail"))].append(as_dict)

    # Protocol violations are not always represented as crashed task_runs;
    # include them as a first-class signature so protocol regressions page on
    # their own instead of being hidden under aggregate counters.
    for row in con.execute(
        """
        SELECT e.task_id, t.assignee AS profile, e.payload AS error, e.payload AS error_detail
        FROM task_events e
        LEFT JOIN tasks t ON t.id = e.task_id
        WHERE e.created_at >= ? AND e.kind = 'protocol_violation'
        ORDER BY e.created_at, e.id
        """,
        (cutoff,),
    ).fetchall():
        grouped["protocol-violation"].append({key: row[key] for key in row.keys()})

    return [
        _signature_payload(label, rows)
        for label, rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def signature_alerts(
    signatures: list[dict],
    total_runs_24h: int,
    prev_signatures: list[dict] | None = None,
) -> list[str]:
    alerts: list[str] = []
    cooldown_keys = {
        (str(sig.get("label") or ""), str(sig.get("suggested_owner") or ""))
        for sig in (prev_signatures or [])
        if sig.get("label") and sig.get("suggested_owner")
    }
    total_crashes = sum(int(sig.get("count") or 0) for sig in signatures if sig.get("label") != "protocol-violation")
    deterministic = [sig for sig in signatures if sig.get("label") in DETERMINISTIC_CRASH_SIGNATURES]
    top_known = max((int(sig.get("count") or 0) for sig in deterministic), default=0)
    downgrade_known = bool(total_crashes and top_known / total_crashes >= DETERMINISTIC_SIGNATURE_DOWNGRADE_SHARE)

    for sig in signatures:
        label = str(sig.get("label") or "unknown-crash")
        count = int(sig.get("count") or 0)
        share = (count / total_runs_24h) if total_runs_24h else 0.0
        owner = sig.get("suggested_owner") or CRASH_SIGNATURE_OWNERS.get(label, "platform-reliability-owner")
        samples = ",".join(sig.get("sample_task_ids") or []) or "none"
        profiles = ",".join(sig.get("affected_profiles") or []) or "none"
        if (label, str(owner)) in cooldown_keys:
            continue
        if label == "unknown-crash":
            if count > UNKNOWN_CRASH_PAGE_MIN:
                alerts.append(
                    f"loop: unknown-crash {count}/24h > {UNKNOWN_CRASH_PAGE_MIN}; "
                    f"samples={samples}; profiles={profiles}; owner={owner}; action=page"
                )
            continue
        if count >= SIGNATURE_VOLUME_MIN_CRASHES or share >= SIGNATURE_VOLUME_MIN_SHARE:
            action = "engineering-ticket" if label in DETERMINISTIC_CRASH_SIGNATURES and downgrade_known else "page"
            alerts.append(
                f"loop: signature {label} {count}/24h ({share:.0%} of runs) triggered; "
                f"samples={samples}; profiles={profiles}; owner={owner}; action={action}"
            )
    return alerts

def check_loop_telemetry(prev_metrics: dict) -> tuple[dict, list[str]]:
    """Autonomous-loop health over the agent-workspace-ops kanban DB.

    Added by the 2026-06-11 reliability overhaul: the nightly audit
    previously computed zero loop telemetry, so the crash storm (26.7%
    of all runs), the Guide-title duplicate flood (181 stacked titles)
    and the controller mill (18 strategist cards/day, ~23% of volume)
    were invisible to it. Read-only sqlite; every metric records its own
    failure rather than aborting the audit.

    Returns ``(metrics, alerts)``. Metrics land in the state snapshot so
    the next run reports deltas.
    """
    import sqlite3

    metrics: dict = {}
    alerts: list[str] = []
    if not KANBAN_DB.exists():
        return {"error": "kanban db missing"}, []
    try:
        # Use a normal sqlite path plus query_only rather than URI mode=ro.
        # On the live macOS board DB, URI read-only opens could intermittently
        # produce all-zero aggregate telemetry while direct path reads saw the
        # populated WAL-backed database. query_only preserves the read-only
        # contract without that URI/WAL failure mode.
        con = sqlite3.connect(str(KANBAN_DB), timeout=10)
        con.execute("PRAGMA query_only=ON")
        con.row_factory = sqlite3.Row
    except Exception as exc:  # noqa: BLE001
        return {"error": f"ro open failed: {exc}"}, []

    now_ts = int(NOW.timestamp())
    hour = now_ts - LOOP_CONTROLLER_CONTEXT_SECONDS
    day = now_ts - 24 * 3600
    week = now_ts - 7 * 24 * 3600

    def q1(sql: str, params: tuple = ()) -> int | float | None:
        try:
            row = con.execute(sql, params).fetchone()
            return row[0] if row else None
        except Exception:
            return None

    try:
        # Crash rate (share of runs ending outcome='crashed') plus the
        # broader failure share (crashed + timed_out + gave_up +
        # spawn_failed), over the rolling windows AND the fixed
        # since-overhaul baseline window.
        baseline = int(datetime.fromisoformat(LOOP_OVERHAUL_BASELINE_ISO).timestamp())
        failure_in = ",".join("?" for _ in LOOP_FAILURE_OUTCOMES)
        for label, cutoff in (("24h", day), ("7d", week), ("since_overhaul", baseline)):
            total = q1("select count(*) from task_runs where started_at >= ?", (cutoff,)) or 0
            crashed = q1(
                "select count(*) from task_runs where started_at >= ? and outcome='crashed'",
                (cutoff,),
            ) or 0
            failed = q1(
                "select count(*) from task_runs where started_at >= ? "
                f"and outcome in ({failure_in})",
                (cutoff, *LOOP_FAILURE_OUTCOMES),
            ) or 0
            metrics[f"runs_{label}"] = total
            metrics[f"crashed_{label}"] = crashed
            metrics[f"crash_rate_{label}"] = round(crashed / total, 3) if total else 0.0
            metrics[f"failed_{label}"] = failed
            metrics[f"failure_share_{label}"] = round(failed / total, 3) if total else 0.0

        # Throughput: runs per completed run.
        completed_7d = q1(
            "select count(*) from task_runs where started_at >= ? and outcome='completed'",
            (week,),
        ) or 0
        metrics["completions_7d"] = completed_7d
        metrics["runs_per_completion_7d"] = (
            round(metrics["runs_7d"] / completed_7d, 2) if completed_7d else None
        )

        # Loop yield: tasks created per creator (7d, top 10).
        try:
            metrics["yield_by_created_by_7d"] = {
                (r["created_by"] or "(none)"): r["n"]
                for r in con.execute(
                    "select coalesce(created_by,'(none)') created_by, count(*) n "
                    "from tasks where created_at >= ? group by 1 order by n desc limit 10",
                    (week,),
                )
            }
        except Exception:
            metrics["yield_by_created_by_7d"] = {}

        # Loop yield: autonomous vs operator origin of completed work.
        try:
            metrics["loop_yield_7d"] = compute_loop_yield(con, week)
        except Exception:
            metrics["loop_yield_7d"] = {}

        # Controller overhead: drain-controller share of 24h creates.
        created_24h = q1("select count(*) from tasks where created_at >= ?", (day,)) or 0
        controllers_24h = q1(
            "select count(*) from tasks where created_at >= ? "
            "and title like 'Board drain controller:%'",
            (day,),
        ) or 0
        metrics["created_24h"] = created_24h
        metrics["controllers_24h"] = controllers_24h
        metrics["controller_share_24h"] = (
            round(controllers_24h / created_24h, 3) if created_24h else 0.0
        )
        # Keep the intentional 24h overhead smoke alarm, but add current
        # pressure context so rollout/backlog-drain residue is distinguishable
        # from an active controller-create loop.
        created_1h = q1("select count(*) from tasks where created_at >= ?", (hour,)) or 0
        controllers_1h = q1(
            "select count(*) from tasks where created_at >= ? "
            "and title like 'Board drain controller:%'",
            (hour,),
        ) or 0
        metrics["created_1h"] = created_1h
        metrics["controllers_1h"] = controllers_1h
        metrics["controller_share_1h"] = (
            round(controllers_1h / created_1h, 3) if created_1h else 0.0
        )
        try:
            active_rows = con.execute(
                "select coalesce(status,'') status, count(*) n from tasks "
                "where title like 'Board drain controller:%' "
                "and status not in ('done','archived','cancelled','canceled','failed') "
                "group by 1 order by 1"
            ).fetchall()
            metrics["active_controllers"] = sum(int(r["n"] or 0) for r in active_rows)
            metrics["active_controller_statuses"] = {
                (r["status"] or "(none)"): int(r["n"] or 0) for r in active_rows
            }
        except Exception:
            metrics["active_controllers"] = None
            metrics["active_controller_statuses"] = {}

        # Blocked latency: blocked events resolved by a later unblocked
        # event within 7d (count + avg hours for the resolved ones).
        try:
            rows = con.execute(
                "select b.task_id, b.created_at b_at, "
                "(select min(u.created_at) from task_events u "
                " where u.task_id = b.task_id and u.kind='unblocked' "
                " and u.id > b.id) u_at "
                "from task_events b where b.kind='blocked' and b.created_at >= ?",
                (week,),
            ).fetchall()
            resolved = [r for r in rows if r["u_at"] is not None]
            metrics["blocked_events_7d"] = len(rows)
            metrics["blocked_resolved_7d"] = len(resolved)
            metrics["blocked_resolution_avg_hours"] = (
                round(sum(r["u_at"] - r["b_at"] for r in resolved) / len(resolved) / 3600, 2)
                if resolved else None
            )
        except Exception:
            metrics["blocked_events_7d"] = None

        # Duplicate pressure: open exact-duplicate normalized titles and
        # stacked Guide prefixes among non-terminal tasks.
        try:
            titles = [
                (r["id"], " ".join((r["title"] or "").lower().split()))
                for r in con.execute(
                    "select id, title from tasks where status not in "
                    "('done','archived','cancelled','canceled','failed')"
                )
            ]
            seen: dict[str, str] = {}
            dup_pairs = 0
            for tid, norm in titles:
                if not norm:
                    continue
                if norm in seen:
                    dup_pairs += 1
                else:
                    seen[norm] = tid
            metrics["open_duplicate_titles"] = dup_pairs
            metrics["open_stacked_guide_titles"] = sum(
                1 for _tid, norm in titles if norm.startswith("guide: guide:")
            )
        except Exception:
            metrics["open_duplicate_titles"] = None

        # Failure-mode counters the overhaul targets.
        metrics["protocol_violations_7d"] = q1(
            "select count(*) from task_events where kind='protocol_violation' "
            "and created_at >= ?", (week,),
        )
        metrics["pid_not_alive_runs_7d"] = q1(
            "select count(*) from task_runs where started_at >= ? "
            "and error like '%not alive%'", (week,),
        )
        metrics["needs_diagnosis_blocks_7d"] = q1(
            "select count(*) from task_events where kind='blocked' "
            "and created_at >= ? and payload like '%needs-diagnosis%'", (week,),
        )
        metrics["dedup_returns_7d"] = q1(
            "select count(*) from task_events where kind='dedup' "
            "and created_at >= ?", (week,),
        )
        # Signature-keyed crash alerting (24h). This surfaces actionable
        # owners/samples even when the aggregate 7d crash-rate threshold is
        # below its page line.
        try:
            signatures_24h = summarize_crash_signatures(con, day)
        except Exception as exc:  # noqa: BLE001 - telemetry must not kill audit
            signatures_24h = []
            metrics["crash_signature_error"] = str(exc)[:200]
        metrics["crash_signatures_24h"] = signatures_24h
        metrics["provider_auth_required_24h"] = sum(
            int(sig.get("count") or 0)
            for sig in signatures_24h
            if sig.get("label") == "provider-auth-required"
        )
    finally:
        con.close()

    # Alerts on hard thresholds.
    if (metrics.get("crash_rate_7d") or 0) >= LOOP_CRASH_RATE_ALERT and (metrics.get("runs_7d") or 0) >= 20:
        alerts.append(
            f"loop: crash rate 7d {metrics['crash_rate_7d']:.0%} "
            f"({metrics['crashed_7d']}/{metrics['runs_7d']} runs) >= {LOOP_CRASH_RATE_ALERT:.0%}"
        )
    if (metrics.get("controller_share_24h") or 0) >= LOOP_CONTROLLER_SHARE_ALERT and (metrics.get("created_24h") or 0) >= 10:
        active_statuses = metrics.get("active_controller_statuses") or {}
        active_context = ", ".join(f"{k}={v}" for k, v in sorted(active_statuses.items())) or "none"
        alerts.append(
            f"loop: drain-controller share 24h {metrics['controller_share_24h']:.0%} "
            f"({metrics['controllers_24h']}/{metrics['created_24h']} creates) >= {LOOP_CONTROLLER_SHARE_ALERT:.0%}; "
            f"recent 1h {metrics.get('controllers_1h')}/{metrics.get('created_1h')} creates; "
            f"open controllers {metrics.get('active_controllers')} ({active_context})"
        )
    rpc = metrics.get("runs_per_completion_7d")
    if rpc is not None and rpc >= LOOP_RUNS_PER_COMPLETION_ALERT:
        alerts.append(f"loop: {rpc} runs per completion (7d) >= {LOOP_RUNS_PER_COMPLETION_ALERT}")
    if (metrics.get("open_stacked_guide_titles") or 0) > 0:
        alerts.append(
            f"loop: {metrics['open_stacked_guide_titles']} open task(s) with stacked "
            "'Guide: Guide:' titles — dedup regression"
        )
    alerts.extend(signature_alerts(
        metrics.get("crash_signatures_24h") or [],
        metrics.get("runs_24h") or 0,
        (prev_metrics or {}).get("crash_signatures_24h") or [],
    ))

    # Deltas vs last run (reported, not alerted).
    deltas: dict = {}
    for key in ("crash_rate_7d", "controller_share_24h", "runs_per_completion_7d",
                "blocked_resolved_7d", "open_duplicate_titles"):
        cur, old = metrics.get(key), (prev_metrics or {}).get(key)
        if isinstance(cur, (int, float)) and isinstance(old, (int, float)):
            deltas[key] = round(cur - old, 3)
    metrics["deltas_vs_last_run"] = deltas
    return metrics, alerts


def hindsight_retain(content: str, tags: list[str]) -> None:
    """Best-effort store into the shared brain. Never raises."""
    try:
        def post(body: dict, sid: str | None):
            data = json.dumps(body).encode()
            req = urllib.request.Request(HINDSIGHT_MCP, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json, text/event-stream")
            if sid:
                req.add_header("mcp-session-id", sid)
            return urllib.request.urlopen(req, timeout=15)

        init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "self-audit", "version": "1.0"}}}, None)
        sid = init.headers.get("mcp-session-id")
        if not sid:
            return
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid).read()
        post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "retain", "arguments": {
                  "content": content, "context": "hermes-platform-ops", "tags": tags}}}, sid).read()
    except Exception:
        pass


# ---------------------------------------------------------------- main
def main() -> int:
    health = check_health()
    crons = check_crons()
    log_alerts, remediations = scan_logs()
    upstream = check_upstream()
    mem = check_memory_pressure()
    sec_alerts, sec_snap, sec_notes = check_security()
    disk = check_disk()

    # Load previous state to diff (cron pause set + security snapshot + issue set)
    prev = {}
    try:
        prev = json.loads(STATE_PATH.read_text())
    except Exception:
        pass

    drift_alerts, drift_notes = compute_listener_drift(prev.get("security", {}), sec_snap)
    sec_notes.extend(drift_notes)

    loop_metrics, loop_alerts = check_loop_telemetry(prev.get("loop_telemetry", {}))
    if not loop_metrics.get("error"):
        loop_metrics["yield_trend"] = update_yield_history(loop_metrics)

    issues = (
        [f"health: {x}" for x in health]
        + sec_alerts
        + drift_alerts
        + ([f"cron failing: {', '.join(crons['failing'])}"] if crons.get("failing") else [])
        + log_alerts + upstream + mem + disk + loop_alerts
    )

    prev_issues = set(prev.get("issues", []))
    cur_issues = set(issues)
    new_issues = sorted(cur_issues - prev_issues)
    resolved = sorted(prev_issues - cur_issues)

    # newly-failing crons (vs last run)
    prev_fail = set(prev.get("crons", {}).get("failing", []))
    new_fail = sorted(set(crons.get("failing", [])) - prev_fail)

    state = {
        "checked_at": NOW.isoformat(),
        "issues": issues,
        "crons": crons,
        "security": sec_snap,
        "security_notes": sec_notes,
        "remediations": remediations,
        "loop_telemetry": loop_metrics,
    }
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))
    try:
        os.chmod(STATE_PATH, 0o600)
    except Exception:
        pass

    # Always write the Obsidian report
    write_report(health, crons, log_alerts, remediations, upstream, mem, sec_alerts, sec_notes, sec_snap, disk, issues, new_issues, resolved, loop_metrics)

    # Decide whether to speak (Slack delivery): only on signal
    speak = bool(new_issues or resolved or remediations or new_fail or any(s.startswith("SECURITY") for s in issues))
    if not speak:
        return 0

    lines = [f"*Hermes platform self-audit — {NOW:%Y-%m-%d %H:%M %Z}*"]
    if any(s.startswith("SECURITY") for s in issues):
        lines.append(":rotating_light: *Security:*")
        lines += [f"  • {s}" for s in issues if s.startswith("SECURITY")]
    if new_issues:
        lines.append("*New issues:*")
        lines += [f"  • {s}" for s in new_issues if not s.startswith("SECURITY")]
    if remediations:
        lines.append("*Auto-remediated:*")
        lines += [f"  • {s}" for s in remediations]
    if resolved:
        lines.append("*Resolved since last run:*")
        lines += [f"  • {s}" for s in resolved]
    lines.append(f"_Full report: Obsidian → Hermes/Self-Audit/{NOW:%Y-%m-%d} - Platform Self-Audit.md_")
    out = "\n".join(lines)
    print(out)

    # Feed the shared brain on significant nights
    if new_issues or remediations or any(s.startswith("SECURITY") for s in issues):
        summary = "; ".join((new_issues or []) + remediations) or "security/drift event"
        hindsight_retain(f"Hermes self-audit {NOW:%Y-%m-%d}: {summary}",
                         ["hermes", "self-audit", "platform-health"])
    return 0


def write_report(health, crons, log_alerts, remediations, upstream, mem, sec_alerts, sec_notes, sec_snap, disk, issues, new_issues, resolved, loop_metrics=None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{NOW:%Y-%m-%d} - Platform Self-Audit.md"
    verdict = "all clear" if not issues else f"{len(issues)} issue(s)"
    md = [
        "---", f"date: {NOW:%Y-%m-%d}", "type: platform-self-audit",
        f"verdict: {verdict}", "---", "",
        f"# Hermes Platform Self-Audit — {NOW:%Y-%m-%d %H:%M %Z}", "",
        f"**Verdict:** {verdict}", "",
        "## Issues" if issues else "## Issues\n\n_None._",
    ]
    md += [f"- {s}" for s in issues]
    if new_issues:
        md += ["", "## New since last run"] + [f"- {s}" for s in new_issues]
    if resolved:
        md += ["", "## Resolved since last run"] + [f"- {s}" for s in resolved]
    if remediations:
        md += ["", "## Auto-remediations"] + [f"- {s}" for s in remediations]
    if sec_notes:
        md += ["", "## Security notes"] + [f"- {s}" for s in sec_notes]
    if loop_metrics:
        md += ["", "## Loop telemetry (agent-workspace-ops)"]
        if loop_metrics.get("error"):
            md += [f"- error: {loop_metrics['error']}"]
        else:
            md += [
                f"- crash rate: {loop_metrics.get('crash_rate_24h')} (24h, "
                f"{loop_metrics.get('crashed_24h')}/{loop_metrics.get('runs_24h')} runs) / "
                f"{loop_metrics.get('crash_rate_7d')} (7d, "
                f"{loop_metrics.get('crashed_7d')}/{loop_metrics.get('runs_7d')}) / "
                f"{loop_metrics.get('crash_rate_since_overhaul')} (since overhaul "
                f"{LOOP_OVERHAUL_BASELINE_ISO[:10]}, "
                f"{loop_metrics.get('crashed_since_overhaul')}/{loop_metrics.get('runs_since_overhaul')})",
                f"- failure share (crashed+timed_out+gave_up+spawn_failed): "
                f"{loop_metrics.get('failure_share_24h')} (24h, "
                f"{loop_metrics.get('failed_24h')}/{loop_metrics.get('runs_24h')}) / "
                f"{loop_metrics.get('failure_share_7d')} (7d, "
                f"{loop_metrics.get('failed_7d')}/{loop_metrics.get('runs_7d')}) / "
                f"{loop_metrics.get('failure_share_since_overhaul')} (since overhaul, "
                f"{loop_metrics.get('failed_since_overhaul')}/{loop_metrics.get('runs_since_overhaul')})",
                f"- runs per completion (7d): {loop_metrics.get('runs_per_completion_7d')} "
                f"({loop_metrics.get('completions_7d')} completions)",
                f"- controller overhead (24h): {loop_metrics.get('controller_share_24h')} "
                f"({loop_metrics.get('controllers_24h')}/{loop_metrics.get('created_24h')} creates); "
                f"recent 1h: {loop_metrics.get('controller_share_1h')} "
                f"({loop_metrics.get('controllers_1h')}/{loop_metrics.get('created_1h')} creates); "
                f"open: {loop_metrics.get('active_controllers')} "
                f"{json.dumps(loop_metrics.get('active_controller_statuses') or {}, sort_keys=True)}",
                f"- blocked events resolved (7d): {loop_metrics.get('blocked_resolved_7d')}/"
                f"{loop_metrics.get('blocked_events_7d')} "
                f"(avg {loop_metrics.get('blocked_resolution_avg_hours')}h for resolved)",
                f"- open duplicate titles: {loop_metrics.get('open_duplicate_titles')}; "
                f"stacked Guide titles: {loop_metrics.get('open_stacked_guide_titles')}",
                f"- protocol violations (7d): {loop_metrics.get('protocol_violations_7d')}; "
                f"pid-not-alive runs (7d): {loop_metrics.get('pid_not_alive_runs_7d')}",
                f"- crash signatures (24h): {json.dumps(loop_metrics.get('crash_signatures_24h') or [], sort_keys=True)}; "
                f"provider-auth-required: {loop_metrics.get('provider_auth_required_24h')}",
                f"- needs-diagnosis blocks (7d): {loop_metrics.get('needs_diagnosis_blocks_7d')}; "
                f"dedup returns (7d): {loop_metrics.get('dedup_returns_7d')}",
                f"- loop yield by creator (7d): {json.dumps(loop_metrics.get('yield_by_created_by_7d') or {}, sort_keys=True)}",
                f"- deltas vs last run: {json.dumps(loop_metrics.get('deltas_vs_last_run') or {}, sort_keys=True)}",
            ]
            ly = loop_metrics.get("loop_yield_7d") or {}
            if ly:
                md += [
                    f"- loop yield (7d completions): autonomous {ly.get('autonomous_share_completed')} "
                    f"({ly.get('completed_autonomous')}/{ly.get('completed_total')}), "
                    f"delegate {ly.get('completed_delegate')}, operator {ly.get('completed_operator')}, "
                    f"unattributed {ly.get('completed_unattributed')}",
                    f"- loop yield (7d creates): autonomous {ly.get('autonomous_share_created')} "
                    f"({ly.get('created_autonomous')}/{ly.get('created_total')})",
                ]
            trend = loop_metrics.get("yield_trend") or {}
            if trend and not trend.get("error"):
                parts = []
                for key, t in sorted(trend.items()):
                    series = t.get("series") or []
                    if series:
                        arrow = f" Δ{t['delta']:+}" if t.get("delta") is not None else ""
                        parts.append(f"{key}: {series[0]} → {series[-1]}{arrow} (n={t['runs']})")
                if parts:
                    md += ["- trend (last audits): " + "; ".join(parts)]
    md += [
        "", "## Raw signals",
        f"- health degraded: {health or 'none'}",
        f"- crons failing: {crons.get('failing') or 'none'}",
        f"- crons paused: {crons.get('paused') or 'none'}",
        f"- log alerts: {log_alerts or 'none'}",
        f"- upstream: {upstream or 'current'}",
        f"- memory: {mem or 'ok'}",
        f"- security: {sec_alerts or 'no drift'}",
        f"- security notes: {sec_notes or 'none'}",
        f"- firewall: {sec_snap.get('firewall')}, funnel_public: {sec_snap.get('funnel_public')}",
        f"- all-interface ports: {sec_snap.get('all_iface_ports')}",
        f"- all-interface listeners: {sec_snap.get('all_iface_listeners')}",
        f"- perms: {sec_snap.get('perms')}",
        f"- disk: {disk or 'ok'}",
        "", f"*Generated by platform-self-audit.py at {NOW.isoformat()}*",
    ]
    path.write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
