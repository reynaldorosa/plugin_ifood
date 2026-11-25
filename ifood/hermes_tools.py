"""Ferramentas registradas no Hermes Agent para sincronizar Google Sheets → iFood/99Food."""

from __future__ import annotations

import json
from typing import Any

from ifood.anota_ai import (
    AnotaAiReadOnlyClient,
    anota_ai_items_to_sheet_rows,
    is_anota_ai_configured,
)
from ifood.client import has_ifood_api_credentials
from ifood.config import Config
from ifood.sheets_client import GoogleSheetsClient, is_sheets_api_configured
from ifood.sheets_parser import parse_sheet_snapshot
from ifood.storage import SyncStorage
from ifood.sync_engine import (
    SyncEngine,
    build_portal_session_overview,
    resolve_hermes_agent_actor,
)


def _authorize_agent() -> tuple[Config, str] | tuple[None, dict[str, Any]]:
    actor = resolve_hermes_agent_actor()
    if not actor:
        return None, {
            "ok": False,
            "status_code": 401,
            "error": "IFOOD_LOJA_AGENT_OPERATOR_ID não configurado.",
        }
    cfg = Config.from_env()
    if not cfg.enabled or actor not in cfg.operator_ids:
        return None, {
            "ok": False,
            "status_code": 403,
            "error": "Plugin bloqueado ou agente sem autorização de operador.",
        }
    return cfg, actor


def tool_sync_status(tenant_key: str = "default") -> str:
    cfg, auth_or_err = _authorize_agent()
    if cfg is None:
        return json.dumps(auth_or_err, ensure_ascii=False)
    storage = SyncStorage(cfg.db_path)
    pending = storage.list_proposals(tenant_key, status="PENDING")
    blocked = storage.list_proposals(tenant_key, status="BLOCKED")
    portal_sessions = build_portal_session_overview(cfg, storage)
    return json.dumps(
        {
            "ok": True,
            "tenant_key": tenant_key,
            "enabled": cfg.enabled,
            "sheets_api_configured": is_sheets_api_configured(cfg),
            "webhook_configured": bool(cfg.sheets_webhook_secret),
            "ifood_api_configured": has_ifood_api_credentials(cfg),
            "anota_ai_configured": is_anota_ai_configured(cfg),
            "playwright_configured": bool(cfg.playwright_cdp_url or cfg.playwright_user_data_dir),
            "portal_sessions": portal_sessions,
            "pending_proposals": len(pending),
            "blocked_proposals": len(blocked),
            "recent_audit": storage.list_audit_events(tenant_key, limit=10),
        },
        ensure_ascii=False,
    )


def tool_sync_from_sheet(
    stores_rows: list[Any] | None = None,
    hours_rows: list[Any] | None = None,
    catalog_rows: list[Any] | None = None,
    dry_run: bool = False,
    tenant_key: str = "default",
) -> str:
    cfg, auth_or_err = _authorize_agent()
    if cfg is None:
        return json.dumps(auth_or_err, ensure_ascii=False)
    actor = str(auth_or_err)
    storage = SyncStorage(cfg.db_path)
    engine = SyncEngine(cfg, storage=storage)
    source_id = "inline" if stores_rows is not None else cfg.sheets_spreadsheet_id

    if stores_rows is None:
        if not is_sheets_api_configured(cfg):
            return json.dumps(
                {
                    "ok": False,
                    "status_code": 400,
                    "error": "Planilha Google Sheets não configurada e nenhum snapshot foi fornecido.",
                },
                ensure_ascii=False,
            )
        tables = GoogleSheetsClient(cfg).fetch_snapshot_tables()
        stores_rows = tables["Lojas"]
        hours_rows = tables["Horarios"]
        catalog_rows = tables["Cardapio"]

    snapshot = parse_sheet_snapshot(
        stores_rows=stores_rows,
        hours_rows=hours_rows or [],
        catalog_rows=catalog_rows or [],
        default_sync_mode=cfg.default_sync_mode,
        default_execution_mode=cfg.default_execution_mode,
        default_merchant_id=cfg.merchant_id,
        source_id=source_id,
        tenant_key=tenant_key,
    )
    res = engine.process_snapshot(
        snapshot=snapshot,
        trigger_source="HERMES_TOOL",
        actor_id=actor,
        tenant_key=tenant_key,
        dry_run=dry_run,
    )
    return json.dumps({"ok": True, "data": res}, ensure_ascii=False)


