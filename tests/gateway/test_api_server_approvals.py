"""Tests for the Phase-1 gateway approval id routes:

* GET  /v1/approvals                     — list pending approvals
* POST /v1/approvals/{id}/{choice}       — resolve a specific approval by id

Covers auth (Bearer), approval-id format validation, alias mapping
(approve->once), choice validation, the always->session downgrade surfaced
from the resolver, 409 already_resolved, response shape, and
auth-before-side-effects ordering.

The approval.py resolver/lister logic itself is unit-tested in
tests/tools/test_gateway_approval_by_id.py; here we patch them and exercise
the HTTP handler behavior.
"""

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)

# A well-formed approval id (uuid4 hex: 32 lowercase hex chars).
AID = "0123456789abcdef0123456789abcdef"


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {}
    if api_key:
        extra["key"] = api_key
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _make_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_get("/v1/approvals", adapter._handle_list_approvals)
    app.router.add_post("/v1/approvals/{id}/{choice}", adapter._handle_resolve_approval_by_id)
    return app


@pytest.fixture
def adapter():
    return _make_adapter()


@pytest.fixture
def auth_adapter():
    return _make_adapter(api_key="sk-secret")


class TestListApprovals:
    @pytest.mark.asyncio
    async def test_returns_pending_list(self, adapter):
        sample = [{
            "approval_id": AID, "session_key": "s1", "command": "rm -rf /tmp",
            "description": "d", "pattern_keys": [], "allow_permanent": True,
            "agent_label": "", "requested_at": 1.0, "expires_at": 901.0,
        }]
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch("tools.approval.list_gateway_approvals", return_value=sample):
                resp = await cli.get("/v1/approvals")
                assert resp.status == 200
                body = await resp.json()
                assert body["object"] == "list"
                assert body["data"] == sample

    @pytest.mark.asyncio
    async def test_requires_auth(self, auth_adapter):
        app = _make_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/v1/approvals")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_with_valid_auth(self, auth_adapter):
        app = _make_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch("tools.approval.list_gateway_approvals", return_value=[]):
                resp = await cli.get(
                    "/v1/approvals",
                    headers={"Authorization": "Bearer sk-secret"},
                )
                assert resp.status == 200
                body = await resp.json()
                assert body["data"] == []


class TestResolveApprovalById:
    @pytest.mark.asyncio
    async def test_malformed_id_returns_400(self, adapter):
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch("tools.approval.resolve_gateway_approval_by_id") as m:
                resp = await cli.post("/v1/approvals/not-a-uuid/once")
                assert resp.status == 400
                body = await resp.json()
                assert body["error"]["code"] == "invalid_approval_id"
                # Malformed id must never reach the resolver / a queue scan.
                m.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_choice_returns_400(self, adapter):
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/v1/approvals/{AID}/banana")
            assert resp.status == 400
            body = await resp.json()
            assert body["error"]["code"] == "invalid_approval_choice"

    @pytest.mark.asyncio
    async def test_alias_approve_maps_to_once(self, adapter):
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch(
                "tools.approval.resolve_gateway_approval_by_id",
                return_value={"resolved": True, "session_key": "s1", "choice": "once"},
            ) as m:
                resp = await cli.post(f"/v1/approvals/{AID}/approve")
                assert resp.status == 200
                m.assert_called_once_with(AID, "once")
                body = await resp.json()
                assert body["approval_id"] == AID
                assert body["choice"] == "once"
                assert body["resolved"] is True

    @pytest.mark.asyncio
    async def test_not_found_returns_409(self, adapter):
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch(
                "tools.approval.resolve_gateway_approval_by_id",
                return_value={"resolved": False, "reason": "not_found"},
            ):
                resp = await cli.post(f"/v1/approvals/{AID}/once")
                assert resp.status == 409
                body = await resp.json()
                assert body["error"]["code"] == "already_resolved"

    @pytest.mark.asyncio
    async def test_always_downgrade_reflected_in_response(self, adapter):
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch(
                "tools.approval.resolve_gateway_approval_by_id",
                return_value={"resolved": True, "session_key": "s1", "choice": "session"},
            ) as m:
                resp = await cli.post(f"/v1/approvals/{AID}/always")
                assert resp.status == 200
                m.assert_called_once_with(AID, "always")
                body = await resp.json()
                assert body["choice"] == "session"

    @pytest.mark.asyncio
    async def test_deny_passthrough(self, adapter):
        app = _make_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch(
                "tools.approval.resolve_gateway_approval_by_id",
                return_value={"resolved": True, "session_key": "s1", "choice": "deny"},
            ) as m:
                resp = await cli.post(f"/v1/approvals/{AID}/deny")
                assert resp.status == 200
                m.assert_called_once_with(AID, "deny")

    @pytest.mark.asyncio
    async def test_requires_auth(self, auth_adapter):
        app = _make_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(f"/v1/approvals/{AID}/once")
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_auth_rejects_before_resolution(self, auth_adapter):
        """Auth must short-circuit before the resolver is ever invoked."""
        app = _make_app(auth_adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch("tools.approval.resolve_gateway_approval_by_id") as m:
                resp = await cli.post(f"/v1/approvals/{AID}/once")
                assert resp.status == 401
                m.assert_not_called()
