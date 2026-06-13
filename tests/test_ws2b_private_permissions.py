import logging
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import status as gateway_status
from gateway.platforms.api_server import ResponseStore
from hermes_cli import kanban_db
from hermes_logging import _ManagedRotatingFileHandler
from hermes_state import SessionDB, secure_private_file


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX mode bits are not enforced on Windows"
)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_private_file_tightens_parent_and_file(tmp_path):
    secret_path = tmp_path / "nested" / "secret.txt"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text("secret", encoding="utf-8")
    secret_path.chmod(0o644)
    secret_path.parent.chmod(0o755)

    secure_private_file(secret_path)

    assert mode(secret_path.parent) == 0o700
    assert mode(secret_path) == 0o600


def test_session_db_uses_owner_only_permissions(tmp_path):
    db_path = tmp_path / "state.db"

    db = SessionDB(db_path)
    db.close()

    assert mode(db_path.parent) == 0o700
    assert mode(db_path) == 0o600


def test_response_store_uses_owner_only_permissions(tmp_path):
    db_path = tmp_path / "response_store.db"

    store = ResponseStore(db_path=str(db_path))
    store.close()

    assert mode(db_path.parent) == 0o700
    assert mode(db_path) == 0o600


def test_kanban_db_uses_owner_only_permissions(tmp_path):
    db_path = tmp_path / "kanban.db"

    conn = kanban_db.connect(db_path=db_path)
    conn.close()

    assert mode(db_path.parent) == 0o700
    assert mode(db_path) == 0o600


def test_rotating_log_handler_uses_owner_only_permissions(tmp_path):
    log_path = tmp_path / "logs" / "agent.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"ws2b-private-perms-{log_path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = _ManagedRotatingFileHandler(str(log_path), maxBytes=1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("hello")
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert mode(log_path) == 0o600


def test_gateway_runtime_status_uses_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_status, "get_hermes_home", lambda: tmp_path)
    runtime_path = tmp_path / "gateway_state.json"
    runtime_path.write_text("{}", encoding="utf-8")
    runtime_path.chmod(0o644)

    gateway_status.write_runtime_status(gateway_state="running")

    assert mode(runtime_path) == 0o600


def test_setup_open_webui_launcher_filters_password_lines():
    script = Path("scripts/setup_open_webui.sh").read_text(encoding="utf-8")

    assert "redact_open_webui_log_stream" in script
    assert "open-webui serve 2>&1 | redact_open_webui_log_stream" in script
    assert "[REDACTED]" in script
    assert "nohup hermes gateway run" not in script

    redacted = subprocess.run(
        ["sed", "-E", r"s/(Password:[[:space:]]*).*/\1[REDACTED]/I"],
        input="Open WebUI Password: super-secret-token\nready\n",
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "super-secret-token" not in redacted
    assert "Password: [REDACTED]" in redacted
    assert "ready" in redacted