def tool_list_proposals(
    status: str | None = None, tenant_key: str = "default"
) -> str:
    cfg, auth_or_err = _authorize_agent()
    if cfg is None:
        return json.dumps(auth_or_err, ensure_ascii=False)
    storage = SyncStorage(cfg.db_path)
    return json.dumps(
        {"ok": True, "data": storage.list_proposals(tenant_key, status=status)},
        ensure_ascii=False,
    )


def tool_confirm_proposal(
    proposal_id: str,
    override_guardrail: bool = False,
    tenant_key: str = "default",
) -> str:
    cfg, auth_or_err = _authorize_agent()
    if cfg is None:
        return json.dumps(auth_or_err, ensure_ascii=False)
    engine = SyncEngine(cfg)
    try:
        res = engine.confirm_proposal(
            proposal_id=proposal_id,
            actor_id=str(auth_or_err),
            tenant_key=tenant_key,
            override_guardrail=override_guardrail,
        )
        return json.dumps({"ok": True, "data": res}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def tool_reject_proposal(
    proposal_id: str, reason: str = "", tenant_key: str = "default"
) -> str:
    cfg, auth_or_err = _authorize_agent()
    if cfg is None:
        return json.dumps(auth_or_err, ensure_ascii=False)
    engine = SyncEngine(cfg)
    try:
        res = engine.reject_proposal(
            proposal_id=proposal_id,
            actor_id=str(auth_or_err),
            tenant_key=tenant_key,
            reason=reason,
        )
        return json.dumps({"ok": True, "data": res}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def tool_import_anota_ai_menu(store_key: str = "loja-anota-ai") -> str:
    cfg, auth_or_err = _authorize_agent()
    if cfg is None:
        return json.dumps(auth_or_err, ensure_ascii=False)
    try:
        items = AnotaAiReadOnlyClient(cfg).export_simple_items()
        rows = anota_ai_items_to_sheet_rows(store_key, items)
        return json.dumps(
            {
                "ok": True,
                "channel": "ANOTA_AI",
                "read_only": True,
                "store_key": store_key.strip(),
                "total_items": len(items),
                "items": items,
                "cardapio_rows": rows,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "ifood_sync_status",
        "schema": {
            "name": "ifood_sync_status",
            "description": "Verifica prontidão (Google Sheets, iFood API, Playwright) e propostas pendentes.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        "handler": tool_sync_status,
    },
    {
        "name": "ifood_sync_from_sheet",
        "schema": {
            "name": "ifood_sync_from_sheet",
            "description": "Lê a planilha Google Sheets (ou snapshot JSON) e sincroniza horários, cardápio, status e preparo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "stores_rows": {"type": "array"},
                    "hours_rows": {"type": "array"},
                    "catalog_rows": {"type": "array"},
                    "dry_run": {"type": "boolean"},
                },
                "required": [],
            },
        },
        "handler": tool_sync_from_sheet,
    },
    {
        "name": "ifood_list_proposals",
        "schema": {
            "name": "ifood_list_proposals",
            "description": "Lista propostas pendentes ou bloqueadas por guardrail vindas da planilha Google Sheets.",
            "parameters": {
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": [],
            },
        },
        "handler": tool_list_proposals,
    },
    {
        "name": "ifood_confirm_proposal",
        "schema": {
            "name": "ifood_confirm_proposal",
            "description": "Aprova e executa uma proposta pendente via iFood API oficial ou fallback Playwright.",
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "override_guardrail": {"type": "boolean"},
                },
                "required": ["proposal_id"],
            },
        },
        "handler": tool_confirm_proposal,
    },
    {
        "name": "ifood_reject_proposal",
        "schema": {
            "name": "ifood_reject_proposal",
            "description": "Rejeita uma proposta pendente gerada a partir da planilha Google Sheets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["proposal_id"],
            },
        },
        "handler": tool_reject_proposal,
    },
    {
        "name": "ifood_import_anota_ai_menu",
        "schema": {
            "name": "ifood_import_anota_ai_menu",
            "description": "Importa somente-leitura o cardápio da Anota AI (simple-item/export) pronto para a aba Cardapio da planilha.",
            "parameters": {
                "type": "object",
                "properties": {
                    "store_key": {"type": "string"},
                },
                "required": [],
            },
        },
        "handler": tool_import_anota_ai_menu,
    },
]
