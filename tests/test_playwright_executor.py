"""Testes do executor Playwright de fallback (SSRF, bloqueio login/CAPTCHA, leitura, proposta e escrita com read-back)."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs

import pytest

from dashboard import plugin_api
from ifood.config import Config, TenantConfig
from ifood.models import ChangeDomain, SheetStoreConfig, StoreChannel, SyncOutcome
from ifood.playwright_executor import (
    PlaywrightStoreExecutor,
    validate_cdp_url,
    validate_portal_url,
)
from ifood.portal_selectors import get_portal_selectors
from ifood.sheets_parser import parse_sheet_snapshot
from ifood.storage import SyncStorage
from ifood.sync_engine import (
    SyncEngine,
    TenantMismatchError,
    build_portal_session_overview,
)

MERCHANT_1 = "550e8400-e29b-41d4-a716-446655440000"
MERCHANT_OTHER = "99999999-e29b-41d4-a716-446655440099"


class _MockPortalHandler(BaseHTTPRequestHandler):
    state: ClassVar[dict[str, Any]] = {
        "merchant_id": MERCHANT_1,
        "store_name": "Padaria Centro",
        "prep_time_minutes": "20",
        "store_status": "OPEN",
        "item_name": "Pão de Queijo",
        "price_brl": "29.90",
        "available": True,
        "ignore_writes": False,
        "omit_price_field": False,
        "second_item": False,
    }

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path.startswith("/login"):
            body = (
                b"<html><title>Partner Login</title>"
                b"<body><form><input type='password'/></form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/captcha"):
            body = (
                b"<html><title>Just a moment...</title>"
                b"<body>cf-turnstile verify you are human</body></html>"
            )
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/portal"):
            checked = "checked" if self.state["available"] else ""
            price_input_html = (
                ""
                if self.state.get("omit_price_field")
                else f'<input data-hermes-field="price_brl" value="{self.state["price_brl"]}" />'
            )
            second_html = ""
            if self.state.get("second_item"):
                second_checked = "checked" if self.state.get("second_available", True) else ""
                second_html = f'''
                <div data-hermes-item-id="item-2">
                  <span data-hermes-field="item_name">Empada</span>
                  <input data-hermes-field="price_brl" value="{self.state.get('second_price_brl', '12.00')}" />
                  <input type="checkbox" data-hermes-field="available" {second_checked} />
                  <button data-hermes-action="save-item"
                          onclick="fetch('/save?item=2&price=' + this.parentElement.querySelector('[data-hermes-field=price_brl]').value + '&avail=' + (this.parentElement.querySelector('[data-hermes-field=available]').checked ? '1' : '0'))">Salvar Item</button>
                </div>'''
            html = f"""
            <html>
              <head><title>Portal Parceiro Mock</title></head>
              <body>
                <div data-hermes-merchant-id="{self.state['merchant_id']}"
                     data-hermes-store-name="{self.state['store_name']}"></div>
                <input data-hermes-field="prep_time_minutes" value="{self.state['prep_time_minutes']}" />
                <button data-hermes-action="save-prep-time"
                        onclick="fetch('/save?prep=' + document.querySelector('[data-hermes-field=prep_time_minutes]').value)">Salvar Preparo</button>

                <div data-hermes-item-id="item-1">
                  <span data-hermes-field="item_name">{self.state['item_name']}</span>
                  {price_input_html}
                  <input type="checkbox" data-hermes-field="available" {checked} />
                  <button data-hermes-action="save-item"
                          onclick="fetch('/save?price=' + (this.parentElement.querySelector('[data-hermes-field=price_brl]') ? this.parentElement.querySelector('[data-hermes-field=price_brl]').value : '') + '&avail=' + (this.parentElement.querySelector('[data-hermes-field=available]').checked ? '1' : '0'))">Salvar Item</button>
                </div>
                {second_html}
              </body>
            </html>
            """
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if self.path.startswith("/save"):
            if not self.state.get("ignore_writes"):
                qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                if "prep" in qs:
                    self.state["prep_time_minutes"] = qs["prep"][0]
                if "price" in qs and qs["price"][0]:
                    self.state["second_price_brl" if qs.get("item") == ["2"] else "price_brl"] = qs["price"][0]
                if "avail" in qs:
                    self.state["second_available" if qs.get("item") == ["2"] else "available"] = qs["avail"][0] == "1"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


@pytest.fixture()
def mock_portal():
    _MockPortalHandler.state.update(
        {
            "merchant_id": MERCHANT_1,
            "store_name": "Padaria Centro",
            "prep_time_minutes": "20",
            "store_status": "OPEN",
            "item_name": "Pão de Queijo",
            "price_brl": "29.90",
            "available": True,
            "ignore_writes": False,
            "omit_price_field": False,
            "second_item": False,
            "second_price_brl": "12.00",
            "second_available": True,
        }
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockPortalHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _make_playwright_config(
    tmp_path: Path,
    *,
    validated_domains: frozenset[str] = frozenset({"CATALOG", "PREP_TIME"}),
    write_capabilities: frozenset[str] = frozenset({"catalog_price", "prep_time"}),
    tenants: dict[str, TenantConfig] | None = None,
) -> Config:
    return Config(
        True,
        frozenset({"operator"}),
        MERCHANT_1,
        "https://merchant-api.ifood.com.br",
        "",
        data_dir=str(tmp_path),
        allowed_portal_domains=("127.0.0.1",),
        allow_http_for_tests=True,
        playwright_headless=True,
        playwright_user_data_dir="",
        validated_portal_domains=validated_domains,
        write_capabilities=write_capabilities,
        tenants=tenants or {},
    )


def test_portal_and_cdp_allowlists_prevent_ssrf() -> None:
    cfg = Config(True, frozenset({"op"}), "", "https://merchant-api.ifood.com.br", "")
    assert (
        validate_portal_url("https://portal.ifood.com.br/store", cfg)
        == "https://portal.ifood.com.br/store"
    )
    with pytest.raises(ValueError, match="Esquema"):
        validate_portal_url("http://portal.ifood.com.br/store", cfg)
    with pytest.raises(ValueError, match="allowlist"):
        validate_portal_url("https://evil.example.com", cfg)
    assert validate_cdp_url("http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    with pytest.raises(ValueError, match="loopback"):
        validate_cdp_url("http://10.0.0.5:9222")


def test_playwright_executor_fails_closed_without_validated_identity(tmp_path: Path) -> None:
    cfg = Config(
        True,
        frozenset({"op"}),
        "",
        "https://merchant-api.ifood.com.br",
        "",
        data_dir=str(tmp_path),
        allowed_portal_domains=("127.0.0.1",),
        allow_http_for_tests=True,
    )
    executor = PlaywrightStoreExecutor(cfg)
    store_99 = SheetStoreConfig(
        store_key="loja-99",
        channel=StoreChannel.FOOD99,
        portal_url="http://127.0.0.1:5089/portal",
    )
    result_99 = executor.execute_domain_update(
        store=store_99,
        domain=ChangeDomain.PREP_TIME,
        proposed_payload={"prep_time_minutes": 35},
    )
    assert result_99["outcome"] == SyncOutcome.UNSUPPORTED.value
    assert "validação específica" in result_99["message"]

    store_ifood = SheetStoreConfig(
        store_key="loja-a",
        channel=StoreChannel.IFOOD,
        merchant_id=MERCHANT_1,
        portal_url="http://127.0.0.1:5089/portal",
    )
    result_ifood = executor.execute_domain_update(
        store=store_ifood,
        domain=ChangeDomain.PREP_TIME,
        proposed_payload={"prep_time_minutes": 35},
    )
    assert result_ifood["outcome"] == SyncOutcome.UNSUPPORTED.value
    assert "validação específica" in result_ifood["message"]
    assert _MockPortalHandler.state["prep_time_minutes"] == "20"


def test_playwright_read_generates_pending_proposal_with_current_and_audit(
    tmp_path: Path, mock_portal: str
) -> None:
    """1. leitura gera proposta: mock com cardápio, backend PLAYWRIGHT ⇒ proposta PENDING criada,
    current preenchido, auditoria PENDING_REVIEW.
    """
    cfg = _make_playwright_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/portal",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,50",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )

    result = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    assert len(result["results"]) == 1
    item_res = result["results"][0]
    assert item_res["backend_used"] == "PLAYWRIGHT"
    assert item_res["outcome"] == SyncOutcome.PENDING_REVIEW.value
    assert item_res["proposal_id"] is not None

    proposal = storage.get_proposal(item_res["proposal_id"], tenant_key="padaria-a")
    assert proposal is not None
    assert proposal["status"] == "PENDING"
    assert proposal["backend_planned"] == "PLAYWRIGHT"
    assert proposal["current"] == [
        {
            "item_id": "item-1",
            "item_name": "Pão de Queijo",
            "description": None,
            "price_cents": 2990,
            "original_price_cents": None,
            "available": True,
        }
    ]
    assert proposal["proposed"][0]["price_cents"] == 3250

    audits = storage.list_audit_events("padaria-a")
    assert len(audits) == 1
    assert audits[0]["outcome"] == SyncOutcome.PENDING_REVIEW.value
    assert audits[0]["backend_used"] == "PLAYWRIGHT"

    overview_sessions = build_portal_session_overview(cfg, storage, tenant_key="padaria-a")
    assert overview_sessions["padaria-a"]["IFOOD"]["state"] == "OK"
    assert overview_sessions["padaria-a"]["IFOOD"]["last_error"] is None


def test_playwright_portal_with_login_returns_login_required_without_proposal(
    tmp_path: Path, mock_portal: str
) -> None:
    """2. portal com login ⇒ LOGIN_REQUIRED, nenhuma proposta criada, nada gravado."""
    cfg = _make_playwright_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/login",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,50",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )

    result = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    assert len(result["results"]) == 1
    item_res = result["results"][0]
    assert item_res["outcome"] == SyncOutcome.LOGIN_REQUIRED.value
    assert item_res["proposal_id"] is None
    assert storage.list_proposals("padaria-a") == []
    assert storage.list_snapshots("padaria-a") == []

    overview_sessions = build_portal_session_overview(cfg, storage, tenant_key="padaria-a")
    assert overview_sessions["padaria-a"]["IFOOD"]["state"] == "LOGIN_REQUIRED"
    assert overview_sessions["padaria-a"]["IFOOD"]["last_error"]


def test_playwright_page_missing_field_blocks_guardrail_with_explicit_reason(
    tmp_path: Path, mock_portal: str
) -> None:
    """3. página sem o campo ⇒ observado=False, BLOCKED_GUARDRAIL com motivo explícito."""
    _MockPortalHandler.state["omit_price_field"] = True
    cfg = _make_playwright_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    executor = PlaywrightStoreExecutor(cfg)
    store_cfg = SheetStoreConfig(
        tenant_key="padaria-a",
        store_key="padaria-centro",
        channel=StoreChannel.IFOOD,
        merchant_id=MERCHANT_1,
        store_name="Padaria Centro",
        portal_url=f"{mock_portal}/portal",
    )
    direct_read = executor.read_domain_state(
        store=store_cfg,
        domain=ChangeDomain.CATALOG,
        desired_payload=[{"item_id": "item-1", "price_cents": 3250, "available": True}],
    )
    assert direct_read["observed"] is False
    assert "Campo de preço" in direct_read["reason"]
    assert "item-1" in direct_read["reason"]

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/portal",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,50",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )
    result = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    item_res = result["results"][0]
    assert item_res["outcome"] == SyncOutcome.BLOCKED_GUARDRAIL.value
    assert item_res["proposal_id"] is None
    assert "Campo de preço" in item_res["message"]
    assert "item-1" in item_res["message"]
    assert storage.list_proposals("padaria-a") == []

    audits = storage.list_audit_events("padaria-a")
    assert len(audits) == 1
    assert audits[0]["outcome"] == SyncOutcome.BLOCKED_GUARDRAIL.value
    assert "Campo de preço" in audits[0]["summary"]


def test_playwright_divergent_store_identity_returns_explicit_error_without_proposal(
    tmp_path: Path, mock_portal: str
) -> None:
    """4. identidade divergente (mock de outra loja) ⇒ erro explícito, sem proposta."""
    _MockPortalHandler.state["merchant_id"] = MERCHANT_OTHER
    cfg = _make_playwright_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/portal",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,50",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )
    result = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    item_res = result["results"][0]
    assert item_res["proposal_id"] is None
    assert "Identidade da loja divergente" in item_res["message"]
    assert MERCHANT_1 in item_res["message"]
    assert MERCHANT_OTHER in item_res["message"]
    assert storage.list_proposals("padaria-a") == []


def test_playwright_write_confirmed_updates_mock_and_verifies_readback(
    tmp_path: Path, mock_portal: str
) -> None:
    """5. escrita confirmada: proposta aprovada + mock que grava ⇒ CONFIRMED e releitura igual ao proposto."""
    cfg = _make_playwright_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/portal",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "34,90",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )
    planned = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    proposal_id = planned["results"][0]["proposal_id"]
    assert proposal_id is not None

    confirmed = engine.confirm_proposal(
        proposal_id=proposal_id,
        actor_id="operator",
        tenant_key="padaria-a",
        write_back=False,
    )
    assert confirmed["execution"]["outcome"] == SyncOutcome.CONFIRMED.value
    assert confirmed["proposal"]["status"] == "APPLIED"
    assert confirmed["execution"]["observed"][0]["price_cents"] == 3490
    assert _MockPortalHandler.state["price_brl"] == "34.90"


def test_auto_apply_availability_from_sheet_with_readback(tmp_path: Path, mock_portal: str) -> None:
    cfg = replace(
        _make_playwright_config(tmp_path), default_sync_mode="AUTO_APPLY",
        write_capabilities=frozenset({"catalog_availability"}),
        tenants={"padaria-a": TenantConfig(
            "padaria-a", stores={"padaria-centro": {
                "merchant_id": MERCHANT_1, "channel": "IFOOD",
                "write_capabilities": ["catalog_availability"],
            }}
        )},
    )
    storage = SyncStorage(cfg.db_path)
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "padaria-centro", "merchant_id": MERCHANT_1,
                      "store_name": "Padaria Centro", "execution_mode": "PLAYWRIGHT_ONLY",
                      "portal_url": f"{mock_portal}/portal"}],
        catalog_rows=[{"store_key": "padaria-centro", "item_id": "item-1",
                       "item_name": "Pão de Queijo", "price_brl": "29,90", "available": False}],
        tenant_key="padaria-a", default_sync_mode="AUTO_APPLY",
    )
    result = SyncEngine(cfg, storage=storage).process_snapshot(
        snapshot=snapshot, trigger_source="SHEETS_POLLER", actor_id="google-sheets-poller",
        tenant_key="padaria-a", write_back=False,
    )
    assert result["results"][0]["outcome"] == "CONFIRMED"
    assert _MockPortalHandler.state["available"] is False
    proposal = storage.get_proposal(result["results"][0]["proposal_id"], tenant_key="padaria-a")
    assert proposal["status"] == "APPLIED"


def test_playwright_catalog_batch_reads_back_each_item(tmp_path: Path, mock_portal: str) -> None:
    _MockPortalHandler.state["second_item"] = True
    cfg = replace(_make_playwright_config(tmp_path),
                  write_capabilities=frozenset({"catalog_price", "catalog_availability"}))
    engine = SyncEngine(cfg, storage=SyncStorage(cfg.db_path))
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "padaria-centro", "merchant_id": MERCHANT_1,
                      "store_name": "Padaria Centro", "execution_mode": "PLAYWRIGHT_ONLY",
                      "portal_url": f"{mock_portal}/portal"}],
        catalog_rows=[
            {"store_key": "padaria-centro", "item_id": "item-1", "item_name": "Pão de Queijo",
             "price_brl": "29,90", "available": False},
            {"store_key": "padaria-centro", "item_id": "item-2", "item_name": "Empada",
             "price_brl": "13,00", "available": True},
        ], tenant_key="padaria-a",
    )
    planned = engine.process_snapshot(snapshot=snapshot, trigger_source="TEST",
                                      actor_id="sheets", tenant_key="padaria-a", write_back=False)
    confirmed = engine.confirm_proposal(proposal_id=planned["results"][0]["proposal_id"],
                                        actor_id="operator", tenant_key="padaria-a", write_back=False)
    assert confirmed["execution"]["outcome"] == "CONFIRMED"
    assert len(confirmed["execution"]["item_results"]) == 2
    observed_ids = [row["item_id"] for row in confirmed["execution"]["observed"]]
    assert observed_ids == ["item-1", "item-2"]
    assert confirmed["execution"]["observed"][0]["available"] is False
    assert confirmed["execution"]["observed"][1]["price_cents"] == 1300
    assert _MockPortalHandler.state["available"] is False
    assert _MockPortalHandler.state["second_price_brl"] == "13.00"


def test_99food_mock_uses_own_channel_and_store_capability(tmp_path: Path, mock_portal: str) -> None:
    cfg = replace(
        _make_playwright_config(tmp_path), default_sync_mode="AUTO_APPLY",
        write_capabilities=frozenset({"catalog_availability"}),
        tenants={"padaria-a": TenantConfig("padaria-a", stores={
            "padaria-centro": {"merchant_id": MERCHANT_1, "channel": "99FOOD",
                               "write_capabilities": ["catalog_availability"]},
        })},
    )
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "padaria-centro", "channel": "99FOOD",
                      "merchant_id": MERCHANT_1, "store_name": "Padaria Centro",
                      "execution_mode": "PLAYWRIGHT_ONLY", "portal_url": f"{mock_portal}/portal"}],
        catalog_rows=[{"store_key": "padaria-centro", "item_id": "item-1",
                       "item_name": "Pão de Queijo", "price_brl": "29,90", "available": False}],
        tenant_key="padaria-a", default_sync_mode="AUTO_APPLY",
    )
    result = SyncEngine(cfg).process_snapshot(
        snapshot=snapshot, trigger_source="SHEETS_POLLER", actor_id="poller",
        tenant_key="padaria-a", write_back=False,
    )
    assert result["results"][0]["outcome"] == "CONFIRMED"
    assert _MockPortalHandler.state["available"] is False


def test_external_selectors_require_version_and_homologation(tmp_path: Path) -> None:
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps({"version": 1, "channels": {
        "99FOOD": {"channel": "99FOOD", "validated_real_portal": False,
                   "notes": "captura ainda sem homologação", "merchant_id_selector": "[data-store-id]"},
    }}), encoding="utf-8")
    selectors = get_portal_selectors("99FOOD", str(path))
    assert selectors.validated_real_portal is False
    assert selectors.merchant_id_selector == "[data-store-id]"
    path.write_text(json.dumps({"version": 2, "channels": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="version=1"):
        get_portal_selectors("99FOOD", str(path))


def test_anota_ai_mock_uses_own_channel_and_store_capability(tmp_path: Path, mock_portal: str) -> None:
    cfg = replace(
        _make_playwright_config(tmp_path), default_sync_mode="AUTO_APPLY",
        write_capabilities=frozenset({"catalog_availability"}),
        tenants={"padaria-a": TenantConfig("padaria-a", stores={
            "padaria-centro": {"merchant_id": MERCHANT_1, "channel": "ANOTA_AI",
                               "write_capabilities": ["catalog_availability"]},
        })},
    )
    assert validate_portal_url("https://admin.anota.ai/loja", Config(True, frozenset(), "", "https://merchant-api.ifood.com.br", "")) == "https://admin.anota.ai/loja"
    assert validate_portal_url("https://app.anota.ai/loja", Config(True, frozenset(), "", "https://merchant-api.ifood.com.br", "")) == "https://app.anota.ai/loja"
    snapshot = parse_sheet_snapshot(
        stores_rows=[{"store_key": "padaria-centro", "channel": "ANOTA_AI",
                      "merchant_id": MERCHANT_1, "store_name": "Padaria Centro",
                      "execution_mode": "PLAYWRIGHT_ONLY", "portal_url": f"{mock_portal}/portal"}],
        catalog_rows=[{"store_key": "padaria-centro", "item_id": "item-1",
                       "item_name": "Pão de Queijo", "price_brl": "29,90", "available": False}],
        tenant_key="padaria-a", default_sync_mode="AUTO_APPLY",
    )
    result = SyncEngine(cfg).process_snapshot(
        snapshot=snapshot, trigger_source="SHEETS_POLLER", actor_id="poller",
        tenant_key="padaria-a", write_back=False,
    )
    assert result["results"][0]["outcome"] == "CONFIRMED"
    assert _MockPortalHandler.state["available"] is False


def test_browser_profile_isolated_by_tenant_store_and_channel(tmp_path: Path) -> None:
    cfg = replace(_make_playwright_config(tmp_path),
                  playwright_user_data_dir=str(tmp_path / "profiles"))
    executor = PlaywrightStoreExecutor(cfg)
    first = SheetStoreConfig(tenant_key="padaria-a", store_key="store",
                             channel=StoreChannel.IFOOD)
    other_channel = first.model_copy(update={"channel": StoreChannel.FOOD99})
    anota_channel = first.model_copy(update={"channel": StoreChannel.ANOTA_AI})
    other_tenant = first.model_copy(update={"tenant_key": "padaria-b"})
    assert len({
        executor._profile_dir(first),
        executor._profile_dir(other_channel),
        executor._profile_dir(anota_channel),
        executor._profile_dir(other_tenant),
    }) == 4


def test_playwright_write_divergent_marks_uncertain_and_locks_further_attempts(
    tmp_path: Path, mock_portal: str
) -> None:
    """6. escrita divergente: mock que ignora a gravação ⇒ UNCERTAIN, item travado para nova tentativa."""
    cfg = _make_playwright_config(tmp_path)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/portal",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "34,90",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )
    planned = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    proposal_id = planned["results"][0]["proposal_id"]
    assert proposal_id is not None

    # Configura o mock para ignorar a gravação (simulando portal que não persistiu a alteração)
    _MockPortalHandler.state["ignore_writes"] = True

    confirmed = engine.confirm_proposal(
        proposal_id=proposal_id,
        actor_id="operator",
        tenant_key="padaria-a",
        write_back=False,
    )
    assert confirmed["execution"]["outcome"] == SyncOutcome.UNCERTAIN.value
    assert confirmed["proposal"]["status"] == "UNCERTAIN"
    assert _MockPortalHandler.state["price_brl"] == "29.90"

    # Nova tentativa de sincronização para a mesma loja/domínio fica travada em UNCERTAIN até reconciliação
    repeated = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="POLL",
        actor_id="poller",
        tenant_key="padaria-a",
        write_back=False,
    )
    assert repeated["results"][0]["outcome"] == SyncOutcome.UNCERTAIN.value
    assert repeated["results"][0]["proposal_id"] == proposal_id


def test_playwright_proposal_isolated_across_tenants_and_overview_exposes_sessions(
    tmp_path: Path, mock_portal: str
) -> None:
    """7. isolamento: proposta de padaria-a não pode ser confirmada com padaria-b
    + estado de sessão visível no overview por padaria × canal.
    """
    tenants = {
        "padaria-a": TenantConfig(tenant_key="padaria-a", sheet_id="sheet-a"),
        "padaria-b": TenantConfig(tenant_key="padaria-b", sheet_id="sheet-b"),
    }
    cfg = _make_playwright_config(tmp_path, tenants=tenants)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)

    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "padaria-centro",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "store_name": "Padaria Centro",
                "execution_mode": "PLAYWRIGHT_ONLY",
                "portal_url": f"{mock_portal}/portal",
            }
        ],
        catalog_rows=[
            {
                "store_key": "padaria-centro",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "34,90",
                "available": True,
            }
        ],
        tenant_key="padaria-a",
    )
    planned = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="TEST",
        actor_id="sheets",
        tenant_key="padaria-a",
        write_back=False,
    )
    proposal_id = planned["results"][0]["proposal_id"]
    assert proposal_id is not None

    with pytest.raises(TenantMismatchError, match="padaria-b"):
        engine.confirm_proposal(
            proposal_id=proposal_id,
            actor_id="operator",
            tenant_key="padaria-b",
            write_back=False,
        )

    assert _MockPortalHandler.state["price_brl"] == "29.90"
    prop_after = storage.get_proposal(proposal_id, tenant_key="padaria-a")
    assert prop_after is not None
    assert prop_after["status"] == "PENDING"

    overview = asyncio.run(plugin_api.sync_overview(tenant_key="padaria-a", cfg=cfg))
    assert overview["portal_sessions"]["padaria-a"]["IFOOD"]["state"] == "OK"
    assert overview["portal_sessions"]["padaria-b"]["IFOOD"]["state"] == "NÃO CONFIGURADO"
    assert overview["portal_sessions"]["padaria-a"]["99FOOD"]["state"] == "NÃO CONFIGURADO"
