"""Parser determinístico para planilhas Google Sheets e verificação HMAC de webhooks."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import unicodedata
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from ifood.models import (
    ExecutionMode,
    ParsedSheetSnapshot,
    SheetCatalogItem,
    SheetStoreConfig,
    StoreChannel,
    StoreDesiredState,
    SyncMode,
    canonical_digest,
)
from ifood.operations import validate_schedule

_DAY_MAP: dict[str, str] = {
    "MONDAY": "MONDAY",
    "MON": "MONDAY",
    "SEGUNDA": "MONDAY",
    "SEGUNDAFEIRA": "MONDAY",
    "SEG": "MONDAY",
    "TUESDAY": "TUESDAY",
    "TUE": "TUESDAY",
    "TERCA": "TUESDAY",
    "TERCAFEIRA": "TUESDAY",
    "TER": "TUESDAY",
    "WEDNESDAY": "WEDNESDAY",
    "WED": "WEDNESDAY",
    "QUARTA": "WEDNESDAY",
    "QUARTAFEIRA": "WEDNESDAY",
    "QUA": "WEDNESDAY",
    "THURSDAY": "THURSDAY",
    "THU": "THURSDAY",
    "QUINTA": "THURSDAY",
    "QUINTAFEIRA": "THURSDAY",
    "QUI": "THURSDAY",
    "FRIDAY": "FRIDAY",
    "FRI": "FRIDAY",
    "SEXTA": "FRIDAY",
    "SEXTAFEIRA": "FRIDAY",
    "SEX": "FRIDAY",
    "SATURDAY": "SATURDAY",
    "SAT": "SATURDAY",
    "SABADO": "SATURDAY",
    "SAB": "SATURDAY",
    "SUNDAY": "SUNDAY",
    "SUN": "SUNDAY",
    "DOMINGO": "SUNDAY",
    "DOM": "SUNDAY",
}

_STORE_ALIASES: dict[str, tuple[str, ...]] = {
    "store_key": ("store_key", "chave_loja", "loja_key", "codigo_loja", "store"),
    "channel": ("channel", "canal", "plataforma"),
    "merchant_id": ("merchant_id", "id_loja", "ifood_merchant_id"),
    "store_name": ("store_name", "nome_loja", "nome"),
    "execution_mode": ("execution_mode", "modo_execucao", "executor"),
    "sync_mode": ("sync_mode", "modo_sync", "modo_aplicacao"),
    "portal_url": ("portal_url", "url_portal", "url"),
    "enabled": ("enabled", "ativo", "habilitado", "sincronizar"),
    "prep_time_minutes": ("prep_time_minutes", "tempo_preparo_min", "tempo_preparo", "prep_time"),
    "status_action": ("status_action", "acao_status", "status_loja", "status"),
    "pause_reason": ("pause_reason", "motivo_pausa", "motivo"),
    "pause_start_iso": ("pause_start_iso", "inicio_pausa", "pause_start"),
    "pause_end_iso": ("pause_end_iso", "fim_pausa", "pause_end"),
    "sheet_row": ("sheet_row", "_row", "row_number"),
}

_HOURS_ALIASES: dict[str, tuple[str, ...]] = {
    "store_key": ("store_key", "chave_loja", "loja_key", "codigo_loja", "store"),
    "day_of_week": ("day_of_week", "dayofweek", "dia_semana", "dia"),
    "start_time": ("start_time", "start", "abertura", "inicio"),
    "end_time": ("end_time", "end", "fechamento", "fim"),
    "duration_minutes": ("duration_minutes", "duration", "duracao_min", "duracao"),
    "enabled": ("enabled", "ativo", "habilitado"),
}

_CATALOG_ALIASES: dict[str, tuple[str, ...]] = {
    "store_key": ("store_key", "chave_loja", "loja_key", "codigo_loja", "store"),
    "item_id": ("item_id", "id_item", "external_code", "sku", "codigo_item"),
    "item_name": ("item_name", "nome_item", "produto", "nome"),
    "description": ("description", "descricao"),
    "price_brl": ("price_brl", "preco_brl", "preco", "price", "valor"),
    "original_price_brl": ("original_price_brl", "preco_de", "preco_original", "original_price"),
    "available": ("available", "disponivel", "ativo", "status"),
    "sheet_row": ("sheet_row", "_row", "row_number"),
}


def _strip_accents(value: str) -> str:
    nfkd = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _strip_accents(key).strip().lower()).strip("_")


def _clean_cell(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def normalize_day_of_week(raw: str) -> str:
    token = re.sub(r"[^A-Z]", "", _strip_accents(raw or "").upper())
    mapped = _DAY_MAP.get(token)
    if not mapped:
        raise ValueError(f"Dia da semana não reconhecido: {raw!r}")
    return mapped


def _parse_bool(val: Any, *, default: bool | None = None) -> bool | None:
    if isinstance(val, bool):
        return val
    text = _strip_accents(_clean_cell(val)).upper()
    if not text:
        return default
    if text in {"TRUE", "1", "SIM", "S", "YES", "Y", "ATIVO", "ACTIVE", "ON", "DISPONIVEL", "AVAILABLE"}:
        return True
    if text in {"FALSE", "0", "NAO", "N", "NO", "INATIVO", "INACTIVE", "OFF", "PAUSADO", "UNAVAILABLE", "INDISPONIVEL"}:
        return False
    raise ValueError(f"Valor booleano inválido: {val!r}")


def _parse_time_to_minutes(raw: Any) -> tuple[str, int]:
    text = _clean_cell(raw)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        raise ValueError(f"Horário inválido (use HH:MM ou HH:MM:SS): {raw!r}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or "0")
    if hour == 24 and minute == 0 and second == 0:
        return "24:00:00", 1440
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"Horário fora do intervalo 00:00–23:59: {raw!r}")
    formatted = f"{hour:02d}:{minute:02d}:{second:02d}"
    return formatted, hour * 60 + minute


def parse_price_to_cents(val: Any) -> int | None:
    if val is None or (isinstance(val, str) and not val.strip()):
        return None
    if isinstance(val, bool):
        raise ValueError("Preço não pode ser booleano.")
    if isinstance(val, (int, float)):
        if val < 0:
            raise ValueError(f"Preço negativo não permitido: {val!r}")
        amount = Decimal(str(val))
        return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    text = str(val).strip()
    cleaned = re.sub(r"(?i)r\$|\s", "", text)
    if not cleaned:
        return None
    # Formato brasileiro: 1.250,90 ou 29,90
    if re.fullmatch(r"(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?", cleaned):
        normalized = cleaned.replace(".", "").replace(",", ".")
    # Formato internacional: 1,250.90 ou 29.90
    elif re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?", cleaned):
        normalized = cleaned.replace(",", "")
    else:
        raise ValueError(f"Formato de preço inválido ou ambíguo: {val!r}")

    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"Formato de preço inválido: {val!r}") from exc
    if amount < 0:
        raise ValueError(f"Preço negativo não permitido: {val!r}")
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _coerce_table_rows(
    rows: list[Any], aliases: dict[str, tuple[str, ...]]
) -> list[tuple[int, dict[str, Any]]]:
    if not rows:
        return []

    first = rows[0]
    if isinstance(first, dict):
        result: list[tuple[int, dict[str, Any]]] = []
        for idx, raw_dict in enumerate(rows, start=2):
            if not isinstance(raw_dict, dict):
                continue
            by_norm = {_norm_key(str(k)): v for k, v in raw_dict.items() if k is not None}
            mapped: dict[str, Any] = {}
            for canonical, cand_list in aliases.items():
                for cand in cand_list:
                    norm_c = _norm_key(cand)
                    if norm_c in by_norm:
                        mapped[canonical] = by_norm[norm_c]
                        break
            row_num = int(mapped["sheet_row"]) if _clean_cell(mapped.get("sheet_row")).isdigit() else idx
            result.append((row_num, mapped))
        return result

    if isinstance(first, (list, tuple)):
        headers = [_norm_key(str(cell)) for cell in first]
        header_idx: dict[str, int] = {}
        for canonical, cand_list in aliases.items():
            for cand in cand_list:
                norm_c = _norm_key(cand)
                if norm_c in headers:
                    header_idx[canonical] = headers.index(norm_c)
                    break
        result = []
        for row_num, raw_row in enumerate(rows[1:], start=2):
            if not isinstance(raw_row, (list, tuple)):
                continue
            if all(_clean_cell(cell) == "" for cell in raw_row):
                continue
            mapped = {
                canonical: raw_row[pos] if pos < len(raw_row) else ""
                for canonical, pos in header_idx.items()
            }
            if _clean_cell(mapped.get("sheet_row")).isdigit():
                resolved_row = int(_clean_cell(mapped["sheet_row"]))
            else:
                resolved_row = row_num
            result.append((resolved_row, mapped))
        return result

    return []


def parse_sheet_snapshot(
    *,
    stores_rows: list[Any],
    hours_rows: list[Any] | None = None,
    catalog_rows: list[Any] | None = None,
    default_sync_mode: str = "REVIEW",
    default_execution_mode: str = "API_FIRST",
    default_merchant_id: str = "",
    source_id: str = "",
    tenant_key: str = "default",
) -> ParsedSheetSnapshot:
    """Converte linhas da planilha Google Sheets em estado desejado validado por loja."""
    stores_parsed = _coerce_table_rows(stores_rows, _STORE_ALIASES)
    hours_parsed = _coerce_table_rows(hours_rows or [], _HOURS_ALIASES)
    catalog_parsed = _coerce_table_rows(catalog_rows or [], _CATALOG_ALIASES)

    row_errors: list[dict[str, Any]] = []
    store_states: dict[str, StoreDesiredState] = {}

    def inspect_schema(rows: list[Any], aliases: dict[str, tuple[str, ...]], sheet: str,
                       required: set[str]) -> None:
        if not rows:
            return
        first = rows[0]
        known = {_norm_key(alias) for choices in aliases.values() for alias in choices}
        if isinstance(first, (list, tuple)):
            headers = [_norm_key(str(cell)) for cell in first if _clean_cell(cell)]
            canonical_present = {
                canonical for canonical, choices in aliases.items()
                if any(_norm_key(choice) in headers for choice in choices)
            }
            for missing in sorted(required - canonical_present):
                row_errors.append({"sheet": sheet, "row": 1, "field": missing,
                                   "message": f"Coluna obrigatória '{missing}' ausente em {sheet}."})
            for header in headers:
                if header not in known:
                    row_errors.append({"sheet": sheet, "row": 1, "field": header,
                                       "message": f"Coluna desconhecida '{header}' em {sheet}."})
            for row_num, row in enumerate(rows[1:], start=2):
                if not isinstance(row, (list, tuple)):
                    row_errors.append({"sheet": sheet, "row": row_num,
                                       "message": "Linha deve ser uma lista de células."})
        elif isinstance(first, dict):
            for row_num, row in enumerate(rows, start=2):
                if not isinstance(row, dict):
                    row_errors.append({"sheet": sheet, "row": row_num,
                                       "message": "Linha deve ser um objeto ou lista de células."})
                    continue
                keys = {_norm_key(str(key)) for key in row if key is not None}
                canonical_present = {
                    canonical for canonical, choices in aliases.items()
                    if any(_norm_key(choice) in keys for choice in choices)
                }
                for missing in sorted(required - canonical_present):
                    row_errors.append({"sheet": sheet, "row": row_num, "field": missing,
                                       "message": f"Campo obrigatório '{missing}' ausente em {sheet}."})
                for key in sorted(keys - known):
                    row_errors.append({"sheet": sheet, "row": row_num, "field": key,
                                       "message": f"Campo desconhecido '{key}' em {sheet}."})

    inspect_schema(stores_rows, _STORE_ALIASES, "Lojas", {"store_key"})
    inspect_schema(hours_rows or [], _HOURS_ALIASES, "Horarios", {"store_key", "day_of_week", "start_time"})
    inspect_schema(catalog_rows or [], _CATALOG_ALIASES, "Cardapio", {"store_key", "item_id"})

    for row_num, row in stores_parsed:
        store_key = _clean_cell(row.get("store_key"))
        if not store_key:
            row_errors.append(
                {
                    "sheet": "Lojas",
                    "row": row_num,
                    "field": "store_key",
                    "message": "store_key obrigatório.",
                }
            )
            continue

        try:
            channel_raw = _strip_accents(_clean_cell(row.get("channel") or "IFOOD")).upper()
            channel = (StoreChannel.FOOD99 if channel_raw in {"99FOOD", "99", "99APP"}
                       else StoreChannel.ANOTA_AI if channel_raw in {"ANOTA_AI", "ANOTAAI", "ANOTA AI"}
                       else StoreChannel.IFOOD)
            if channel_raw not in {"", "IFOOD", "99FOOD", "99", "99APP", "ANOTA_AI", "ANOTAAI", "ANOTA AI"}:
                raise ValueError(f"Canal não suportado: {channel_raw!r}")

            exec_raw = _clean_cell(row.get("execution_mode") or default_execution_mode).upper()
            exec_mode = ExecutionMode(exec_raw)

            configured_mode = SyncMode(default_sync_mode)
            requested_mode = SyncMode(_clean_cell(row.get("sync_mode") or configured_mode.value).upper())
            # A planilha pode reduzir AUTO_APPLY para REVIEW, mas nunca ampliar a política do runtime.
            sync_mode = requested_mode if configured_mode == SyncMode.AUTO_APPLY else SyncMode.REVIEW

            enabled_val = _parse_bool(row.get("enabled"), default=True)
            enabled = True if enabled_val is None else enabled_val

            prep_raw = _clean_cell(row.get("prep_time_minutes"))
            prep_minutes = int(prep_raw) if prep_raw else None

            status_raw = _strip_accents(_clean_cell(row.get("status_action"))).upper()
            status_action: str | None = None
            if status_raw in {"OPEN", "ABERTA", "ABERTO", "ONLINE"}:
                status_action = "OPEN"
            elif status_raw in {"PAUSE", "PAUSED", "PAUSADA", "PAUSAR", "CLOSED", "FECHADA"}:
                status_action = "PAUSE"
            elif status_raw:
                raise ValueError(f"status_action inválido (use OPEN ou PAUSE): {status_raw!r}")

            pause_start = _clean_cell(row.get("pause_start_iso"))
            pause_end = _clean_cell(row.get("pause_end_iso"))
            if status_action == "PAUSE":
                try:
                    start_dt = datetime.fromisoformat(pause_start)
                    end_dt = datetime.fromisoformat(pause_end)
                except ValueError as exc:
                    raise ValueError("Pausa requer início e fim em ISO 8601.") from exc
                if start_dt.tzinfo is None or end_dt.tzinfo is None:
                    raise ValueError("Início e fim da pausa devem incluir fuso horário.")
                if end_dt <= start_dt:
                    raise ValueError("Fim da pausa deve ser posterior ao início.")
                if (end_dt - start_dt).total_seconds() > 7 * 24 * 60 * 60:
                    raise ValueError("Interrupções não podem exceder sete dias.")

            merchant_id = _clean_cell(row.get("merchant_id")) or (
                default_merchant_id if channel == StoreChannel.IFOOD and len(stores_parsed) == 1 else ""
            )

            store_cfg = SheetStoreConfig(
                store_key=store_key,
                tenant_key=tenant_key,
                channel=channel,
                merchant_id=merchant_id,
                store_name=_clean_cell(row.get("store_name")) or store_key,
                execution_mode=exec_mode,
                sync_mode=sync_mode,
                portal_url=_clean_cell(row.get("portal_url")),
                enabled=enabled,
                prep_time_minutes=prep_minutes,
                status_action=status_action,
                pause_reason=_clean_cell(row.get("pause_reason")),
                pause_start_iso=pause_start,
                pause_end_iso=pause_end,
                sheet_row=row_num,
            )
            if store_key in store_states:
                raise ValueError(f"store_key duplicada: {store_key!r}.")
            store_states[store_key] = StoreDesiredState(
                tenant_key=tenant_key, store=store_cfg
            )
        except Exception as exc:
            row_errors.append(
                {
                    "sheet": "Lojas",
                    "row": row_num,
                    "store_key": store_key,
                    "message": str(exc),
                }
            )

    # Coleta turnos brutos por loja
    raw_shifts_by_store: dict[str, list[dict[str, Any]]] = {}
    shift_rows_by_store: dict[str, list[int]] = {}
    for row_num, row in hours_parsed:
        store_key = _clean_cell(row.get("store_key"))
        if not store_key:
            row_errors.append(
                {
                    "sheet": "Horarios",
                    "row": row_num,
                    "field": "store_key",
                    "message": "store_key obrigatório em Horarios.",
                }
            )
            continue
        if store_key not in store_states:
            row_errors.append(
                {
                    "sheet": "Horarios",
                    "row": row_num,
                    "store_key": store_key,
                    "message": f"Loja '{store_key}' não encontrada na aba Lojas.",
                }
            )
            continue
        try:
            enabled_shift = _parse_bool(row.get("enabled"), default=True)
            if enabled_shift is False:
                continue
            day = normalize_day_of_week(_clean_cell(row.get("day_of_week")))
            start_str, start_mins = _parse_time_to_minutes(row.get("start_time"))
            dur_raw = _clean_cell(row.get("duration_minutes"))
            end_raw = _clean_cell(row.get("end_time"))
            if dur_raw:
                duration = int(dur_raw)
            elif end_raw:
                _, end_mins = _parse_time_to_minutes(end_raw)
                duration = end_mins - start_mins
                if duration <= 0:
                    raise ValueError("end_time deve ser posterior a start_time no mesmo dia.")
            else:
                raise ValueError("Informe end_time ou duration_minutes em Horarios.")

            raw_shifts_by_store.setdefault(store_key, []).append(
                {"dayOfWeek": day, "start": start_str, "duration": duration}
            )
            shift_rows_by_store.setdefault(store_key, []).append(row_num)
        except Exception as exc:
            row_errors.append(
                {
                    "sheet": "Horarios",
                    "row": row_num,
                    "store_key": store_key,
                    "message": str(exc),
                }
            )

    for store_key, shifts in raw_shifts_by_store.items():
        # Keep the parsed rows so the engine can attach the sheet's validation
        # error to this domain; it must never treat the partial set as valid.
        store_states[store_key].shifts = sorted(
            shifts, key=lambda shift: (shift["dayOfWeek"], shift["start"], shift["duration"])
        )
        try:
            store_states[store_key].shifts = validate_schedule(shifts)
        except Exception as exc:
            row_errors.append(
                {
                    "sheet": "Horarios",
                    "row": shift_rows_by_store.get(store_key, []),
                    "store_key": store_key,
                    "message": f"Grade inválida para '{store_key}': {exc}",
                }
            )

    # Coleta itens do cardápio
    seen_catalog_items: set[tuple[str, str]] = set()
    for row_num, row in catalog_parsed:
        store_key = _clean_cell(row.get("store_key"))
        item_id = _clean_cell(row.get("item_id"))
        if not store_key or not item_id:
            row_errors.append(
                {
                    "sheet": "Cardapio",
                    "row": row_num,
                    "message": "store_key e item_id são obrigatórios em Cardapio.",
                }
            )
            continue
        if store_key not in store_states:
            row_errors.append(
                {
                    "sheet": "Cardapio",
                    "row": row_num,
                    "store_key": store_key,
                    "item_id": item_id,
                    "message": f"Loja '{store_key}' não encontrada na aba Lojas.",
                }
            )
            continue
        catalog_key = (store_key, item_id)
        if catalog_key in seen_catalog_items:
            row_errors.append({
                "sheet": "Cardapio", "row": row_num, "store_key": store_key,
                "item_id": item_id, "message": "item_id duplicado para esta loja.",
            })
            continue
        seen_catalog_items.add(catalog_key)
        try:
            price_cents = parse_price_to_cents(row.get("price_brl"))
            orig_cents = parse_price_to_cents(row.get("original_price_brl"))
            available = _parse_bool(row.get("available"), default=None)
            desc_raw = _clean_cell(row.get("description"))
            item = SheetCatalogItem(
                store_key=store_key,
                tenant_key=tenant_key,
                item_id=item_id,
                item_name=_clean_cell(row.get("item_name")) or item_id,
                description=desc_raw if desc_raw != "" else None,
                price_cents=price_cents,
                original_price_cents=orig_cents,
                available=available,
                sheet_row=row_num,
            )
            store_states[store_key].catalog_items.append(item)
        except Exception as exc:
            row_errors.append(
                {
                    "sheet": "Cardapio",
                    "row": row_num,
                    "store_key": store_key,
                    "item_id": item_id,
                    "message": str(exc),
                }
            )

    snap_payload = {
        k: state.model_dump(mode="json") for k, state in sorted(store_states.items())
    }
    return ParsedSheetSnapshot(
        tenant_key=tenant_key,
        stores=store_states,
        row_errors=row_errors,
        snapshot_hash=canonical_digest(snap_payload),
        source_id=source_id[:200],
    )


def verify_webhook_signature(
    *,
    secret: str,
    timestamp_header: str,
    signature_header: str,
    raw_body: bytes,
    max_age_seconds: int = 300,
    now_epoch: int | None = None,
) -> None:
    """Valida assinatura HMAC-SHA256 + janela anti-replay para webhooks do Apps Script."""
    if not secret:
        raise PermissionError("Segredo do webhook do Google Sheets não configurado no servidor.")
    ts_clean = (timestamp_header or "").strip()
    sig_clean = (signature_header or "").strip()
    if not ts_clean or not sig_clean:
        raise PermissionError("Cabeçalhos de assinatura HMAC obrigatórios.")
    try:
        ts_val = int(ts_clean)
    except ValueError as exc:
        raise PermissionError("Timestamp HMAC inválido.") from exc

    current_ts = int(time.time()) if now_epoch is None else now_epoch
    if abs(current_ts - ts_val) > max_age_seconds:
        raise PermissionError("Webhook expirado fora da janela anti-replay.")

    provided_hex = sig_clean[7:] if sig_clean.lower().startswith("sha256=") else sig_clean
    signed_payload = ts_clean.encode("utf-8") + b"." + raw_body
    expected_hex = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hex, provided_hex.lower()):
        raise PermissionError("Assinatura HMAC-SHA256 do Google Sheets inválida.")
