"""Testes da Fatia 5: Catalog v2 real, fallback API→Playwright e importador Anota AI."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from dashboard import plugin_api
from ifood.anota_ai import (
    AnotaAiReadOnlyClient,
    anota_ai_items_to_sheet_rows,
    is_anota_ai_configured,
    normalize_anota_ai_export,
    validate_anota_ai_base,
)
from ifood.client import IfoodClient
from ifood.config import Config
from ifood.models import ChangeDomain
from ifood.sheets_parser import parse_sheet_snapshot
from ifood.sync_engine import SyncEngine
from ifood.write_policy import approved_execution

MERCHANT_1 = "550e8400-e29b-41d4-a716-446655440000"


def _make_config(tmp_path: Path, **overrides: Any) -> Config:
    base = Config(
        True,
        frozenset({"operator"}),
        MERCHANT_1,
        "https://merchant-api.ifood.com.br",
        "ifood-token",
        data_dir=str(tmp_path),
    )
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# 1. Catalog v2 real em IfoodClient + SyncEngine
# ---------------------------------------------------------------------------


def test_catalog_v2_patch_price_and_status_enforce_approval_and_send_official_payload(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/merchant/v1.0/merchants":
            return httpx.Response(200, json=[{"id": MERCHANT_1}])
        calls.append((request.method, request.url.path, json.loads(request.content.decode())))
        return httpx.Response(200, json={"ok": True})

    cfg = _make_config(
        tmp_path,
        write_capabilities=frozenset({"catalog_price", "catalog_availability"}),
    )
    client = IfoodClient(cfg, transport=httpx.MockTransport(handler))

    # Bloqueado fora de approved_execution
    with pytest.raises(PermissionError, match="Escrita bloqueada"):
        client.patch_item_price(item_id="item-1", price_brl=34.9, merchant_id=MERCHANT_1)
    assert calls == []

    approved_payload = [
        {
            "item_id": "item-1",
            "item_name": "Pão de Queijo",
            "price_cents": 3490,
            "original_price_cents": 3990,
            "available": False,
        }
    ]
    with approved_execution(
        execution_id="prop-cat-1",
        merchant_id=MERCHANT_1,
        channel="IFOOD",
        domain=ChangeDomain.CATALOG,
        payload=approved_payload,
        actor_id="operator",
    ):
        # Valor divergente do aprovado deve ser recusado
        with pytest.raises(PermissionError, match="valores de escrita"):
            client.patch_item_price(item_id="item-1", price_brl=99.9, merchant_id=MERCHANT_1)

        with pytest.raises(PermissionError, match="valores de escrita"):
            client.patch_item_price(item_id="item-1", price_brl=34.90, merchant_id=MERCHANT_1)

        # Item fora da proposta deve ser recusado
        with pytest.raises(PermissionError, match="não consta na proposta"):
            client.patch_item_status(item_id="item-outro", available=False, merchant_id=MERCHANT_1)

        # Escritas válidas
        client.patch_item_price(
            item_id="item-1",
            price_brl=34.90,
            original_price_brl=39.90,
            merchant_id=MERCHANT_1,
        )
        client.patch_item_status(item_id="item-1", available=False, merchant_id=MERCHANT_1)

    assert calls == [
        (
            "PATCH",
            f"/catalog/v2.0/merchants/{MERCHANT_1}/items/price",
            {"itemId": "item-1", "price": {"value": 34.9, "originalValue": 39.9}},
        ),
        (
            "PATCH",
            f"/catalog/v2.0/merchants/{MERCHANT_1}/items/status",
            {"itemId": "item-1", "status": "UNAVAILABLE"},
        ),
    ]


def test_sync_engine_catalog_v2_end_to_end_only_patches_changed_fields(tmp_path: Path) -> None:
    live_item = {
        "id": "item-1",
        "name": "Pão de Queijo",
        "description": None,
        "price": {"value": 29.9, "originalValue": None},
        "status": "AVAILABLE",
    }
    patched: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/merchant/v1.0/merchants":
            return httpx.Response(200, json=[{"id": MERCHANT_1}])
        if request.url.path == f"/catalog/v2.0/merchants/{MERCHANT_1}/catalogs":
            return httpx.Response(200, json=[{"id": "cat-1"}])
        if request.url.path == f"/catalog/v2.0/merchants/{MERCHANT_1}/catalogs/cat-1/categories":
            return httpx.Response(200, json=[{"id": "g1", "items": [dict(live_item)]}])
        if request.url.path == f"/catalog/v2.0/merchants/{MERCHANT_1}/items/status":
            body = json.loads(request.content.decode())
            patched.append(("status", body))
            live_item["status"] = body["status"]
            return httpx.Response(200, json={"ok": True})
        if request.url.path == f"/catalog/v2.0/merchants/{MERCHANT_1}/items/price":
            body = json.loads(request.content.decode())
            patched.append(("price", body))
            live_item["price"]["value"] = body["price"]["value"]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    cfg = _make_config(
        tmp_path,
        write_capabilities=frozenset({"catalog_availability"}),
    )
    engine = SyncEngine(cfg, api_client_factory=lambda c: IfoodClient(c, transport=transport))
    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "loja-1",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "execution_mode": "API_ONLY",
            }
        ],
        catalog_rows=[
            {
                "store_key": "loja-1",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "29,90",
                "available": False,
            }
        ],
    )
    planned = engine.process_snapshot(
        snapshot=snapshot, trigger_source="TEST", actor_id="sheets", write_back=False
    )
    assert planned["results"][0]["outcome"] == "PENDING_REVIEW"
    proposal_id = planned["results"][0]["proposal_id"]

    confirmed = engine.confirm_proposal(
        proposal_id=proposal_id, actor_id="operator", write_back=False
    )
    assert confirmed["execution"]["outcome"] == "CONFIRMED"
    assert patched == [("status", {"itemId": "item-1", "status": "UNAVAILABLE"})]


# ---------------------------------------------------------------------------
# 2. Fallback automático API → Playwright em erro de autorização (401/403)
# ---------------------------------------------------------------------------


def test_api_first_falls_back_to_playwright_on_api_403_during_read_and_confirm(
    tmp_path: Path,
) -> None:
    def api_403_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request, json={"message": "Forbidden"})

    transport = httpx.MockTransport(api_403_handler)
    pw_calls: list[str] = []

    class FakePlaywright:
        def __init__(self, _cfg: Config) -> None:
            self.price_cents = 2990

        def read_domain_state(self, *, store, domain, desired_payload=None):
            pw_calls.append("read")
            return {
                "state": [
                    {
                        "item_id": "item-1",
                        "item_name": "Pão de Queijo",
                        "description": None,
                        "price_cents": self.price_cents,
                        "original_price_cents": None,
                        "available": True,
                    }
                ],
                "observed": True,
                "outcome": "CONFIRMED",
                "reason": None,
                "session_status": "OK",
            }

        def execute_domain_update(self, *, store, domain, proposed_payload):
            pw_calls.append("write")
            self.price_cents = proposed_payload[0]["price_cents"]
            return {
                "outcome": "CONFIRMED",
                "message": "Aplicado via fallback Playwright.",
                "observed": proposed_payload,
            }

    pw_instance = FakePlaywright(_make_config(tmp_path))
    cfg = _make_config(
        tmp_path,
        write_capabilities=frozenset({"catalog_price"}),
        validated_portal_domains=frozenset({"CATALOG"}),
    )
    engine = SyncEngine(
        cfg,
        api_client_factory=lambda c: IfoodClient(c, transport=transport),
        playwright_factory=lambda _c: pw_instance,
    )
    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "loja-1",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "execution_mode": "API_FIRST",
                "portal_url": "https://portal.ifood.com.br/loja-1",
            }
        ],
        catalog_rows=[
            {
                "store_key": "loja-1",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,00",
                "available": True,
            }
        ],
    )
    planned = engine.process_snapshot(
        snapshot=snapshot, trigger_source="TEST", actor_id="sheets", write_back=False
    )
    row = planned["results"][0]
    assert row["outcome"] == "PENDING_REVIEW"
    assert row["backend_used"] == "PLAYWRIGHT"
    assert "read" in pw_calls

    confirmed = engine.confirm_proposal(
        proposal_id=row["proposal_id"], actor_id="operator", write_back=False
    )
    assert confirmed["execution"]["outcome"] == "CONFIRMED"
    assert "write" in pw_calls


def test_api_first_falls_back_to_playwright_on_write_403_before_mutation(tmp_path: Path) -> None:
    pw_writes: list[Any] = []

    class ApiReadsThen403OnWrite:
        def __init__(self, _cfg: Config) -> None:
            pass

        def list_catalogs(self, merchant_id=None):
            return [{"id": "cat-1"}]

        def list_catalog_items(self, catalog_id, merchant_id=None):
            return [
                {
                    "id": "item-1",
                    "name": "Pão de Queijo",
                    "description": None,
                    "price": {"value": 29.9},
                    "status": "AVAILABLE",
                }
            ]

        def patch_item_price(self, **kwargs):
            req = httpx.Request("PATCH", "https://merchant-api.ifood.com.br/catalog/v2.0/items/price")
            resp = httpx.Response(403, request=req)
            raise httpx.HTTPStatusError("403 Forbidden", request=req, response=resp)

    class FakePlaywright:
        def __init__(self, _cfg: Config) -> None:
            pass

        def execute_domain_update(self, *, store, domain, proposed_payload):
            pw_writes.append(proposed_payload)
            return {
                "outcome": "CONFIRMED",
                "message": "Aplicado via fallback Playwright após 403 da API.",
                "observed": proposed_payload,
            }

    cfg = _make_config(
        tmp_path,
        write_capabilities=frozenset({"catalog_price"}),
        validated_portal_domains=frozenset({"CATALOG"}),
    )
    engine = SyncEngine(
        cfg,
        api_client_factory=ApiReadsThen403OnWrite,
        playwright_factory=FakePlaywright,
    )
    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "loja-1",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "execution_mode": "API_FIRST",
                "portal_url": "https://portal.ifood.com.br/loja-1",
            }
        ],
        catalog_rows=[
            {
                "store_key": "loja-1",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,00",
                "available": True,
            }
        ],
    )
    planned = engine.process_snapshot(
        snapshot=snapshot, trigger_source="TEST", actor_id="sheets", write_back=False
    )
    assert planned["results"][0]["backend_used"] == "IFOOD_API"

    confirmed = engine.confirm_proposal(
        proposal_id=planned["results"][0]["proposal_id"],
        actor_id="operator",
        write_back=False,
    )
    assert confirmed["execution"]["outcome"] == "CONFIRMED"
    assert confirmed["execution"]["fallback_backend"] == "PLAYWRIGHT"
    assert len(pw_writes) == 1
    snap = engine.storage.get_snapshot("loja-1", "CATALOG", tenant_key="default")
    assert snap is not None and snap["last_backend"] == "PLAYWRIGHT"


def test_api_only_never_falls_back_to_playwright_on_403(tmp_path: Path) -> None:
    def api_403_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, request=request)

    pw_called = False

    class NeverPlaywright:
        def __init__(self, _cfg: Config) -> None:
            pass

        def read_domain_state(self, **kwargs):
            nonlocal pw_called
            pw_called = True
            return {"observed": True, "state": []}

    cfg = _make_config(
        tmp_path,
        write_capabilities=frozenset({"catalog_price"}),
        validated_portal_domains=frozenset({"CATALOG"}),
    )
    engine = SyncEngine(
        cfg,
        api_client_factory=lambda c: IfoodClient(c, transport=httpx.MockTransport(api_403_handler)),
        playwright_factory=NeverPlaywright,
    )
    snapshot = parse_sheet_snapshot(
        stores_rows=[
            {
                "store_key": "loja-1",
                "channel": "IFOOD",
                "merchant_id": MERCHANT_1,
                "execution_mode": "API_ONLY",
                "portal_url": "https://portal.ifood.com.br/loja-1",
            }
        ],
        catalog_rows=[
            {
                "store_key": "loja-1",
                "item_id": "item-1",
                "item_name": "Pão de Queijo",
                "price_brl": "32,00",
                "available": True,
            }
        ],
    )
    res = engine.process_snapshot(
        snapshot=snapshot, trigger_source="TEST", actor_id="sheets", write_back=False
    )
    assert res["results"][0]["outcome"] == "BLOCKED_GUARDRAIL"
    assert pw_called is False


# ---------------------------------------------------------------------------
# 3. Importador somente-leitura da Anota AI (simple-item/export)
# ---------------------------------------------------------------------------


def test_anota_ai_rejects_untrusted_hosts_and_missing_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Host da API Anota AI inválido"):
        validate_anota_ai_base("http://api-parceiros.anota.ai")
    with pytest.raises(ValueError, match="Host da API Anota AI inválido"):
        validate_anota_ai_base("https://evil.example.com")
    with pytest.raises(ValueError, match="IFOOD_LOJA_ANOTA_AI_TOKEN"):
        AnotaAiReadOnlyClient(_make_config(tmp_path))


def test_anota_ai_export_readonly_and_sheet_conversion(tmp_path: Path, monkeypatch) -> None:
    sample_export = {
        "info": {
            "data": [
                {
                    "title": "Salgados",
                    "itens": [
                        {
                            "id": "anota-02",
                            "title": "Coxinha",
                            "description": "Frango com catupiry",
                            "price": 8.5,
                            "available": True,
                        },
                        {
                            "id": "anota-01",
                            "title": "Pão de Queijo",
                            "description": "Tradicional",
                            "price": {"value": "6,00", "originalValue": "7,50"},
                            "status": "UNAVAILABLE",
                        },
                    ],
                }
            ]
        }
    }
    captured_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(
            (request.method, str(request.url), request.headers.get("Authorization", ""))
        )
        return httpx.Response(200, json=sample_export)

    cfg = _make_config(tmp_path, anota_ai_api_token="anota-secret-token")
    assert is_anota_ai_configured(cfg) is True

    client = AnotaAiReadOnlyClient(cfg, transport=httpx.MockTransport(handler))
    items = client.export_simple_items()
    assert captured_requests == [
        (
            "GET",
            "https://api-parceiros.anota.ai/partnerauth/v1/simple-item/export",
            "anota-secret-token",
        )
    ]
    assert [it["item_id"] for it in items] == ["anota-01", "anota-02"]
    assert items[0]["price_cents"] == 600
    assert items[0]["original_price_cents"] == 750
    assert items[0]["available"] is False
    assert items[1]["price_cents"] == 850
    assert items[1]["available"] is True

    rows = anota_ai_items_to_sheet_rows("padaria-anota", items)
    assert rows[0] == [
        "store_key",
        "item_id",
        "item_name",
        "description",
        "price_brl",
        "original_price_brl",
        "available",
    ]
    assert rows[1] == ["padaria-anota", "anota-01", "Pão de Queijo", "Tradicional", "6,00", "7,50", "NAO"]
    assert rows[2] == ["padaria-anota", "anota-02", "Coxinha", "Frango com catupiry", "8,50", "", "SIM"]

    # Round-trip: linhas geradas devem ser 100% válidas no parse_sheet_snapshot
    snap = parse_sheet_snapshot(
        stores_rows=[{"store_key": "padaria-anota", "channel": "ANOTA_AI", "execution_mode": "DISABLED"}],
        catalog_rows=rows,
    )
    assert snap.row_errors == []
    assert len(snap.stores["padaria-anota"].catalog_items) == 2

    # Config.from_env carrega IFOOD_LOJA_ANOTA_AI_TOKEN
    monkeypatch.setenv("IFOOD_LOJA_ANOTA_AI_TOKEN", "env-token-123")
    monkeypatch.setenv("IFOOD_LOJA_DATA_DIR", str(tmp_path))
    env_cfg = Config.from_env()
    assert env_cfg.anota_ai_api_token == "env-token-123"
    assert normalize_anota_ai_export(sample_export) == items


@pytest.mark.asyncio
async def test_plugin_api_anota_ai_export_route(tmp_path: Path, monkeypatch) -> None:
    cfg = _make_config(tmp_path, anota_ai_api_token="tok-anota")
    monkeypatch.setattr(
        plugin_api.AnotaAiReadOnlyClient,
        "export_simple_items",
        lambda self: [
            {
                "item_id": "a1",
                "item_name": "Bolo de Cenoura",
                "description": "Com calda",
                "category": "Bolos",
                "price_cents": 2500,
                "original_price_cents": None,
                "available": True,
            }
        ],
    )
    res = await plugin_api.export_anota_ai_menu(store_key="padaria-1", cfg=cfg)
    assert res["read_only"] is True
    assert res["channel"] == "ANOTA_AI"
    assert res["total_items"] == 1
    assert res["cardapio_rows"][1][1] == "a1"


def test_overview_exposes_auto_apply(tmp_path: Path) -> None:
    import asyncio

    cfg = _make_config(tmp_path, default_sync_mode="AUTO_APPLY")
    overview = asyncio.run(plugin_api.sync_overview(tenant_key="default", cfg=cfg))
    assert overview["readiness"]["default_sync_mode"] == "AUTO_APPLY"
    assert overview["readiness"]["automation_policy"] == "AUTO_APPLY"


def test_reconcile_uses_actual_fallback_backend(tmp_path: Path, monkeypatch) -> None:
    from ifood.models import BackendUsed, canonical_digest

    cfg = _make_config(tmp_path, validated_portal_domains=frozenset({"CATALOG"}))
    engine = SyncEngine(cfg)
    proposed = [{"item_id": "x", "price_cents": 100}]
    prop = {"status": "UNCERTAIN", "metadata": {"portal_url": "https://portal.ifood.com.br"},
            "store_key": "loja", "channel": "IFOOD", "merchant_id": MERCHANT_1,
            "execution_mode": "API_FIRST", "domain": "CATALOG", "backend_planned": "IFOOD_API",
            "outcome": {"fallback_backend": "PLAYWRIGHT"}, "proposed": proposed,
            "current_hash": "old", "proposed_hash": canonical_digest(proposed)}
    monkeypatch.setattr(engine, "_load_proposal_for_tenant", lambda *_: prop)
    seen = []
    def observe(**kwargs):
        seen.append(kwargs["backend"])
        return proposed, True
    monkeypatch.setattr(engine, "_observe_current", observe)
    monkeypatch.setattr(engine.storage, "resolve_proposal", lambda *args, **kwargs: {"status": kwargs["status"]})
    result = engine.reconcile_uncertain(proposal_id="p", actor_id="operator")
    assert seen == [BackendUsed.PLAYWRIGHT]
    assert result["outcome"] == "CONFIRMED"


def test_reconcile_api_auth_error_uses_validated_portal(tmp_path: Path, monkeypatch) -> None:
    from ifood.models import BackendUsed, canonical_digest

    cfg = _make_config(tmp_path, validated_portal_domains=frozenset({"CATALOG"}))
    engine = SyncEngine(cfg)
    proposed = [{"item_id": "x", "price_cents": 100}]
    prop = {"status": "UNCERTAIN", "metadata": {"portal_url": "https://portal.ifood.com.br"},
            "store_key": "loja", "channel": "IFOOD", "merchant_id": MERCHANT_1,
            "execution_mode": "API_FIRST", "domain": "CATALOG", "backend_planned": "IFOOD_API",
            "outcome": {}, "proposed": proposed, "current_hash": "old",
            "proposed_hash": canonical_digest(proposed)}
    monkeypatch.setattr(engine, "_load_proposal_for_tenant", lambda *_: prop)
    seen = []
    def observe(**kwargs):
        seen.append(kwargs["backend"])
        if kwargs["backend"] == BackendUsed.IFOOD_API:
            engine._last_observation_detail = {"api_auth_error": True}
            return None, False
        return proposed, True
    monkeypatch.setattr(engine, "_observe_current", observe)
    monkeypatch.setattr(engine.storage, "resolve_proposal", lambda *args, **kwargs: {"status": kwargs["status"]})
    result = engine.reconcile_uncertain(proposal_id="p", actor_id="operator")
    assert seen == [BackendUsed.IFOOD_API, BackendUsed.PLAYWRIGHT]
    assert result["outcome"] == "CONFIRMED"
