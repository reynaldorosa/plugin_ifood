"""Cliente restrito à API oficial do iFood (Merchant v1.0 e Catalog v2.0)."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .auth import access_token
from .config import Config
from .write_policy import require_approved_write


def has_ifood_api_credentials(config: Config) -> bool:
    return bool(config.access_token or (config.client_id and config.client_secret))


class IfoodClient:
    def __init__(
        self,
        config: Config,
        transport: httpx.BaseTransport | None = None,
        *,
        allow_empty_merchant_id: bool = False,
    ) -> None:
        if config.api_base != "https://merchant-api.ifood.com.br":
            raise ValueError("Host da API iFood inválido")
        if config.merchant_id:
            uuid.UUID(config.merchant_id)
        elif not allow_empty_merchant_id:
            raise ValueError("Loja oficial precisa estar configurada")
        self.config = config
        self.transport = transport

    def _require_write_capability(
        self, capability: str, merchant_id: str, payload: Any = None
    ) -> None:
        if capability not in self.config.write_capabilities:
            raise PermissionError(
                f"Escrita '{capability}' bloqueada: contrato e autorização da conta ainda não foram validados."
            )
        require_approved_write(
            capability=capability, merchant_id=merchant_id, payload=payload
        )

    def _resolve_merchant_id(self, merchant_id: str | None = None) -> str:
        target = (merchant_id or self.config.merchant_id or "").strip()
        if not target:
            raise ValueError("Loja oficial (merchant_id) precisa estar configurada")
        uuid.UUID(target)
        return target

    def _request(
        self, method: str, path: str, payload: Any = None
    ) -> Any:
        safe_read = method.upper() == "GET"
        force_refresh = False
        with httpx.Client(
            base_url=self.config.api_base,
            transport=self.transport,
            follow_redirects=False,
            timeout=15,
        ) as client:
            for attempt in range(4 if safe_read else 1):
                token = access_token(
                    self.config, self.transport, force_refresh=force_refresh
                )
                response = client.request(
                    method, path, json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 401 and safe_read and not force_refresh and self.config.client_id and self.config.client_secret:
                    force_refresh = True
                    continue
                if safe_read and response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                    delay = min(2 ** attempt, 16)
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after:
                            try:
                                delay = max(0.0, float(retry_after))
                            except ValueError:
                                try:
                                    deadline = parsedate_to_datetime(retry_after)
                                    if deadline.tzinfo is None:
                                        deadline = deadline.replace(tzinfo=UTC)
                                    delay = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
                                except (TypeError, ValueError, OverflowError):
                                    pass
                    time.sleep(min(delay, 300.0))
                    continue
                response.raise_for_status()
                return response.json() if response.content else None
            response.raise_for_status()
            return response.json() if response.content else None

    def merchants(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/merchant/v1.0/merchants")
        if not isinstance(result, list):
            raise ValueError("Resposta de lojas inválida")
        return result

    def authorized_merchant(self, merchant_id: str | None = None) -> dict[str, Any]:
        target_id = self._resolve_merchant_id(merchant_id)
        match = next(
            (item for item in self.merchants() if item.get("id") == target_id), None
        )
        if match is None:
            raise PermissionError("Loja não consta na autorização do aplicativo")
        return match

    # --- Opening hours ---

    def opening_hours(self, merchant_id: str | None = None) -> list[dict[str, Any]]:
        target_id = self._resolve_merchant_id(merchant_id)
        self.authorized_merchant(target_id)
        path = f"/merchant/v1.0/merchants/{target_id}/opening-hours"
        result = self._request("GET", path)
        if not isinstance(result, list):
            raise ValueError("Resposta de horários inválida")
        return result

    def put_opening_hours(
        self, shifts: list[dict[str, Any]], merchant_id: str | None = None
    ) -> Any:
        target_id = self._resolve_merchant_id(merchant_id)
        self._require_write_capability("opening_hours", target_id, shifts)
        self.authorized_merchant(target_id)
        path = f"/merchant/v1.0/merchants/{target_id}/opening-hours"
        return self._request(
            "PUT", path, {"storeId": target_id, "shifts": shifts}
        )

    # --- Merchant status & interruptions (pauses) ---

    def merchant_status(self, merchant_id: str | None = None) -> list[dict[str, Any]] | dict[str, Any]:
        target_id = self._resolve_merchant_id(merchant_id)
        self.authorized_merchant(target_id)
        return self._request("GET", f"/merchant/v1.0/merchants/{target_id}/status")

    def list_interruptions(self, merchant_id: str | None = None) -> list[dict[str, Any]]:
        target_id = self._resolve_merchant_id(merchant_id)
        self.authorized_merchant(target_id)
        result = self._request(
            "GET", f"/merchant/v1.0/merchants/{target_id}/interruptions"
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ValueError("Resposta de interrupções incompleta ou inválida")
        return result

    def create_interruption(
        self,
        *,
        description: str,
        start_iso: str,
        end_iso: str,
        merchant_id: str | None = None,
    ) -> Any:
        target_id = self._resolve_merchant_id(merchant_id)
        self._require_write_capability(
            "store_pause", target_id,
            {"status_action": "PAUSE", "pause_reason": description or "Pausa via Sheets",
             "pause_start_iso": start_iso, "pause_end_iso": end_iso},
        )
        self.authorized_merchant(target_id)
        return self._request(
            "POST",
            f"/merchant/v1.0/merchants/{target_id}/interruptions",
            {
                "id": str(uuid.uuid4()),
                "description": description or "Pausa operacional via Google Sheets",
                "start": start_iso,
                "end": end_iso,
            },
        )

    def delete_interruption(
        self, interruption_id: str, merchant_id: str | None = None
    ) -> Any:
        target_id = self._resolve_merchant_id(merchant_id)
        self._require_write_capability("store_open", target_id)
        self.authorized_merchant(target_id)
        return self._request(
            "DELETE",
            f"/merchant/v1.0/merchants/{target_id}/interruptions/{interruption_id}",
        )

    # --- Catalog v2.0 (items, prices, availability) ---

    def list_catalogs(self, merchant_id: str | None = None) -> list[dict[str, Any]]:
        target_id = self._resolve_merchant_id(merchant_id)
        self.authorized_merchant(target_id)
        result = self._request(
            "GET", f"/catalog/v2.0/merchants/{target_id}/catalogs"
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ValueError("Resposta de catálogos incompleta ou inválida")
        return result

    def list_catalog_items(
        self, catalog_id: str, merchant_id: str | None = None
    ) -> list[dict[str, Any]]:
        target_id = self._resolve_merchant_id(merchant_id)
        self.authorized_merchant(target_id)
        result = self._request(
            "GET",
            f"/catalog/v2.0/merchants/{target_id}/catalogs/{catalog_id}/categories?includeItems=true",
        )
        items: list[dict[str, Any]] = []
        if not isinstance(result, list):
            raise ValueError("Resposta de categorias incompleta ou inválida")
        for category in result:
            if not isinstance(category, dict) or not isinstance(category.get("items"), list):
                raise ValueError("Categoria sem lista de itens; leitura parcial bloqueada")
            if any(not isinstance(item, dict) for item in category["items"]):
                raise ValueError("Item inválido no catálogo; leitura parcial bloqueada")
            items.extend(category["items"])
        return items

    def patch_item_price(
        self,
        *,
        item_id: str,
        price_brl: float,
        original_price_brl: float | None = None,
        merchant_id: str | None = None,
    ) -> Any:
        clean_item_id = (item_id or "").strip()
        if not clean_item_id:
            raise ValueError("item_id obrigatório para atualização de preço.")
        if price_brl < 0:
            raise ValueError("Preço do item não pode ser negativo.")
        if original_price_brl is not None and original_price_brl < 0:
            raise ValueError("Preço original do item não pode ser negativo.")
        target_id = self._resolve_merchant_id(merchant_id)
        price_cents = round(float(price_brl) * 100)
        orig_cents = round(float(original_price_brl) * 100) if original_price_brl is not None else None
        self._require_write_capability(
            "catalog_price",
            target_id,
            {
                "item_id": clean_item_id,
                "price_cents": price_cents,
                "original_price_cents": orig_cents,
            },
        )
        self.authorized_merchant(target_id)
        price_obj: dict[str, Any] = {"value": round(float(price_brl), 2)}
        if original_price_brl is not None:
            price_obj["originalValue"] = round(float(original_price_brl), 2)
        return self._request(
            "PATCH",
            f"/catalog/v2.0/merchants/{target_id}/items/price",
            {"itemId": clean_item_id, "price": price_obj},
        )

    def patch_item_status(
        self,
        *,
        item_id: str,
        available: bool,
        merchant_id: str | None = None,
    ) -> Any:
        clean_item_id = (item_id or "").strip()
        if not clean_item_id:
            raise ValueError("item_id obrigatório para atualização de disponibilidade.")
        target_id = self._resolve_merchant_id(merchant_id)
        self._require_write_capability(
            "catalog_availability",
            target_id,
            {"item_id": clean_item_id, "available": bool(available)},
        )
        self.authorized_merchant(target_id)
        return self._request(
            "PATCH",
            f"/catalog/v2.0/merchants/{target_id}/items/status",
            {
                "itemId": clean_item_id,
                "status": "AVAILABLE" if available else "UNAVAILABLE",
            },
        )

