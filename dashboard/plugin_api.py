"""Rotas administrativas e webhook Google Sheets do plugin iFood/99Food no Hermes."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from ifood.anota_ai import (
    AnotaAiReadOnlyClient,
    anota_ai_items_to_sheet_rows,
    is_anota_ai_configured,
)
from ifood.client import IfoodClient, has_ifood_api_credentials
from ifood.config import Config
from ifood.operations import prepare
from ifood.playwright_executor import PlaywrightStoreExecutor
from ifood.poller import get_poller_runtime_state
from ifood.sheets_client import GoogleSheetsClient, is_sheets_api_configured
from ifood.sheets_parser import parse_sheet_snapshot, verify_webhook_signature
from ifood.storage import SyncStorage
from ifood.sync_engine import SyncEngine, build_portal_session_overview

router = APIRouter()


class ProposalBody(BaseModel):
    shifts: list[dict[str, Any]] = Field(min_length=1, max_length=56)


class ConfirmBody(BaseModel):
    proposal_id: str


class SheetsSyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stores_rows: list[Any] | None = None
    hours_rows: list[Any] | None = None
    catalog_rows: list[Any] | None = None
    dry_run: bool = False
    tenant_key: str | None = None


class SyncConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    override_guardrail: bool = False
    tenant_key: str | None = None


class SyncRejectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = ""
    tenant_key: str | None = None


def _tenant(cfg: Config, requested: str | None) -> str:
    if requested is None:
        if len(cfg.tenants) != 1:
            raise HTTPException(400, "tenant_key obrigatório quando há múltiplas padarias")
        return next(iter(cfg.tenants))
    key = requested.strip()
    if not key or key not in cfg.tenants:
        raise HTTPException(400, "tenant_key inválido ou não configurado")
    return key


def _tenant_for_sheet(cfg: Config, spreadsheet_id: str) -> str:
    matches = [key for key, tenant in cfg.tenants.items() if tenant.sheet_id == spreadsheet_id]
    if not spreadsheet_id or len(matches) != 1:
        raise HTTPException(400, "spreadsheet_id não corresponde a uma única padaria configurada")
    return matches[0]


def resolve_hermes_request_identity(request: Any) -> str | None:
    """Extrai user_id autenticado pelo Hermes Dashboard sem confiar em headers de cliente."""
    state = getattr(request, "state", None)
    session = getattr(state, "session", None)
    token_principal = getattr(state, "token_principal", None)
    principal = getattr(state, "native_http_principal", None)

    user_id = getattr(session, "user_id", None) or (
        session.get("user_id") if isinstance(session, dict) else None
    )
    if not user_id and token_principal is not None:
        user_id = (
            getattr(token_principal, "principal", None)
            or getattr(token_principal, "user_id", None)
            or (
                token_principal.get("principal") or token_principal.get("user_id")
                if isinstance(token_principal, dict)
                else None
            )
        )
    if not user_id and principal is not None:
        if isinstance(principal, dict):
            user_id = str(
                principal.get("principal")
                or principal.get("user_id")
                or "legacy-token-owner"
            )
        else:
            user_id = (
                getattr(principal, "principal", None)
                or getattr(principal, "user_id", None)
                or "legacy-token-owner"
            )
    if not user_id and getattr(state, "hosted_http_authenticated", False) is True:
        user_id = "legacy-token-owner"

    app = getattr(request, "app", None)
    app_state = getattr(app, "state", None)
    if not user_id and not getattr(app_state, "auth_required", False):
        ws_mod = sys.modules.get("hermes_cli.web_server")
        if ws_mod is None:
            try:
                ws_mod = importlib.import_module("hermes_cli.web_server")
            except ImportError:
                ws_mod = None
        require_token_fn = getattr(ws_mod, "_require_token", None) if ws_mod else None
        if callable(require_token_fn):
            require_token_fn(request)
            user_id = "legacy-token-owner"
        elif getattr(state, "token_authenticated", False) is True:
            user_id = "legacy-token-owner"
    return user_id


async def operator(request: Request) -> Config:
    cfg = Config.from_env()
    user_id = resolve_hermes_request_identity(request)
    if not user_id:
        raise HTTPException(401, "Sessão Hermes obrigatória")
    if not cfg.enabled or user_id not in cfg.operator_ids:
        raise HTTPException(403, "Operador não autorizado")
    request.state.ifood_operator_id = user_id
    return cfg


def _build_api_client(cfg: Config) -> IfoodClient:
    try:
        return IfoodClient(cfg, allow_empty_merchant_id=True)
    except TypeError:
        return IfoodClient(cfg)


def _build_engine(cfg: Config) -> SyncEngine:
    storage = SyncStorage(cfg.db_path)
    return SyncEngine(
        cfg,
        storage=storage,
        api_client_factory=_build_api_client,
        playwright_factory=PlaywrightStoreExecutor,
        sheets_client_factory=lambda c: GoogleSheetsClient(c),
    )


@router.get("/status")
async def status(cfg: Config = Depends(operator)):
    return {
        "enabled": cfg.enabled,
        "merchant_configured": bool(cfg.merchant_id),
        "token_configured": has_ifood_api_credentials(cfg),
        "sheets_api_configured": is_sheets_api_configured(cfg),
        "webhook_configured": bool(cfg.sheets_webhook_secret),
        "playwright_configured": bool(cfg.playwright_cdp_url or cfg.playwright_user_data_dir),
    }


@router.get("/tenants")
async def list_tenants(cfg: Config = Depends(operator)):
    return [{"tenant_key": key, "sheet_configured": bool(tenant.sheet_id)}
            for key, tenant in sorted(cfg.tenants.items())]


@router.get("/opening-hours")
async def opening_hours(cfg: Config = Depends(operator)):
    try:
        data = await asyncio.to_thread(IfoodClient(cfg).opening_hours)
        return {"merchant_id": cfg.merchant_id, "opening_hours": data}
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            raise HTTPException(exc.response.status_code, "Falha na consulta à API iFood") from exc
        if isinstance(exc, httpx.HTTPError):
            raise HTTPException(502, "API iFood indisponível") from exc
        raise


@router.post("/opening-hours/proposals")
async def propose_hours(body: ProposalBody, request: Request, cfg: Config = Depends(operator)):
    try:
        actor = request.state.ifood_operator_id
        return await asyncio.to_thread(prepare, IfoodClient(cfg), actor, body.shifts)
    except (ValueError, PermissionError) as exc:
        raise HTTPException(400 if isinstance(exc, ValueError) else 403, str(exc)) from exc


@router.post("/opening-hours/confirm")
async def confirm_hours(body: ConfirmBody, request: Request, cfg: Config = Depends(operator)):
    raise HTTPException(409, "Fluxo legado sem auditoria durável desativado; use propostas em /sync.")


# --- Google Sheets Source-of-Truth & Multi-Store Sync Endpoints ---


@router.post("/sheets/webhook")
async def sheets_webhook(request: Request):
    cfg = Config.from_env()
    if not cfg.enabled:
        raise HTTPException(403, "Plugin desabilitado")
    raw_body = await request.body()
    if len(raw_body) > 5 * 1024 * 1024:
        raise HTTPException(413, "Payload do webhook excede 5 MiB")
    ts_hdr = request.headers.get("X-Hermes-Sheets-Timestamp", "")
    sig_hdr = request.headers.get("X-Hermes-Sheets-Signature", "")
    try:
        verify_webhook_signature(
            secret=cfg.sheets_webhook_secret,
            timestamp_header=ts_hdr,
            signature_header=sig_hdr,
            raw_body=raw_body,
        )
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, "JSON inválido no webhook") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "O payload do webhook deve ser um objeto JSON")
    allowed_fields = {"spreadsheet_id", "trigger_source", "metadata", "stores_rows", "hours_rows", "catalog_rows"}
    if set(payload) - allowed_fields:
        raise HTTPException(400, "O payload contém campos desconhecidos")
    for field in ("stores_rows", "hours_rows", "catalog_rows"):
        if field in payload and not isinstance(payload[field], list):
            raise HTTPException(400, f"{field} deve ser uma lista")
    if len(json.dumps(payload, ensure_ascii=False)) > 5 * 1024 * 1024:
        raise HTTPException(413, "Payload do webhook excede 5 MiB")

    storage = SyncStorage(cfg.db_path)
    tenant_key = _tenant_for_sheet(cfg, str(payload.get("spreadsheet_id") or "").strip())
    delivery_hash = hashlib.sha256(raw_body).hexdigest()
    if not storage.claim_webhook_delivery(delivery_hash, tenant_key=tenant_key):
        existing_delivery = storage.get_webhook_delivery(delivery_hash, tenant_key=tenant_key)
        if existing_delivery and existing_delivery["summary"].get("status") == "PROCESSING":
            raise HTTPException(409, "Entrega já está em processamento")
        return {"duplicate": True, **(existing_delivery or {}).get("summary", {})}

    trigger_source = str(payload.get("trigger_source") or "WEBHOOK")
    stores_rows = payload.get("stores_rows") or []
    hours_rows = payload.get("hours_rows") or []
    catalog_rows = payload.get("catalog_rows") or []

    def _run_webhook() -> dict[str, Any]:
        snapshot = parse_sheet_snapshot(
            stores_rows=stores_rows,
            hours_rows=hours_rows,
            catalog_rows=catalog_rows,
            default_sync_mode=cfg.default_sync_mode,
            default_execution_mode=cfg.default_execution_mode,
            default_merchant_id=cfg.merchant_id,
            source_id=str(payload.get("spreadsheet_id") or ""),
            tenant_key=tenant_key,
        )
        engine = _build_engine(cfg)
        result = engine.process_snapshot(
            snapshot=snapshot,
            trigger_source=trigger_source,
            actor_id="google-sheets-webhook",
            dry_run=False,
            tenant_key=tenant_key,
        )
        storage.record_webhook_delivery(delivery_hash, result, tenant_key=tenant_key)
        return result

    try:
        result = await asyncio.to_thread(_run_webhook)
    except Exception:
        storage.record_webhook_delivery(delivery_hash, {"status": "UNCERTAIN"}, tenant_key=tenant_key)
        raise
    return {"duplicate": False, **result}


@router.post("/sheets/sync")
async def sync_from_sheets(
    body: SheetsSyncBody, request: Request, cfg: Config = Depends(operator)
):
    actor = request.state.ifood_operator_id
    tenant_key = _tenant(cfg, body.tenant_key)

    def _run_sync() -> dict[str, Any]:
        stores_rows = body.stores_rows
        hours_rows = body.hours_rows or []
        catalog_rows = body.catalog_rows or []
        if stores_rows is None:
            tenant_cfg = replace(cfg, sheets_spreadsheet_id=cfg.tenants[tenant_key].sheet_id)
            if not is_sheets_api_configured(tenant_cfg):
                raise ValueError(
                    "Google Sheets API não configurada; informe IFOOD_LOJA_SHEETS_ID "
                    "e credenciais ou envie snapshot inline."
                )
            tables = GoogleSheetsClient(tenant_cfg).fetch_snapshot_tables()
            stores_rows = tables["Lojas"]
            hours_rows = tables["Horarios"]
            catalog_rows = tables["Cardapio"]

        snapshot = parse_sheet_snapshot(
            stores_rows=stores_rows,
            hours_rows=hours_rows,
            catalog_rows=catalog_rows,
            default_sync_mode=cfg.default_sync_mode,
            default_execution_mode=cfg.default_execution_mode,
            default_merchant_id=cfg.merchant_id,
            source_id=cfg.tenants[tenant_key].sheet_id if body.stores_rows is None else "inline",
            tenant_key=tenant_key,
        )
        return _build_engine(cfg).process_snapshot(
            snapshot=snapshot,
            trigger_source="MANUAL_PULL",
            actor_id=actor,
            dry_run=body.dry_run,
            tenant_key=tenant_key,
        )

    try:
        return await asyncio.to_thread(_run_sync)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/sync/overview")
async def sync_overview(
    tenant_key: str | None = None, cfg: Config = Depends(operator)
):
    tenant_key = _tenant(cfg, tenant_key)
    def _load() -> dict[str, Any]:
        storage = SyncStorage(cfg.db_path)
        portal_sessions = build_portal_session_overview(cfg, storage)
        return {
            "tenant_key": tenant_key,
            "readiness": {
                "enabled": cfg.enabled,
                "sheets_api_configured": is_sheets_api_configured(
                    replace(cfg, sheets_spreadsheet_id=cfg.tenants[tenant_key].sheet_id)
                ),
                "sheets_spreadsheet_id": cfg.tenants[tenant_key].sheet_id,
                "webhook_configured": bool(cfg.sheets_webhook_secret),
                "sheets_poller_enabled": (
                    cfg.sheets_poll_seconds >= 30 and is_sheets_api_configured(
                        replace(cfg, sheets_spreadsheet_id=cfg.tenants[tenant_key].sheet_id)
                    )
                ),
                "sheets_poller_runtime": get_poller_runtime_state(),
                "ifood_api_configured": has_ifood_api_credentials(cfg),
                "anota_ai_configured": is_anota_ai_configured(cfg),
                "playwright_configured": bool(
                    cfg.playwright_cdp_url or cfg.playwright_user_data_dir
                ),
                "default_sync_mode": cfg.default_sync_mode,
                "default_execution_mode": cfg.default_execution_mode,
                "write_capabilities": sorted(cfg.write_capabilities),
                "validated_portal_domains": sorted(cfg.validated_portal_domains),
                "write_capabilities_configured": bool(cfg.write_capabilities),
                "automation_policy": cfg.default_sync_mode,
                "max_price_change_pct": cfg.max_price_change_pct,
                "portal_sessions": portal_sessions,
            },
            "portal_sessions": portal_sessions,
            "snapshots": storage.list_snapshots(tenant_key),
            "proposals": storage.list_proposals(tenant_key, limit=100),
            "audit_events": storage.list_audit_events(tenant_key, limit=50),
            "sheets_poller": storage.get_poller_status(tenant_key),
        }

    return await asyncio.to_thread(_load)


@router.get("/sync/proposals")
async def list_sync_proposals(
    status: str | None = None,
    store_key: str | None = None,
    tenant_key: str | None = None,
    cfg: Config = Depends(operator),
):
    tenant_key = _tenant(cfg, tenant_key)
    return await asyncio.to_thread(
        lambda: SyncStorage(cfg.db_path).list_proposals(tenant_key, status=status, store_key=store_key)
    )


@router.post("/sync/proposals/{proposal_id}/confirm")
async def confirm_sync_proposal(
    proposal_id: str,
    body: SyncConfirmBody,
    request: Request,
    cfg: Config = Depends(operator),
):
    actor = request.state.ifood_operator_id
    tenant_key = _tenant(cfg, body.tenant_key)
    try:
        return await asyncio.to_thread(
            lambda: _build_engine(cfg).confirm_proposal(
                proposal_id=proposal_id,
                actor_id=actor,
                override_guardrail=body.override_guardrail,
                tenant_key=tenant_key,
            )
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sync/proposals/{proposal_id}/reject")
async def reject_sync_proposal(
    proposal_id: str,
    body: SyncRejectBody,
    request: Request,
    cfg: Config = Depends(operator),
):
    actor = request.state.ifood_operator_id
    tenant_key = _tenant(cfg, body.tenant_key)
    try:
        return await asyncio.to_thread(
            lambda: _build_engine(cfg).reject_proposal(
                proposal_id=proposal_id,
                actor_id=actor,
                reason=body.reason,
                tenant_key=tenant_key,
            )
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sync/proposals/{proposal_id}/reconcile")
async def reconcile_sync_proposal(
    proposal_id: str, request: Request, tenant_key: str | None = None, cfg: Config = Depends(operator)
):
    actor = request.state.ifood_operator_id
    tenant_key = _tenant(cfg, tenant_key)
    try:
        return await asyncio.to_thread(
            lambda: _build_engine(cfg).reconcile_uncertain(
                proposal_id=proposal_id, actor_id=actor, tenant_key=tenant_key
            )
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/sync/audit")
async def sync_audit(
    limit: int = 100, offset: int = 0, tenant_key: str | None = None, cfg: Config = Depends(operator)
):
    tenant_key = _tenant(cfg, tenant_key)
    return await asyncio.to_thread(
        lambda: SyncStorage(cfg.db_path).list_audit_events(tenant_key, limit=limit, offset=offset)
    )


@router.get("/sheets/apps-script")
async def get_apps_script(cfg: Config = Depends(operator)):
    gs_path = PLUGIN_ROOT / "apps_script" / "Code.gs"
    return {
        "filename": "Code.gs",
        "content": gs_path.read_text(encoding="utf-8") if gs_path.exists() else "",
    }


@router.get("/anota-ai/export")
async def export_anota_ai_menu(
    store_key: str = "loja-anota-ai",
    cfg: Config = Depends(operator),
):
    """Importação somente-leitura do cardápio Anota AI (`simple-item/export`)."""
    def _fetch() -> dict[str, Any]:
        items = AnotaAiReadOnlyClient(cfg).export_simple_items()
        rows = anota_ai_items_to_sheet_rows(store_key, items)
        return {
            "channel": "ANOTA_AI",
            "read_only": True,
            "store_key": store_key.strip(),
            "total_items": len(items),
            "items": items,
            "cardapio_rows": rows,
        }

    try:
        return await asyncio.to_thread(_fetch)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502, f"Falha ao importar cardápio somente-leitura da Anota AI ({type(exc).__name__})."
        ) from exc

