"""Poller opt-in para reconciliar Google Sheets sem depender do webhook público."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime

from ifood.config import Config
from ifood.sheets_client import GoogleSheetsClient, is_sheets_api_configured
from ifood.sheets_parser import parse_sheet_snapshot
from ifood.storage import SyncStorage
from ifood.sync_engine import SyncEngine

_runtime_lock = threading.Lock()
_runtime_state = {"state": "UNOBSERVED", "detail": None}


def set_poller_runtime_state(state: str, detail: str | None = None) -> None:
    with _runtime_lock:
        _runtime_state.update(state=state, detail=detail)


def get_poller_runtime_state() -> dict[str, str | None]:
    with _runtime_lock:
        return dict(_runtime_state)


def poller_enabled(config: Config) -> bool:
    return (
        config.enabled
        and config.sheets_poll_seconds >= 30
        and any(is_sheets_api_configured(replace(config, sheets_spreadsheet_id=t.sheet_id))
                for t in config.tenants.values())
    )


async def run_sheets_poller(config: Config) -> None:
    """Executa uma reconciliação imediata e segue com intervalo/backoff supervisionado."""
    if not poller_enabled(config):
        set_poller_runtime_state("DISABLED")
        return

    set_poller_runtime_state("RUNNING")

    configured = [(key, replace(config, sheets_spreadsheet_id=t.sheet_id))
                  for key, t in config.tenants.items()
                  if is_sheets_api_configured(replace(config, sheets_spreadsheet_id=t.sheet_id))]
    await asyncio.gather(*(_run_tenant_poller(tenant_config, key)
                           for key, tenant_config in configured))


async def _run_tenant_poller(config: Config, tenant_key: str) -> None:
    storage = SyncStorage(config.db_path)
    interval = config.sheets_poll_seconds
    retry_delay = interval
    previous_status = storage.get_poller_status(tenant_key)
    last_success_at: str | None = previous_status.get("last_success_at")
    failures = int(previous_status.get("consecutive_failures") or 0)

    while True:
        try:
            tables = await asyncio.to_thread(GoogleSheetsClient(config).fetch_snapshot_tables)
            snapshot = parse_sheet_snapshot(
                stores_rows=tables["Lojas"],
                hours_rows=tables["Horarios"],
                catalog_rows=tables["Cardapio"],
                default_sync_mode=config.default_sync_mode,
                default_execution_mode=config.default_execution_mode,
                default_merchant_id=config.merchant_id,
                source_id=config.sheets_spreadsheet_id,
                tenant_key=tenant_key,
            )
            result = await asyncio.to_thread(
                SyncEngine(config, storage=storage).process_snapshot,
                snapshot=snapshot,
                trigger_source="SHEETS_POLLER",
                actor_id="google-sheets-poller",
                tenant_key=tenant_key,
                dry_run=False,
            )
            last_success_at = datetime.now(UTC).isoformat()
            failures = 0
            retry_delay = interval
            storage.update_poller_status(
                tenant_key=tenant_key,
                last_success_at=last_success_at,
                last_error=None,
                consecutive_failures=0,
                last_summary=str(result.get("summary") or "Reconciliação concluída."),
            )
        except asyncio.CancelledError:
            set_poller_runtime_state("STOPPED")
            raise
        except Exception as exc:
            failures += 1
            # Guardar a classe do erro, não a mensagem externa que pode conter
            # URLs, cabeçalhos ou detalhes de credenciais.
            storage.update_poller_status(
                tenant_key=tenant_key,
                last_success_at=last_success_at,
                last_error=f"Falha de reconciliação ({type(exc).__name__}).",
                consecutive_failures=failures,
                last_summary=None,
            )
            retry_delay = min(max(interval, retry_delay * 2), 900)

        await asyncio.sleep(retry_delay)
