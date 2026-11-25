"""Testes da sincronização Google Sheets → iFood API / Playwright Fallback + Guardrails."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ifood.client import IfoodClient
from ifood.config import Config
from ifood.models import BackendUsed, ChangeDomain
from ifood.poller import poller_enabled
from ifood.sheets_client import GoogleSheetsClient, mint_service_account_token
from ifood.sheets_parser import (
    normalize_day_of_week,
    parse_price_to_cents,
    parse_sheet_snapshot,
    verify_webhook_signature,
)
from ifood.storage import SyncStorage
from ifood.sync_engine import SyncEngine
from ifood.write_policy import approved_execution

MERCHANT_1 = "550e8400-e29b-41d4-a716-446655440000"


def _make_config(
    tmp_path: Path,
    *,
    access_token: str = "ifood-token",
    sheets_id: str = "sheet-123",
    google_token: str = "google-token",
    max_pct: float = 40.0,
) -> Config:
    return Config(
        True,
        frozenset({"operator"}),
        MERCHANT_1,
        "https://merchant-api.ifood.com.br",
        access_token,
        data_dir=str(tmp_path),
        sheets_spreadsheet_id=sheets_id,
        google_access_token=google_token,
        sheets_webhook_secret="super-secret-hmac",
        max_price_change_pct=max_pct,
    )


def test_sheets_parser_handles_ptbr_days_prices_and_flags_invalid_rows() -> None:
    assert normalize_day_of_week("Segunda-feira") == "MONDAY"
    assert normalize_day_of_week("Sáb") == "SATURDAY"
    assert parse_price_to_cents("R$ 34,90") == 3490
    assert parse_price_to_cents("1.250,50") == 125050
    with pytest.raises(ValueError, match="inválido"):
        parse_price_to_cents("abc")

    snap = parse_sheet_snapshot(
        stores_rows=[
            [
                "store_key",
                "channel",
                "merchant_id",
                "store_name",
                "execution_mode",
                "sync_mode",
                "enabled",
                "prep_time_minutes",
                "status_action",
            ],
            [
                "loja-1",
                "IFOOD",
                MERCHANT_1,
                "Loja Centro",
                "API_FIRST",
                "AUTO_APPLY",
                "SIM",
                "25",
                "OPEN",
            ],
        ],
        hours_rows=[
            ["store_key", "day_of_week", "start_time", "end_time"],
            ["loja-1", "SEGUNDA", "09:00", "18:00"],
            ["loja-1", "TERCA", "20:00", "18:00"],  # inválido: end < start
        ],
        catalog_rows=[
            ["store_key", "item_id", "item_name", "price_brl", "available"],
            ["loja-1", "item-1", "Burger", "R$ 29,90", "SIM"],
        ],
    )
    assert "loja-1" in snap.stores
    assert len(snap.stores["loja-1"].shifts) == 1
    assert snap.stores["loja-1"].shifts[0] == {
        "dayOfWeek": "MONDAY",
        "start": "09:00:00",
        "duration": 540,
    }
    assert snap.stores["loja-1"].catalog_items[0].price_cents == 2990
    assert len(snap.row_errors) == 2
    assert all(error["sheet"] == "Horarios" and "row" in error for error in snap.row_errors)
    assert snap.stores["loja-1"].store.sync_mode.value == "REVIEW"


def test_webhook_hmac_signature_verification() -> None:
    body = b'{"hello":"world"}'
    now = int(time.time())
    ts = str(now)
    sig = "sha256=" + hmac.new(b"sec", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    verify_webhook_signature(
        secret="sec",
        timestamp_header=ts,
        signature_header=sig,
        raw_body=body,
        now_epoch=now,
    )
    with pytest.raises(PermissionError, match="expirado"):
        verify_webhook_signature(
            secret="sec",
            timestamp_header=str(now - 900),
            signature_header=sig,
            raw_body=body,
            now_epoch=now,
        )
    with pytest.raises(PermissionError, match="inválida"):
        verify_webhook_signature(
            secret="sec", timestamp_header=ts, signature_header="sha256=deadbeef",
            raw_body=body, now_epoch=now,
        )


def test_auto_apply_requires_explicit_environment_and_capabilities_are_independent(monkeypatch, tmp_path):
    monkeypatch.setenv("IFOOD_LOJA_DEFAULT_SYNC_MODE", "AUTO_APPLY")
    monkeypatch.setenv("IFOOD_LOJA_VERIFIED_WRITE_CAPABILITIES", "opening_hours,catalog_price,unknown")
    monkeypatch.setenv("IFOOD_LOJA_DATA_DIR", str(tmp_path))
    config = Config.from_env()
    assert config.default_sync_mode == "AUTO_APPLY"
    assert config.write_capabilities == frozenset({"opening_hours", "catalog_price"})


def test_service_account_token_uri_rejects_non_google_hosts() -> None:
    with pytest.raises(ValueError, match="endpoint HTTPS oficial"):
        mint_service_account_token({
            "client_email": "sync@example.iam.gserviceaccount.com",
            "private_key": "invalid-key-is-never-parsed",
            "token_uri": "http://127.0.0.1/token",
        })


def test_observe_catalog_reads_every_active_catalog(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    seen: list[str] = []

    class MultiCatalogApi:
        def __init__(self, _config: Config) -> None:
            pass

        def list_catalogs(self, merchant_id=None):
            return [{"id": "delivery"}, {"id": "pickup"}]

        def list_catalog_items(self, catalog_id, merchant_id=None):
            seen.append(catalog_id)
            item_id = "item-a" if catalog_id == "delivery" else "item-b"
            return [{
                "id": item_id,
                "name": item_id,
                "description": "",
                "price": {"value": 10, "originalValue": 12},
                "status": "AVAILABLE",
            }]

    engine = SyncEngine(cfg, api_client_factory=MultiCatalogApi)
    store = parse_sheet_snapshot(stores_rows=[{
        "store_key": "store-a", "merchant_id": MERCHANT_1, "execution_mode": "API_ONLY"
    }]).stores["store-a"].store
    observed, live = engine._observe_current(
        store=store,
        domain=ChangeDomain.CATALOG,
        backend=BackendUsed.IFOOD_API,
        desired_payload=[{"item_id": "item-a"}, {"item_id": "item-b"}],
        last_snapshot=None,
    )
    assert live is True
    assert seen == ["delivery", "pickup"]
    assert {row["item_id"] for row in observed} == {"item-a", "item-b"}


def test_apps_script_preserves_display_values_and_supports_hermes_auth() -> None:
    source = (Path(__file__).parents[1] / "apps_script" / "Code.gs").read_text(encoding="utf-8")
    assert "getDataRange().getDisplayValues()" in source
    assert "HERMES_AUTH_TOKEN" in source
    assert "X-Hermes-Session-Token" in source
    assert "'Bearer ' + authToken" in source
    assert "appendRow([" not in source


def test_ifood_write_refuses_merchant_outside_oauth_authorization(tmp_path: Path) -> None:
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/merchant/v1.0/merchants":
            return httpx.Response(200, json=[{"id": "another-merchant"}])
        writes.append(request.url.path)
        return httpx.Response(200, json={})

    client = IfoodClient(_make_config(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(PermissionError, match="bloqueada"):
        client.put_opening_hours([], merchant_id=MERCHANT_1)
    assert writes == []


def test_scoped_approval_binds_write_to_merchant_and_proposal(tmp_path: Path) -> None:
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/merchant/v1.0/merchants":
            return httpx.Response(200, json=[{"id": MERCHANT_1}])
        writes.append(request.url.path)
        return httpx.Response(200, json={})

    config = replace(_make_config(tmp_path), write_capabilities=frozenset({"opening_hours"}))
    client = IfoodClient(config, transport=httpx.MockTransport(handler))
    with pytest.raises(PermissionError):
        client.put_opening_hours([], merchant_id=MERCHANT_1)
    assert writes == []
    with approved_execution(
        execution_id="proposal-a", merchant_id=MERCHANT_1, channel="IFOOD",
        domain=ChangeDomain.OPENING_HOURS, payload=[], actor_id="operator",
    ):
        with pytest.raises(PermissionError, match="valores de escrita"):
            client.put_opening_hours(
                [{"dayOfWeek": "MONDAY", "start": "10:00:00", "duration": 60}],
                merchant_id=MERCHANT_1,
            )
        client.put_opening_hours([], merchant_id=MERCHANT_1)
    assert writes == [f"/merchant/v1.0/merchants/{MERCHANT_1}/opening-hours"]


def test_timeout_is_uncertain_and_readonly_reconciliation_prevents_retry(tmp_path: Path) -> None:
    cfg = replace(_make_config(tmp_path), write_capabilities=frozenset({"opening_hours"}))
    weekdays = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    live = [{"dayOfWeek": day, "start": "08:00:00", "duration": 480} for day in weekdays]
    writes = 0

    class TimeoutAfterApply:
        def __init__(self, _config: Config) -> None:
            pass

        def opening_hours(self, merchant_id=None):
            return [{"shifts": live}]

        def put_opening_hours(self, shifts, merchant_id=None):
            nonlocal writes
            writes += 1
            live[:] = shifts
            raise httpx.ReadTimeout("synthetic timeout after server applied the write")

    engine = SyncEngine(cfg, api_client_factory=TimeoutAfterApply)
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "store-a", "merchant_id": MERCHANT_1,
                      "execution_mode": "API_ONLY"}],
        hours_rows=[{"store_key": "store-a", "day_of_week": day,
                     "start_time": "09:00", "end_time": "17:00"} for day in weekdays],
    )
    planned = engine.process_snapshot(snapshot=snapshot, trigger_source="TEST", actor_id="sheets", write_back=False)
    proposal_id = planned["results"][0]["proposal_id"]
    confirmed = engine.confirm_proposal(
        proposal_id=proposal_id, actor_id="operator", override_guardrail=True, write_back=False
    )
    assert confirmed["proposal"]["status"] == "UNCERTAIN"
    assert writes == 1

    repeated = engine.process_snapshot(snapshot=snapshot, trigger_source="POLL", actor_id="poller", write_back=False)
    assert repeated["results"][0]["outcome"] == "UNCERTAIN"
    assert repeated["results"][0]["proposal_id"] == proposal_id
    assert writes == 1

    reconciled = engine.reconcile_uncertain(proposal_id=proposal_id, actor_id="operator")
    assert reconciled["outcome"] == "CONFIRMED"
    assert reconciled["proposal"]["status"] == "APPLIED"
    assert writes == 1


def test_sheets_poller_is_opt_in_and_persists_health(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert not poller_enabled(cfg)
    enabled = replace(cfg, enabled=True, sheets_poll_seconds=30)
    assert poller_enabled(enabled)

    storage = SyncStorage(cfg.db_path)
    storage.update_poller_status(
        tenant_key="default",
        last_success_at="2026-11-19T12:00:00+00:00",
        last_error="Falha de reconciliação (TimeoutException).",
        consecutive_failures=2,
        last_summary=None,
    )
    status = storage.get_poller_status("default")
    assert status["consecutive_failures"] == 2
    assert status["last_error"].startswith("Falha de reconciliação")


def test_row_error_blocks_only_affected_sheet_domain(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    engine = SyncEngine(cfg, storage=SyncStorage(cfg.db_path))
    engine._observe_current = lambda **kwargs: (kwargs["desired_payload"], True)
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "store-a", "merchant_id": MERCHANT_1,
                      "execution_mode": "API_ONLY", "sync_mode": "AUTO_APPLY"}],
        hours_rows=[
            {"store_key": "store-a", "day_of_week": "MONDAY", "start_time": "09:00", "end_time": "18:00"},
            {"store_key": "store-a", "day_of_week": "TUESDAY", "start_time": "20:00", "end_time": "18:00"},
        ],
        catalog_rows=[{"store_key": "store-a", "item_id": "item-a", "price_brl": "10,00"}],
    )
    result = engine.process_snapshot(snapshot=snapshot, trigger_source="TEST", actor_id="tester", write_back=False)
    outcomes = {item["domain"]: item["outcome"] for item in result["results"]}
    assert outcomes["OPENING_HOURS"] == "BLOCKED_GUARDRAIL"
    assert "CATALOG" not in outcomes or outcomes["CATALOG"] != "BLOCKED_GUARDRAIL"
def test_google_service_account_jwt_and_sheets_roundtrip(tmp_path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, str(request.url)))
        if "oauth2.googleapis.com/token" in str(request.url):
            assert b"jwt-bearer" in request.content
            return httpx.Response(200, json={"access_token": "goog-jwt-tok", "expires_in": 3600})
        if "values:batchGet" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "valueRanges": [
                        {"range": "Lojas!A1:P2", "values": [["store_key"], ["loja-a"]]},
                        {"range": "Horarios!A1:F1", "values": [["store_key", "day_of_week", "start_time", "end_time"]]},
                        {"range": "Cardapio!A1:K1", "values": [["store_key", "item_id", "price_brl"]]},
                    ]
                },
            )
        if ":append" in str(request.url):
            return httpx.Response(200, json={"updates": {"updatedRows": 1}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    token = mint_service_account_token(
        {
            "client_email": "hermes-sync@test.iam.gserviceaccount.com",
            "private_key": pem,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        transport=transport,
        now_epoch=1700000000,
    )
    assert token == "goog-jwt-tok"

    cfg = _make_config(tmp_path, google_token="goog-jwt-tok")
    client = GoogleSheetsClient(cfg, transport=transport)
    tables = client.fetch_snapshot_tables()
    assert tables["Lojas"][1] == ["loja-a"]
    client.write_sync_feedback(audit_rows=[["2026-11-19", "PULL", "loja-a", "CATALOG", "IFOOD_API", "CONFIRMED", "OK", ""]])
    assert any(":append" in url for _, url in captured)


def test_sync_engine_forces_review_and_blocks_unvalidated_capabilities(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    weekdays = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    existing_shifts = [
        {"dayOfWeek": day, "start": "08:00:00", "duration": 480} for day in weekdays
    ]
    api_state: dict[str, Any] = {"shifts": existing_shifts, "writes": []}

    class FakeIfoodApi:
        def __init__(self, _config: Config) -> None:
            pass

        def opening_hours(self, merchant_id=None):
            return [{"shifts": api_state["shifts"]}]

        def put_opening_hours(self, shifts, merchant_id=None):
            api_state["writes"].append((merchant_id, shifts))
            api_state["shifts"] = shifts

        def list_catalogs(self, merchant_id=None):
            return [{"id": "catalog-a"}]

        def list_catalog_items(self, catalog_id, merchant_id=None):
            return [{"id": "item-a", "name": "Burger", "description": None,
                     "price": {"value": 30.0}, "status": "AVAILABLE"}]

        def list_interruptions(self, merchant_id=None):
            return []

    engine = SyncEngine(cfg, storage=storage, api_client_factory=FakeIfoodApi)
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "loja-a", "channel": "IFOOD", "merchant_id": MERCHANT_1,
                      "execution_mode": "API_ONLY", "sync_mode": "AUTO_APPLY",
                      "status_action": "OPEN"}],
        hours_rows=[{"store_key": "loja-a", "day_of_week": day,
                     "start_time": "09:00", "end_time": "18:00"} for day in weekdays],
        catalog_rows=[{"store_key": "loja-a", "item_id": "item-a", "item_name": "Burger",
                       "price_brl": "36,00", "available": True}],
    )
    assert snapshot.stores["loja-a"].store.sync_mode.value == "REVIEW"
    result = engine.process_snapshot(snapshot=snapshot, trigger_source="WEBHOOK", actor_id="sheets")
    by_domain = {row["domain"]: row for row in result["results"]}

    assert by_domain["OPENING_HOURS"]["outcome"] == "BLOCKED_GUARDRAIL"
    assert by_domain["CATALOG"]["outcome"] == "BLOCKED_GUARDRAIL"
    # Interruption absence cannot prove the merchant is open/reopenable.
    assert by_domain["STORE_STATUS"]["outcome"] == "BLOCKED_GUARDRAIL"
    assert api_state["writes"] == []

    opening_proposal = by_domain["OPENING_HOURS"]["proposal_id"]
    with pytest.raises(PermissionError, match="Capacidade de escrita"):
        engine.confirm_proposal(proposal_id=opening_proposal, actor_id="operator", override_guardrail=True)
    assert api_state["writes"] == []

    repeated = engine.process_snapshot(snapshot=snapshot, trigger_source="POLL", actor_id="poller")
    repeated_opening = next(row for row in repeated["results"] if row["domain"] == "OPENING_HOURS")
    assert repeated_opening["proposal_id"] == opening_proposal
    assert api_state["writes"] == []
