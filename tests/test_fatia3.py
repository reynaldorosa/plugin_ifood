"""Regressões da fatia multi-padaria, carência e avisos."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import replace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from dashboard import plugin_api
from ifood import poller
from ifood.config import Config, TenantConfig
from ifood.models import BackendUsed, ChangeDomain, canonical_digest
from ifood.notifier import notify_event
from ifood.sheets_parser import parse_sheet_snapshot
from ifood.storage import SyncStorage
from ifood.sync_engine import SyncEngine


def _config(tmp_path):
    return Config(
        True, frozenset({"operator"}), "", "https://merchant-api.ifood.com.br", "",
        data_dir=str(tmp_path),
        tenants={
            "padaria-a": TenantConfig("padaria-a", "sheet-a", "111"),
            "padaria-b": TenantConfig("padaria-b", "sheet-b", "222"),
        },
    )


@pytest.mark.asyncio
async def test_http_reads_require_tenant_and_keep_data_separate(tmp_path):
    cfg = _config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    storage.update_poller_status(tenant_key="padaria-a", last_success_at="a",
                                 last_error=None, consecutive_failures=0, last_summary="A")
    storage.update_poller_status(tenant_key="padaria-b", last_success_at="b",
                                 last_error=None, consecutive_failures=0, last_summary="B")
    with pytest.raises(HTTPException) as exc:
        await plugin_api.sync_overview(cfg=cfg)
    assert exc.value.status_code == 400
    a = await plugin_api.sync_overview(tenant_key="padaria-a", cfg=cfg)
    b = await plugin_api.sync_overview(tenant_key="padaria-b", cfg=cfg)
    assert a["sheets_poller"]["last_summary"] == "A"
    assert b["sheets_poller"]["last_summary"] == "B"
    assert a["readiness"]["sheets_spreadsheet_id"] == "sheet-a"
    assert b["readiness"]["sheets_spreadsheet_id"] == "sheet-b"
    assert plugin_api._tenant_for_sheet(cfg, "sheet-b") == "padaria-b"
    assert await plugin_api.list_sync_proposals(tenant_key="padaria-a", cfg=cfg) == []


def test_catalog_removal_waits_30_minutes_and_cancels_on_return(tmp_path):
    storage = SyncStorage(_config(tmp_path).db_path)
    previous = [{"item_id": "bread"}]
    assert storage.review_catalog_removals("padaria-a", "store", previous, [], now_epoch=100) == (
        ["bread"], [], ["bread"]
    )
    assert storage.review_catalog_removals("padaria-a", "store", previous, [], now_epoch=1899) == (
        ["bread"], [], []
    )
    assert storage.review_catalog_removals("padaria-a", "store", previous, [], now_epoch=1900) == (
        [], ["bread"], []
    )
    assert storage.review_catalog_removals("padaria-a", "store", previous, previous, now_epoch=1901) == (
        [], [], []
    )
    assert storage.review_catalog_removals("padaria-a", "store", previous, [], now_epoch=1902) == (
        ["bread"], [], ["bread"]
    )
    assert storage.review_catalog_removals("padaria-b", "store", previous, [], now_epoch=1902) == (
        ["bread"], [], ["bread"]
    )


def test_auto_write_budget_is_scoped_and_limits_batch(tmp_path):
    storage = SyncStorage(_config(tmp_path).db_path)
    assert storage.claim_auto_write_budget("padaria-a", "store", 2, 3)
    assert not storage.claim_auto_write_budget("padaria-a", "store", 2, 3)
    assert storage.claim_auto_write_budget("padaria-a", "store", 1, 3)
    assert storage.claim_auto_write_budget("padaria-b", "store", 3, 3)
    assert not storage.claim_auto_write_budget("padaria-b", "store", 1, 3)


def test_engine_preserves_item_during_grace_then_proposes_unavailable(tmp_path):
    cfg = _config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    stores = [{"store_key": "store", "channel": "IFOOD", "merchant_id": "merchant-1",
               "execution_mode": "PLAYWRIGHT_ONLY", "portal_url": "https://portal.ifood.com.br"}]
    first = parse_sheet_snapshot(stores_rows=stores, hours_rows=[], catalog_rows=[
        {"store_key": "store", "item_id": "bread", "price_brl": "10,00", "available": True},
    ], tenant_key="padaria-a")
    previous = first.stores["store"].domain_payload(ChangeDomain.CATALOG)
    storage.upsert_snapshot(tenant_key="padaria-a", store_key="store", domain="CATALOG",
                            last_applied_hash=canonical_digest(previous), last_state=previous,
                            last_backend="PLAYWRIGHT", last_outcome="CONFIRMED")
    engine = SyncEngine(cfg, storage=storage)
    engine._select_backend = lambda *args: (BackendUsed.PLAYWRIGHT, None)
    engine._observe_current = lambda **kwargs: (previous, True)
    absent = parse_sheet_snapshot(stores_rows=stores, hours_rows=[], catalog_rows=[],
                                  tenant_key="padaria-a")
    waiting = engine.process_snapshot(snapshot=absent, tenant_key="padaria-a",
                                      trigger_source="TEST", actor_id="operator", write_back=False)
    assert waiting["results"][0]["outcome"] == "NO_CHANGES"
    assert storage.list_notifications("padaria-a")[0]["event_type"] == "REMOVAL_GRACE"
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("UPDATE catalog_removal_grace SET first_missing_epoch=?", (time.time() - 1801,))
    due = engine.process_snapshot(snapshot=absent, tenant_key="padaria-a",
                                  trigger_source="TEST", actor_id="operator", write_back=False)
    proposal_id = due["results"][0]["proposal_id"]
    assert proposal_id
    proposal = storage.get_proposal(proposal_id, tenant_key="padaria-a")
    assert proposal["proposed"][0]["available"] is False
    assert proposal["status"] in {"PENDING", "BLOCKED"}


def test_deleted_sheet_row_never_auto_applies_after_grace(tmp_path):
    cfg = replace(_config(tmp_path), default_sync_mode="AUTO_APPLY",
                  write_capabilities=frozenset({"catalog_availability"}),
                  tenants={"padaria-a": TenantConfig("padaria-a", stores={
                      "store": {"merchant_id": "merchant-1", "channel": "IFOOD",
                                "write_capabilities": ["catalog_availability"]},
                  })})
    storage = SyncStorage(cfg.db_path)
    previous = [{"item_id": "bread", "item_name": "Pão", "description": None,
                 "price_cents": 1000, "original_price_cents": None, "available": True}]
    storage.upsert_snapshot(tenant_key="padaria-a", store_key="store", domain="CATALOG",
                            last_applied_hash=canonical_digest(previous), last_state=previous,
                            last_backend="PLAYWRIGHT", last_outcome="CONFIRMED")
    stores = [{"store_key": "store", "merchant_id": "merchant-1",
               "execution_mode": "PLAYWRIGHT_ONLY", "portal_url": "https://portal.ifood.com.br"}]
    absent = parse_sheet_snapshot(stores_rows=stores, hours_rows=[], catalog_rows=[],
                                  tenant_key="padaria-a", default_sync_mode="AUTO_APPLY")
    engine = SyncEngine(cfg, storage=storage)
    engine._select_backend = lambda *args: (BackendUsed.PLAYWRIGHT, None)
    engine._observe_current = lambda **kwargs: (previous, True)
    engine._execute_write = lambda **kwargs: pytest.fail("Exclusão automática tentou escrever")
    engine.process_snapshot(snapshot=absent, tenant_key="padaria-a",
                            trigger_source="TEST", actor_id="operator", write_back=False)
    with sqlite3.connect(cfg.db_path) as conn:
        conn.execute("UPDATE catalog_removal_grace SET first_missing_epoch=?", (time.time() - 1801,))
    due = engine.process_snapshot(snapshot=absent, tenant_key="padaria-a",
                                  trigger_source="TEST", actor_id="operator", write_back=False)
    assert due["results"][0]["outcome"] == "BLOCKED_GUARDRAIL"
    assert "confirmação explícita" in due["results"][0]["message"]
    assert storage.get_snapshot("store", "CATALOG", tenant_key="padaria-a")["last_state"] == previous


def test_telegram_uses_tenant_chat_and_retries_failed_event(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    monkeypatch.setenv("IFOOD_LOJA_TELEGRAM_BOT_TOKEN", "synthetic-token")
    calls = []

    def fail_once(url, **kwargs):
        calls.append(kwargs["json"]["chat_id"])
        if len(calls) == 1:
            raise httpx.ConnectError("synthetic failure")
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fail_once)
    notify_event(cfg, storage, "padaria-b", "event-1", "PROPOSAL", "Revisar")
    assert storage.list_notifications("padaria-b")[0]["delivery_status"] == "FAILED"
    notify_event(cfg, storage, "padaria-b", "event-1", "PROPOSAL", "Revisar")
    notify_event(cfg, storage, "padaria-b", "event-1", "PROPOSAL", "Revisar")
    assert calls == ["222", "222"]
    assert storage.list_notifications("padaria-b")[0]["delivery_status"] == "SENT"
    assert storage.list_notifications("padaria-a") == []


@pytest.mark.asyncio
async def test_signed_webhook_routes_by_spreadsheet_id(tmp_path, monkeypatch):
    cfg = replace(_config(tmp_path), sheets_webhook_secret="synthetic-secret")
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: cfg))
    payload = {"spreadsheet_id": "sheet-b", "stores_rows": [], "hours_rows": [], "catalog_rows": []}
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"synthetic-secret", timestamp.encode() + b"." + body,
                         hashlib.sha256).hexdigest()
    headers = [(b"x-hermes-sheets-timestamp", timestamp.encode()),
               (b"x-hermes-sheets-signature", f"sha256={signature}".encode())]
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": "/sheets/webhook",
                       "headers": headers}, receive)
    result = await plugin_api.sheets_webhook(request)
    assert result["tenant_key"] == "padaria-b"
    assert result["duplicate"] is False
    assert SyncStorage(cfg.db_path).list_snapshots("padaria-a") == []


@pytest.mark.asyncio
async def test_poller_starts_each_configured_tenant(tmp_path, monkeypatch):
    cfg = replace(_config(tmp_path), google_access_token="synthetic-token",
                  sheets_poll_seconds=30)
    started = []
    async def fake_tenant_poller(tenant_config, key):
        started.append((key, tenant_config.sheets_spreadsheet_id))
    monkeypatch.setattr(poller, "_run_tenant_poller", fake_tenant_poller)
    await poller.run_sheets_poller(cfg)
    assert set(started) == {("padaria-a", "sheet-a"), ("padaria-b", "sheet-b")}


def test_write_back_uses_tenant_specific_spreadsheet_id(tmp_path):
    cfg = replace(_config(tmp_path), google_access_token="synthetic-token")
    seen_sheet_ids: list[str] = []

    class FakeSheetsClient:
        def __init__(self, c: Config) -> None:
            seen_sheet_ids.append(c.sheets_spreadsheet_id)

        def write_sync_feedback(self, *, audit_rows):
            assert len(audit_rows) == 1

    engine = SyncEngine(cfg, sheets_client_factory=FakeSheetsClient)
    warning = engine._write_back_to_sheets(
        [
            {
                "store_key": "loja-b",
                "domain": "CATALOG",
                "backend_used": "PLAYWRIGHT",
                "outcome": "PENDING_REVIEW",
                "message": "Proposta criada",
                "proposal_id": "prop-b",
            }
        ],
        "WEBHOOK",
        tenant_key="padaria-b",
    )
    assert warning is None
    assert seen_sheet_ids == ["sheet-b"]


def test_verified_store_capabilities_normalizes_case(tmp_path):
    cfg = replace(
        _config(tmp_path),
        write_capabilities=frozenset({"catalog_availability", "catalog_price"}),
        tenants={
            "padaria-a": TenantConfig(
                "padaria-a",
                stores={
                    "loja-99": {
                        "channel": " 99food ",
                        "merchant_id": "m-99",
                        "write_capabilities": [" CATALOG_AVAILABILITY "],
                    }
                },
            )
        },
    )
    caps = cfg.verified_store_capabilities("padaria-a", "loja-99", "99FOOD", "m-99")
    assert caps == frozenset({"catalog_availability"})


def test_auto_apply_login_required_emits_single_notification(tmp_path):
    cfg = replace(
        _config(tmp_path),
        default_sync_mode="AUTO_APPLY",
        write_capabilities=frozenset({"catalog_availability"}),
        validated_portal_domains=frozenset({"CATALOG"}),
        tenants={
            "padaria-a": TenantConfig(
                "padaria-a",
                stores={
                    "store": {
                        "merchant_id": "merchant-1",
                        "channel": "99FOOD",
                        "write_capabilities": ["catalog_availability"],
                    }
                },
            )
        },
    )
    storage = SyncStorage(cfg.db_path)
    previous = [
        {
            "item_id": "bread",
            "item_name": "Pão",
            "description": None,
            "price_cents": 1000,
            "original_price_cents": None,
            "available": True,
        }
    ]
    stores = [
        {
            "store_key": "store",
            "channel": "99FOOD",
            "merchant_id": "merchant-1",
            "execution_mode": "PLAYWRIGHT_ONLY",
            "portal_url": "https://merchant.99app.com/portal",
        }
    ]
    snap = parse_sheet_snapshot(
        stores_rows=stores,
        hours_rows=[],
        catalog_rows=[{"store_key": "store", "item_id": "bread", "price_brl": "10,00", "available": False}],
        tenant_key="padaria-a",
        default_sync_mode="AUTO_APPLY",
    )
    engine = SyncEngine(cfg, storage=storage)
    engine._observe_current = lambda **kwargs: (previous, True)
    engine._execute_write = lambda **kwargs: {
        "outcome": "LOGIN_REQUIRED",
        "message": "Sessão expirada.",
        "observed": None,
    }
    res = engine.process_snapshot(
        snapshot=snap,
        trigger_source="SHEETS_POLLER",
        actor_id="poller",
        tenant_key="padaria-a",
        write_back=False,
    )
    assert res["results"][0]["outcome"] == "LOGIN_REQUIRED"
    notes = storage.list_notifications("padaria-a")
    assert len(notes) == 1
    assert notes[0]["event_type"] == "LOGIN_REQUIRED"
