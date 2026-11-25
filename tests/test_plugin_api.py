"""Route authorization and webhook validation without external accounts."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from dashboard import plugin_api
from ifood.config import Config


def fake_request(*, user_id: str | None = None, app: FastAPI | None = None):
    state = SimpleNamespace()
    if user_id:
        state.session = SimpleNamespace(user_id=user_id)
    return SimpleNamespace(state=state, app=app or SimpleNamespace(state=SimpleNamespace(auth_required=True)))


def webhook_request(body: bytes, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http", "method": "POST", "path": "/sheets/webhook",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
    }
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_operator_requires_hermes_identity_and_allowed_operator(monkeypatch):
    cfg = Config(True, frozenset({"operator"}), "", "https://merchant-api.ifood.com.br", "")
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: cfg))
    with pytest.raises(HTTPException) as missing:
        await plugin_api.operator(fake_request())
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException) as denied:
        await plugin_api.operator(fake_request(user_id="intruder"))
    assert denied.value.status_code == 403
    authorized = fake_request(user_id="operator")
    assert await plugin_api.operator(authorized) == cfg
    assert authorized.state.ifood_operator_id == "operator"


@pytest.mark.asyncio
async def test_legacy_confirmation_is_retired_and_webhook_validation_is_strict(monkeypatch):
    secret = "synthetic-hmac-key"
    cfg = Config(True, frozenset({"operator"}), "", "https://merchant-api.ifood.com.br", "",
                 sheets_webhook_secret=secret)
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: cfg))
    req = fake_request(user_id="operator")
    req.state.ifood_operator_id = "operator"
    with pytest.raises(HTTPException) as legacy:
        await plugin_api.confirm_hours(plugin_api.ConfirmBody(proposal_id="old"), req, cfg)
    assert legacy.value.status_code == 409

    now = str(int(time.time()))
    body = json.dumps({"unknown": "value"}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), now.encode() + b"." + body, hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as invalid:
        await plugin_api.sheets_webhook(webhook_request(body, {
            "X-Hermes-Sheets-Timestamp": now,
            "X-Hermes-Sheets-Signature": "sha256=bad",
        }))
    assert invalid.value.status_code == 401

    with pytest.raises(HTTPException) as unknown:
        await plugin_api.sheets_webhook(webhook_request(body, {
            "X-Hermes-Sheets-Timestamp": now,
            "X-Hermes-Sheets-Signature": sig,
        }))
    assert unknown.value.status_code == 400


def test_hermes_principal_extraction_supports_native_and_hosted_sessions():
    native = fake_request()
    native.state.native_http_principal = {"profile_id": "default", "purpose": "native-http",
                                          "capabilities": frozenset({"http:owner"})}
    hosted = fake_request()
    hosted.state.hosted_http_authenticated = True
    assert plugin_api.resolve_hermes_request_identity(native) == "legacy-token-owner"
    assert plugin_api.resolve_hermes_request_identity(hosted) == "legacy-token-owner"


@pytest.mark.asyncio
async def test_mounted_plugin_rejects_anonymous_http_request(monkeypatch):
    cfg = Config(True, frozenset({"operator"}), "", "https://merchant-api.ifood.com.br", "")
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: cfg))
    app = FastAPI()
    app.state.auth_required = True
    app.include_router(plugin_api.router, prefix="/api/plugins/ifood-loja")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/plugins/ifood-loja/status")
    assert response.status_code == 401
