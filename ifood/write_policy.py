"""Runtime guard for write adapters; only SyncEngine may open a scoped permit."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ifood.models import ChangeDomain, StoreChannel, canonical_digest

_active_permit: ContextVar[dict[str, Any] | None] = ContextVar("ifood_write_permit", default=None)


@contextmanager
def approved_execution(
    *, execution_id: str, merchant_id: str, channel: str,
    domain: ChangeDomain, payload: Any, actor_id: str,
) -> Iterator[None]:
    if not execution_id or not actor_id or channel not in {
        StoreChannel.IFOOD.value, StoreChannel.FOOD99.value,
    }:
        raise PermissionError("Contexto de aprovação incompleto para escrita.")
    token = _active_permit.set({
        "execution_id": execution_id,
        "merchant_id": merchant_id,
        "channel": channel,
        "domain": domain.value,
        "payload_hash": canonical_digest(payload),
        "payload": payload,
        "actor_id": actor_id,
    })
    try:
        yield
    finally:
        _active_permit.reset(token)


def require_approved_write(
    *, capability: str, merchant_id: str, payload: Any = None
) -> None:
    permit = _active_permit.get()
    if permit is None or permit["merchant_id"] != merchant_id:
        raise PermissionError("Escrita bloqueada fora de execução aprovada e vinculada ao merchant.")
    domain = permit["domain"]
    allowed = {
        "store_pause": "STORE_STATUS",
        "store_open": "STORE_STATUS",
        "catalog_price": "CATALOG",
        "catalog_availability": "CATALOG",
        "prep_time": "PREP_TIME",
        "opening_hours": "OPENING_HOURS",
    }
    if allowed.get(capability) != domain:
        raise PermissionError("A capacidade de escrita não corresponde à proposta aprovada.")
    if payload is not None:
        approved = permit["payload"]
        if capability == "store_pause":
            approved = {
                "status_action": "PAUSE",
                "pause_reason": approved.get("pause_reason") or "Pausa via Sheets",
                "pause_start_iso": approved.get("pause_start_iso"),
                "pause_end_iso": approved.get("pause_end_iso"),
            }
        elif capability == "store_open":
            approved = {"status_action": "OPEN"}
        if canonical_digest(payload) != canonical_digest(approved):
            raise PermissionError("Os valores de escrita diferem dos valores aprovados.")

