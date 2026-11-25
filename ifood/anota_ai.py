"""Importador somente-leitura de cardápio da Anota AI (`simple-item/export`).

Regras estritas de segurança:
- Usa exclusivamente token oficial de API (`IFOOD_LOJA_ANOTA_AI_TOKEN`); nunca senha de portal.
- Somente leitura (`GET`): nunca envia mutações à Anota AI.
- Anti-SSRF: aceita apenas HTTPS nos hosts oficiais da Anota AI (`api-parceiros.anota.ai`),
  sem credenciais na URL e sem seguir redirecionamentos.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from ifood.config import Config

_ALLOWED_ANOTA_AI_HOSTS = frozenset({"api-parceiros.anota.ai"})
_DEFAULT_EXPORT_PATH = "/partnerauth/v1/simple-item/export"
_CARDAPIO_HEADERS = [
    "store_key",
    "item_id",
    "item_name",
    "description",
    "price_brl",
    "original_price_brl",
    "available",
]


def is_anota_ai_configured(config: Config) -> bool:
    return bool((config.anota_ai_api_token or "").strip())


def validate_anota_ai_base(api_base: str) -> str:
    raw = (api_base or "").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in _ALLOWED_ANOTA_AI_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Host da API Anota AI inválido; permitido apenas HTTPS oficial (api-parceiros.anota.ai)."
        )
    return raw


def _parse_price_cents(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val < 0:
            return None
        return round(val * 100)
    text = str(raw).strip().replace("R$", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        val = float(text)
    except ValueError:
        return None
    return round(val * 100) if val >= 0 else None


def _parse_available(item: dict[str, Any]) -> bool:
    for key in ("available", "active", "enabled"):
        if key in item and item[key] is not None:
            val = item[key]
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return bool(val)
            text = str(val).strip().lower()
            if text in {"true", "1", "yes", "sim", "available", "active"}:
                return True
            if text in {"false", "0", "no", "nao", "não", "unavailable", "inactive", "paused"}:
                return False
    status = str(item.get("status") or "").strip().upper()
    return status not in {"UNAVAILABLE", "INACTIVE", "PAUSED", "DISABLED"}


def _extract_raw_items(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    """Extrai pares (categoria, item_dict) dos formatos usuais de `simple-item/export`."""
    data: Any = payload
    if isinstance(data, dict):
        if isinstance(data.get("info"), dict) and "data" in data["info"]:
            data = data["info"]["data"]
        elif "data" in data:
            data = data["data"]
        elif "categories" in data:
            data = data["categories"]
        elif "items" in data:
            data = data["items"]
        elif "itens" in data:
            data = data["itens"]

    if not isinstance(data, list):
        raise ValueError("Resposta de simple-item/export da Anota AI em formato inválido.")

    extracted: list[tuple[str, dict[str, Any]]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("itens") if isinstance(entry.get("itens"), list) else entry.get("items")
        if isinstance(nested, list):
            cat_name = str(entry.get("title") or entry.get("name") or "").strip()
            for sub in nested:
                if isinstance(sub, dict):
                    extracted.append((cat_name, sub))
        else:
            cat_name = str(entry.get("category") or entry.get("category_name") or "").strip()
            extracted.append((cat_name, entry))
    return extracted


def normalize_anota_ai_export(payload: Any) -> list[dict[str, Any]]:
    """Normaliza resposta JSON de `simple-item/export` para itens canônicos."""
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for category, raw in _extract_raw_items(payload):
        raw_id = (
            raw.get("id")
            or raw.get("_id")
            or raw.get("external_id")
            or raw.get("externalCode")
            or raw.get("sku")
        )
        item_id = str(raw_id or "").strip()
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        item_name = str(raw.get("title") or raw.get("name") or item_id).strip()
        description_raw = raw.get("description")
        description = str(description_raw).strip() if description_raw is not None else ""
        price_val = raw.get("price")
        if isinstance(price_val, dict):
            price_cents = _parse_price_cents(price_val.get("value"))
            orig_cents = _parse_price_cents(price_val.get("originalValue"))
        else:
            price_cents = _parse_price_cents(price_val)
            orig_cents = _parse_price_cents(
                raw.get("original_price") or raw.get("originalPrice")
            )
        items.append(
            {
                "item_id": item_id,
                "item_name": item_name,
                "description": description,
                "category": category,
                "price_cents": price_cents,
                "original_price_cents": orig_cents,
                "available": _parse_available(raw),
            }
        )
    items.sort(key=lambda row: row["item_id"])
    return items


def anota_ai_items_to_sheet_rows(
    store_key: str,
    items: list[dict[str, Any]],
    *,
    include_header: bool = True,
) -> list[list[str]]:
    """Converte itens normalizados para linhas prontas da aba `Cardapio`."""
    clean_store = (store_key or "").strip()
    if not clean_store:
        raise ValueError("store_key obrigatório para gerar linhas da aba Cardapio.")

    def _fmt_brl(cents: int | None) -> str:
        if cents is None:
            return ""
        return f"{cents / 100:.2f}".replace(".", ",")

    rows: list[list[str]] = [list(_CARDAPIO_HEADERS)] if include_header else []
    for item in items:
        rows.append(
            [
                clean_store,
                str(item["item_id"]),
                str(item.get("item_name") or ""),
                str(item.get("description") or ""),
                _fmt_brl(item.get("price_cents")),
                _fmt_brl(item.get("original_price_cents")),
                "SIM" if item.get("available", True) else "NAO",
            ]
        )
    return rows


class AnotaAiReadOnlyClient:
    """Cliente HTTP somente-leitura para `simple-item/export` da Anota AI."""

    def __init__(
        self,
        config: Config,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        token = (config.anota_ai_api_token or "").strip()
        if not token:
            raise ValueError("Token oficial da API Anota AI (IFOOD_LOJA_ANOTA_AI_TOKEN) não configurado.")
        self.api_base = validate_anota_ai_base(config.anota_ai_api_base)
        self.token = token
        self.transport = transport

    def export_simple_items(self) -> list[dict[str, Any]]:
        """Executa GET somente-leitura em `simple-item/export` e retorna itens normalizados."""
        with httpx.Client(
            base_url=self.api_base,
            transport=self.transport,
            follow_redirects=False,
            timeout=20,
        ) as client:
            response = client.get(
                _DEFAULT_EXPORT_PATH,
                headers={"Authorization": self.token, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        return normalize_anota_ai_export(payload)
