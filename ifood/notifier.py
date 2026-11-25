"""Avisos isolados por padaria; falhas de entrega não afetam a sincronização."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ifood.config import Config
    from ifood.storage import SyncStorage


def notify_event(config: Config, storage: SyncStorage, tenant_key: str,
                 event_key: str, event_type: str, message: str) -> None:
    """Persiste o evento e tenta enviá-lo uma vez; o mesmo evento não duplica."""
    note = storage.create_notification(tenant_key, event_key, event_type, message)
    if note is None:
        return
    tenant = config.tenants.get(tenant_key)
    chat_id = (tenant.telegram_chat_id if tenant else "") or config.telegram_chat_id
    token = os.getenv("IFOOD_LOJA_TELEGRAM_BOT_TOKEN", "").strip()
    if not chat_id or not token:
        storage.set_notification_delivery(note["id"], "UNCONFIGURED")
        return
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"[Padaria {tenant_key}] {message}"},
            timeout=10.0,
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise ValueError("Telegram recusou o envio")
    except (httpx.HTTPError, ValueError):
        storage.set_notification_delivery(note["id"], "FAILED", "Falha no envio ao Telegram")
    else:
        storage.set_notification_delivery(note["id"], "SENT")
