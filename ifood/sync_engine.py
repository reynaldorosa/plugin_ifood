"""Motor de sincronização unificado: Google Sheets (Source of Truth) → iFood API / Playwright."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from ifood.client import IfoodClient, has_ifood_api_credentials
from ifood.config import Config
from ifood.models import (
    BackendUsed,
    ChangeDomain,
    ExecutionMode,
    ParsedSheetSnapshot,
    SheetStoreConfig,
    StoreChannel,
    StoreDesiredState,
    SyncMode,
    SyncOutcome,
    canonical_digest,
)
from ifood.notifier import notify_event
from ifood.operations import digest as shifts_digest
from ifood.operations import normalize as normalize_shifts
from ifood.playwright_executor import PlaywrightStoreExecutor
from ifood.sheets_client import GoogleSheetsClient, is_sheets_api_configured
from ifood.storage import SyncStorage
from ifood.write_policy import approved_execution


class ApiClientProtocol(Protocol):
    def opening_hours(self, merchant_id: str | None = None) -> list[dict[str, Any]]: ...

    def put_opening_hours(
        self, shifts: list[dict[str, Any]], merchant_id: str | None = None
    ) -> Any: ...

    def list_catalogs(self, merchant_id: str | None = None) -> list[dict[str, Any]]: ...

    def list_catalog_items(
        self, catalog_id: str, merchant_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def patch_item_price(
        self,
        *,
        item_id: str,
        price_brl: float,
        original_price_brl: float | None = None,
        merchant_id: str | None = None,
    ) -> Any: ...

    def patch_item_status(
        self,
        *,
        item_id: str,
        available: bool,
        merchant_id: str | None = None,
    ) -> Any: ...

    def list_interruptions(self, merchant_id: str | None = None) -> list[dict[str, Any]]: ...

    def merchant_status(self, merchant_id: str | None = None) -> Any: ...

    def create_interruption(
        self,
        *,
        description: str,
        start_iso: str,
        end_iso: str,
        merchant_id: str | None = None,
    ) -> Any: ...

    def delete_interruption(
        self, interruption_id: str, merchant_id: str | None = None
    ) -> Any: ...


class PlaywrightExecutorProtocol(Protocol):
    def read_domain_state(
        self,
        *,
        store: SheetStoreConfig,
        domain: ChangeDomain,
        desired_payload: Any = None,
    ) -> dict[str, Any]: ...

    def execute_domain_update(
        self,
        *,
        store: SheetStoreConfig,
        domain: ChangeDomain,
        proposed_payload: Any,
    ) -> dict[str, Any]: ...


ApiClientFactory = Callable[[Config], Any]
PlaywrightFactory = Callable[[Config], Any]
SheetsClientFactory = Callable[[Config], Any]

_API_SUPPORTED_DOMAINS = frozenset(
    {ChangeDomain.OPENING_HOURS, ChangeDomain.CATALOG, ChangeDomain.STORE_STATUS}
)


class TenantMismatchError(PermissionError, ValueError):
    """Erro levantado quando o tenant_key da operação diverge do tenant_key da proposta/snapshot."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _is_api_auth_error(exc: Exception) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return exc.response.status_code in {401, 403}
    return False


class SyncEngine:
    """Orquestra diff determinístico, guardrails, propostas, execução API/Playwright e auditoria."""

    def __init__(
        self,
        config: Config,
        storage: SyncStorage | None = None,
        *,
        api_client_factory: ApiClientFactory | None = None,
        playwright_factory: PlaywrightFactory | None = None,
        sheets_client_factory: SheetsClientFactory | None = None,
    ) -> None:
        self.config = config
        self.storage = storage or SyncStorage(config.db_path)
        self.api_client_factory = api_client_factory
        self.playwright_factory = playwright_factory
        self.sheets_client_factory = sheets_client_factory
        self._last_observation_detail: dict[str, Any] = {}

    def _make_api_client(self) -> ApiClientProtocol:
        if self.api_client_factory is not None:
            return self.api_client_factory(self.config)
        return IfoodClient(self.config, allow_empty_merchant_id=True)

    def _make_playwright(self) -> PlaywrightExecutorProtocol:
        if self.playwright_factory is not None:
            return self.playwright_factory(self.config)
        return PlaywrightStoreExecutor(self.config)

    def _make_sheets_client(self, tenant_key: str | None = None) -> GoogleSheetsClient | None:
        effective_cfg = self.config
        if tenant_key and tenant_key in self.config.tenants:
            tenant_sheet = self.config.tenants[tenant_key].sheet_id
            if tenant_sheet:
                effective_cfg = replace(self.config, sheets_spreadsheet_id=tenant_sheet)
        if self.sheets_client_factory is not None:
            return self.sheets_client_factory(effective_cfg)
        if is_sheets_api_configured(effective_cfg):
            return GoogleSheetsClient(effective_cfg)
        return None

    def _select_backend(
        self, store: SheetStoreConfig, domain: ChangeDomain, payload: Any = None
    ) -> tuple[BackendUsed, str | None]:
        capability_set = set(self.config.write_capabilities)
        if store.execution_mode == ExecutionMode.DISABLED:
            return BackendUsed.NONE, "Execução desativada para este canal."
        if store.channel != StoreChannel.IFOOD:
            if (store.channel == StoreChannel.FOOD99
                    and store.execution_mode in {ExecutionMode.PLAYWRIGHT_ONLY, ExecutionMode.API_FIRST}
                    and store.portal_url
                    and domain.value in self.config.validated_portal_domains):
                return BackendUsed.PLAYWRIGHT, None
            return BackendUsed.NONE, f"Canal {store.channel.value} sem API ou portal validado para este domínio."
        needed: set[str] = set()
        blocked_reason: str | None = None
        if domain == ChangeDomain.OPENING_HOURS:
            needed = {"opening_hours"}
        elif domain == ChangeDomain.CATALOG and isinstance(payload, list):
            pass  # O executor escolhido valida as capacidades para cada campo alterado.
        elif domain == ChangeDomain.STORE_STATUS and isinstance(payload, dict):
            if payload.get("status_action") == "OPEN":
                blocked_reason = "A reabertura exige contrato de status e de interrupções confirmado para a conta piloto."
            else:
                needed = {"store_pause"}
        elif domain == ChangeDomain.PREP_TIME:
            needed = {"prep_time"}

        api_observable = (
            store.channel == StoreChannel.IFOOD
            and domain in _API_SUPPORTED_DOMAINS
            and bool(store.merchant_id)
            and has_ifood_api_credentials(self.config)
        )
        if api_observable and needed and not needed.issubset(capability_set):
            blocked_reason = blocked_reason or (
                "Capacidade de escrita não habilitada após validação do contrato e do merchant."
            )
        if api_observable and domain == ChangeDomain.CATALOG and not (
            {"catalog_price", "catalog_availability"} & capability_set
        ):
            blocked_reason = blocked_reason or (
                "Capacidade de escrita não habilitada após validação do contrato e do merchant."
            )
        portal_ready = bool(store.portal_url) and domain.value in self.config.validated_portal_domains

        if store.execution_mode == ExecutionMode.API_ONLY:
            if api_observable:
                return BackendUsed.IFOOD_API, blocked_reason
            msg = (
                f"Modo API_ONLY ativo, mas credenciais/merchant_id/domínio {domain.value} "
                f"indisponíveis para '{store.store_key}'."
            )
            return BackendUsed.NONE, msg

        if store.execution_mode == ExecutionMode.PLAYWRIGHT_ONLY:
            if portal_ready:
                portal_needed = needed
                portal_blocked = (
                    "Capacidade de escrita não habilitada após validação do contrato e do merchant."
                    if portal_needed and not portal_needed.issubset(capability_set)
                    else None
                )
                return BackendUsed.PLAYWRIGHT, portal_blocked
            return (
                BackendUsed.NONE,
                f"Modo PLAYWRIGHT_ONLY exige portal_url configurada para '{store.store_key}'.",
            )

        # A ausência de contrato validado bloqueia a capacidade. Portal não é
        # fallback até que domínio, conta e leitura/read-back sejam verificados.
        if api_observable:
            return BackendUsed.IFOOD_API, blocked_reason
        if portal_ready:
            portal_needed = needed
            portal_blocked = (
                "Capacidade de escrita não habilitada após validação do contrato e do merchant."
                if portal_needed and not portal_needed.issubset(capability_set)
                else None
            )
            return BackendUsed.PLAYWRIGHT, portal_blocked
        msg = (
            f"Nem API iFood nem portal_url para fallback Playwright estão disponíveis para "
            f"'{store.store_key}' ({domain.value})."
        )
        return BackendUsed.NONE, msg

    def _observe_current(
        self,
        *,
        store: SheetStoreConfig,
        domain: ChangeDomain,
        backend: BackendUsed,
        desired_payload: Any,
        last_snapshot: dict[str, Any] | None,
    ) -> tuple[Any, bool]:
        """Lê estado atual via API ou Playwright quando disponível, ou usa último snapshot como referência."""
        fallback = last_snapshot["last_state"] if last_snapshot else None
        self._last_observation_detail = {}

        if backend == BackendUsed.PLAYWRIGHT:
            executor = self._make_playwright()
            read_fn = getattr(executor, "read_domain_state", None)
            if not callable(read_fn):
                self._last_observation_detail = {
                    "outcome": SyncOutcome.UNSUPPORTED.value,
                    "reason": "Executor Playwright sem suporte a leitura de estado.",
                }
                return fallback, False
            read_res = read_fn(
                store=store,
                domain=domain,
                desired_payload=desired_payload,
            )
            sess_status = str(read_res.get("session_status") or "").strip()
            if sess_status:
                last_err = (
                    None
                    if sess_status == "OK" and read_res.get("observed")
                    else str(read_res.get("reason") or "") or None
                )
                self.storage.update_portal_session_status(
                    tenant_key=store.tenant_key or "default",
                    channel=store.channel.value,
                    state=sess_status,
                    last_error=last_err,
                )
            if read_res.get("observed") is True:
                self._last_observation_detail = {"outcome": SyncOutcome.CONFIRMED.value, "reason": None}
                return read_res.get("state"), True
            self._last_observation_detail = {
                "outcome": str(read_res.get("outcome") or SyncOutcome.BLOCKED_GUARDRAIL.value),
                "reason": str(
                    read_res.get("reason")
                    or "Leitura atual completa e verificável indisponível no portal."
                ),
            }
            return fallback, False

        if backend != BackendUsed.IFOOD_API or not store.merchant_id:
            return fallback, False

        try:
            client = self._make_api_client()
            if domain == ChangeDomain.OPENING_HOURS:
                groups = client.opening_hours(merchant_id=store.merchant_id)
                if not isinstance(groups, list) or not groups:
                    return fallback, False
                if any(
                    not isinstance(group, dict)
                    or not isinstance(group.get("shifts"), list)
                    or any(not isinstance(shift, dict) for shift in group["shifts"])
                    for group in groups
                ):
                    return fallback, False
                shifts = [shift for group in groups for shift in group["shifts"]]
                # The current representation cannot express closed weekdays,
                # so an incomplete seven-day read is not a safe baseline.
                weekdays = {shift.get("dayOfWeek") for shift in shifts}
                if len(weekdays) != 7:
                    return fallback, False
                return normalize_shifts(shifts), True

            if domain == ChangeDomain.CATALOG:
                catalogs = client.list_catalogs(merchant_id=store.merchant_id)
                if not catalogs:
                    return fallback, False
                # Uma loja pode publicar itens em vários catálogos (delivery,
                # retirada etc.). O estado observado precisa abranger todos.
                by_id: dict[str, dict[str, Any]] = {}
                for catalog in catalogs:
                    catalog_id = catalog.get("id")
                    if not catalog_id:
                        continue
                    for it in client.list_catalog_items(
                        str(catalog_id), merchant_id=store.merchant_id
                    ):
                        if isinstance(it, dict):
                            item_id = str(it.get("id") or it.get("externalCode") or "")
                            if item_id:
                                by_id[item_id] = it
                observed: list[dict[str, Any]] = []
                for desired_item in desired_payload:
                    iid = str(desired_item["item_id"])
                    live = by_id.get(iid)
                    if live is None:
                        continue
                    price_obj = live.get("price") or {}
                    price_val = price_obj.get("value")
                    orig_val = price_obj.get("originalValue")
                    observed.append(
                        {
                            "item_id": iid,
                            "item_name": str(live.get("name") or ""),
                            "description": live.get("description"),
                            "price_cents": round(float(price_val) * 100)
                            if price_val is not None
                            else None,
                            "original_price_cents": round(float(orig_val) * 100)
                            if orig_val is not None
                            else None,
                            "available": str(live.get("status", "")).upper() == "AVAILABLE"
                            if live.get("status")
                            else None,
                        }
                    )
                if len(observed) != len(desired_payload):
                    return fallback, False
                if any(
                    row.get("price_cents") is None and row.get("available") is None
                    for row in observed
                ):
                    return fallback, False
                return observed, bool(observed)

            if domain == ChangeDomain.STORE_STATUS:
                if desired_payload.get("status_action") == "OPEN":
                    # The interruptions endpoint alone cannot prove that the
                    # merchant status is reopenable or actually open.
                    return fallback, False
                interruptions = client.list_interruptions(merchant_id=store.merchant_id)
                normalized_interruptions = sorted(
                    ({key: row.get(key) for key in ("id", "description", "start", "end")}
                     for row in interruptions),
                    key=lambda row: str(row.get("id") or ""),
                )
                return {
                    "status_action": "PAUSE" if interruptions else "OPEN",
                    "active_interruptions": len(interruptions),
                    "interruptions": normalized_interruptions,
                }, True
        except Exception as exc:
            if _is_api_auth_error(exc):
                self._last_observation_detail = {
                    "outcome": "API_AUTH_ERROR",
                    "reason": str(exc) or type(exc).__name__,
                    "api_auth_error": True,
                }
            return fallback, False

        return fallback, False

    def _evaluate_guardrails(
        self,
        *,
        domain: ChangeDomain,
        current_state: Any,
        proposed_state: Any,
        backend: BackendUsed,
    ) -> list[str]:
        warnings: list[str] = []
        if domain == ChangeDomain.OPENING_HOURS:
            warnings.append(
                "A grade será substituída por completo; confira os sete dias e a diferença antes de aprovar."
            )
        if (
            domain == ChangeDomain.STORE_STATUS
            and isinstance(proposed_state, dict)
            and proposed_state.get("status_action") == "OPEN"
        ):
            warnings.append(
                "Reabrir remove interrupções ativas; confirme o efeito e a identidade da loja."
            )
        if (
            domain == ChangeDomain.STORE_STATUS
            and isinstance(proposed_state, dict)
            and proposed_state.get("status_action") == "PAUSE"
            and isinstance(current_state, dict)
            and current_state.get("active_interruptions", 0) > 0
        ):
            warnings.append("Já há interrupção ativa; uma nova pausa requer revisão do estado atual.")
        if domain == ChangeDomain.CATALOG and isinstance(proposed_state, list):
            prev_by_id: dict[str, dict[str, Any]] = {}
            if isinstance(current_state, list):
                for row in current_state:
                    if isinstance(row, dict) and row.get("item_id"):
                        prev_by_id[str(row["item_id"])] = row
            for item in proposed_state:
                iid = str(item.get("item_id") or "")
                new_cents = item.get("price_cents")
                prev_cents = (prev_by_id.get(iid) or {}).get("price_cents")
                if (
                    isinstance(prev_cents, int)
                    and prev_cents > 0
                    and isinstance(new_cents, int)
                ):
                    delta_pct = abs(new_cents - prev_cents) / prev_cents * 100.0
                    if delta_pct > self.config.max_price_change_pct:
                        warnings.append(
                            f"Item '{iid}' ({item.get('item_name', iid)}): alteração de preço "
                            f"de {delta_pct:.1f}% excede o limite de "
                            f"{self.config.max_price_change_pct:.1f}% "
                            f"(de R$ {prev_cents / 100:.2f} para R$ {new_cents / 100:.2f})."
                        )

        if (
            domain == ChangeDomain.STORE_STATUS
            and isinstance(proposed_state, dict)
            and proposed_state.get("status_action") == "PAUSE"
        ):
            if not str(proposed_state.get("pause_reason") or "").strip():
                warnings.append(
                    "Pausa da loja requer preenchimento de 'pause_reason' na aba Lojas."
                )
            if backend == BackendUsed.IFOOD_API and (
                not proposed_state.get("pause_start_iso")
                or not proposed_state.get("pause_end_iso")
            ):
                warnings.append(
                    "Pausa via API oficial iFood requer 'pause_start_iso' e 'pause_end_iso' (ISO 8601)."
                )
        return warnings

    def _compute_diff(
        self, domain: ChangeDomain, current_state: Any, proposed_state: Any
    ) -> dict[str, Any]:
        if domain == ChangeDomain.OPENING_HOURS:
            curr_list = current_state if isinstance(current_state, list) else []
            return {
                "before_count": len(curr_list),
                "after_count": len(proposed_state) if isinstance(proposed_state, list) else 0,
                "changed": curr_list != proposed_state,
            }
        if domain == ChangeDomain.CATALOG and isinstance(proposed_state, list):
            curr_map = {
                str(r["item_id"]): r
                for r in (current_state or [])
                if isinstance(r, dict) and "item_id" in r
            }
            changed_items: list[dict[str, Any]] = []
            for item in proposed_state:
                iid = str(item["item_id"])
                prev = curr_map.get(iid)
                if prev != item:
                    changed_items.append({"item_id": iid, "before": prev, "after": item})
            return {"changed_items": changed_items, "total_changed": len(changed_items)}
        return {"before": current_state, "after": proposed_state}

    @staticmethod
    def _required_capabilities(domain: ChangeDomain, current: Any, proposed: Any) -> set[str]:
        if domain == ChangeDomain.CATALOG and isinstance(proposed, list):
            previous = {str(row["item_id"]): row for row in current or []
                        if isinstance(row, dict) and row.get("item_id")}
            required = set()
            for item in proposed:
                old = previous.get(str(item.get("item_id")), {})
                if item.get("price_cents") is not None and item.get("price_cents") != old.get("price_cents"):
                    required.add("catalog_price")
                if item.get("available") is not None and item.get("available") != old.get("available"):
                    required.add("catalog_availability")
            return required
        if domain == ChangeDomain.OPENING_HOURS:
            return {"opening_hours"}
        if domain == ChangeDomain.STORE_STATUS:
            return {"store_open" if isinstance(proposed, dict) and proposed.get("status_action") == "OPEN"
                    else "store_pause"}
        if domain == ChangeDomain.PREP_TIME:
            return {"prep_time"}
        return set()

    def _execute_write(
        self,
        *,
        store: SheetStoreConfig,
        domain: ChangeDomain,
        backend: BackendUsed,
        proposed_payload: Any,
    ) -> dict[str, Any]:
        if backend == BackendUsed.PLAYWRIGHT:
            executor = self._make_playwright()
            res = executor.execute_domain_update(
                store=store, domain=domain, proposed_payload=proposed_payload
            )
            outcome_val = str(res.get("outcome") or "")
            if outcome_val == SyncOutcome.CONFIRMED.value:
                self.storage.update_portal_session_status(
                    tenant_key=store.tenant_key or "default",
                    channel=store.channel.value,
                    state="OK",
                    last_error=None,
                )
            elif outcome_val == SyncOutcome.LOGIN_REQUIRED.value:
                self.storage.update_portal_session_status(
                    tenant_key=store.tenant_key or "default",
                    channel=store.channel.value,
                    state="LOGIN_REQUIRED",
                    last_error=str(res.get("message") or ""),
                )
            elif outcome_val in {
                SyncOutcome.CAPTCHA_OR_BOT_BLOCK.value,
                SyncOutcome.ERROR.value,
                SyncOutcome.UNCERTAIN.value,
            }:
                self.storage.update_portal_session_status(
                    tenant_key=store.tenant_key or "default",
                    channel=store.channel.value,
                    state="EXPIRED" if outcome_val == SyncOutcome.CAPTCHA_OR_BOT_BLOCK.value else "OK",
                    last_error=str(res.get("message") or ""),
                )
            return res

        if backend != BackendUsed.IFOOD_API:
            return {
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "message": "Nenhum backend disponível para execução.",
                "observed": None,
            }

        client = self._make_api_client()
        merchant_id = store.merchant_id
        any_write_succeeded = False
        try:
            if domain == ChangeDomain.OPENING_HOURS:
                client.put_opening_hours(proposed_payload, merchant_id=merchant_id)
                any_write_succeeded = True
                observed_raw = client.opening_hours(merchant_id=merchant_id)
                observed = normalize_shifts(
                    [s for g in observed_raw for s in g.get("shifts", [])]
                )
                matched = shifts_digest(observed) == shifts_digest(proposed_payload)
                return {
                    "outcome": (
                        SyncOutcome.CONFIRMED.value if matched else SyncOutcome.DIVERGENT.value
                    ),
                    "message": (
                        "Horários gravados e confirmados via readback."
                        if matched
                        else "Leitura posterior divergiu da planilha."
                    ),
                    "observed": observed,
                }

            if domain == ChangeDomain.CATALOG:
                # Ainda não existe escrita segura para nome/descrição pelos
                # métodos de patch disponíveis. Recuse antes de alterar preço
                # ou disponibilidade para evitar aplicação parcial silenciosa.
                current, live = self._observe_current(
                    store=store, domain=domain, backend=backend,
                    desired_payload=proposed_payload, last_snapshot=None,
                )
                if not live:
                    if self._last_observation_detail.get("api_auth_error"):
                        raise PermissionError(
                            str(self._last_observation_detail.get("reason") or "Autorização iFood API recusada.")
                        )
                    return {
                        "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                        "message": "Não foi possível ler o catálogo antes de validar os campos solicitados.",
                        "observed": None,
                    }
                current_by_id = {
                    str(item.get("item_id")): item
                    for item in current
                    if isinstance(item, dict) and item.get("item_id")
                }
                for item in proposed_payload:
                    live_item = current_by_id.get(str(item.get("item_id")), {})
                    for field in ("item_name", "description"):
                        requested = item.get(field)
                        if field == "item_name" and requested == str(item.get("item_id")):
                            continue  # valor padrão quando a coluna está vazia
                        if requested is not None and requested != live_item.get(field):
                            return {
                                "outcome": SyncOutcome.UNSUPPORTED.value,
                                "message": f"Alteração de {field} do item {item.get('item_id')} não é suportada pelo executor atual.",
                                "observed": current,
                            }
                for item in proposed_payload:
                    iid = str(item["item_id"])
                    live_item = current_by_id.get(iid, {})
                    price_cents_req = item.get("price_cents")
                    orig = item.get("original_price_cents")
                    price_changed = (
                        price_cents_req is not None
                        and price_cents_req != live_item.get("price_cents")
                    )
                    orig_changed = (
                        orig is not None
                        and orig != live_item.get("original_price_cents")
                    )
                    if price_changed or orig_changed:
                        fallback_cents = int(live_item.get("price_cents") or 0)
                        effective_cents = (
                            int(price_cents_req) if price_cents_req is not None else fallback_cents
                        )
                        client.patch_item_price(
                            item_id=iid,
                            price_brl=effective_cents / 100.0,
                            original_price_brl=(int(orig) / 100.0) if orig is not None else None,
                            merchant_id=merchant_id,
                        )
                        any_write_succeeded = True
                    avail_req = item.get("available")
                    if avail_req is not None and avail_req != live_item.get("available"):
                        client.patch_item_status(
                            item_id=iid,
                            available=bool(avail_req),
                            merchant_id=merchant_id,
                        )
                        any_write_succeeded = True
                observed, live = self._observe_current(
                    store=store, domain=domain, backend=backend,
                    desired_payload=proposed_payload, last_snapshot=None,
                )
                matched = live and self._matches_live_state(domain, observed, proposed_payload)
                return {
                    "outcome": SyncOutcome.CONFIRMED.value if matched else SyncOutcome.DIVERGENT.value,
                    "message": "Cardápio confirmado por leitura posterior." if matched else "Cardápio divergiu ou não pôde ser conferido.",
                    "observed": observed if live else None,
                }

            if domain == ChangeDomain.STORE_STATUS:
                action = proposed_payload.get("status_action")
                if action == "PAUSE":
                    before = client.list_interruptions(merchant_id=merchant_id)
                    if before:
                        return {
                            "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                            "message": "Já existe interrupção ativa; revise antes de criar outra.",
                            "observed": {"active_interruptions": len(before)},
                        }
                    created = client.create_interruption(
                        description=proposed_payload.get("pause_reason") or "Pausa via Sheets",
                        start_iso=proposed_payload["pause_start_iso"],
                        end_iso=proposed_payload["pause_end_iso"],
                        merchant_id=merchant_id,
                    )
                    any_write_succeeded = True
                    interruptions = client.list_interruptions(merchant_id=merchant_id)
                    expected_reason = proposed_payload.get("pause_reason") or "Pausa via Sheets"
                    created_id = created.get("id") if isinstance(created, dict) else None
                    matches = [
                        item for item in interruptions
                        if (created_id and str(item.get("id")) == str(created_id))
                        or (item.get("description") == expected_reason
                            and item.get("start") == proposed_payload.get("pause_start_iso")
                            and item.get("end") == proposed_payload.get("pause_end_iso"))
                    ]
                    ok = len(matches) == 1
                    return {
                        "outcome": SyncOutcome.CONFIRMED.value if ok else SyncOutcome.UNCERTAIN.value,
                        "message": "Interrupção identificada via read-back." if ok else "Interrupção enviada sem read-back inequívoco; reconciliação obrigatória.",
                        "observed": {
                            "status_action": "PAUSE" if ok else "OPEN",
                            "active_interruptions": len(interruptions),
                        },
                    }
                if action == "OPEN":
                    status_raw = client.merchant_status(merchant_id=merchant_id)
                    status_rows = status_raw if isinstance(status_raw, list) else [status_raw]
                    if (len(status_rows) != 1 or not isinstance(status_rows[0], dict)
                            or status_rows[0].get("reopenable") is not True):
                        return {
                            "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                            "message": "Status atual não comprovou reopenable=true; nenhuma interrupção será removida.",
                            "observed": status_raw if isinstance(status_raw, (dict, list)) else None,
                        }
                    existing = client.list_interruptions(merchant_id=merchant_id)
                    for intr in existing:
                        intr_id = intr.get("id")
                        if intr_id:
                            client.delete_interruption(str(intr_id), merchant_id=merchant_id)
                            any_write_succeeded = True
                    remaining = client.list_interruptions(merchant_id=merchant_id)
                    ok = len(remaining) == 0
                    return {
                        "outcome": (
                            SyncOutcome.CONFIRMED.value if ok else SyncOutcome.UNCERTAIN.value
                        ),
                        "message": "Loja reaberta (interrupções removidas e confirmadas).",
                        "observed": {
                            "status_action": "OPEN" if ok else "PAUSE",
                            "active_interruptions": len(remaining),
                        },
                    }
        except Exception as exc:
            can_fallback_playwright = (
                not any_write_succeeded
                and _is_api_auth_error(exc)
                and store.channel == StoreChannel.IFOOD
                and store.execution_mode == ExecutionMode.API_FIRST
                and bool(store.portal_url)
                and domain.value in self.config.validated_portal_domains
            )
            if can_fallback_playwright:
                fb = self._execute_write(
                    store=store,
                    domain=domain,
                    backend=BackendUsed.PLAYWRIGHT,
                    proposed_payload=proposed_payload,
                )
                fb["fallback_backend"] = BackendUsed.PLAYWRIGHT.value
                fb["api_fallback_reason"] = str(exc) or type(exc).__name__
                return fb
            return {
                "outcome": SyncOutcome.UNCERTAIN.value,
                "message": f"Resultado da escrita não confirmado ({type(exc).__name__}); reconciliação obrigatória.",
                "observed": None,
            }

        return {
            "outcome": SyncOutcome.UNSUPPORTED.value,
            "message": f"Domínio {domain.value} não suportado pela API iFood.",
            "observed": None,
        }

    def _execute_write_serialized(
        self, *, store: SheetStoreConfig, domain: ChangeDomain,
        backend: BackendUsed, proposed_payload: Any, expected_current_hash: str,
        tenant_key: str = "default",
    ) -> dict[str, Any]:
        owner = str(uuid.uuid4())
        if not self.storage.acquire_sync_lock(
            store.store_key, domain.value, owner, tenant_key=tenant_key
        ):
            return {"outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                    "message": "Outra alteração está em execução para esta loja e domínio.", "observed": None}
        try:
            current, live = self._observe_current(
                store=store, domain=domain, backend=backend,
                desired_payload=proposed_payload, last_snapshot=None,
            )
            if not live or canonical_digest(current) != expected_current_hash:
                return {"outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                        "message": "Estado da loja mudou ou não pôde ser conferido após obter o bloqueio.",
                        "observed": current if live else None}
            return self._execute_write(store=store, domain=domain, backend=backend,
                                       proposed_payload=proposed_payload)
        finally:
            self.storage.release_sync_lock(
                store.store_key, domain.value, owner, tenant_key=tenant_key
            )

    def _write_back_to_sheets(
        self,
        results: list[dict[str, Any]],
        trigger_source: str,
        tenant_key: str | None = None,
    ) -> str | None:
        client = self._make_sheets_client(tenant_key=tenant_key)
        if client is None or not results:
            return None
        audit_rows = [
            [
                _utc_now(),
                trigger_source,
                r["store_key"],
                r["domain"],
                r["backend_used"],
                r["outcome"],
                r["message"],
                r.get("proposal_id") or "",
            ]
            for r in results
            if r["outcome"] != SyncOutcome.NO_CHANGES.value
        ]
        if not audit_rows:
            return None
        try:
            client.write_sync_feedback(audit_rows=audit_rows)
            return None
        except Exception as exc:
            return f"Aviso ao gravar retorno no Google Sheets ({type(exc).__name__})."

    def _matches_live_state(
        self, domain: ChangeDomain, current_state: Any, proposed_payload: Any
    ) -> bool:
        if current_state is None:
            return False
        if domain == ChangeDomain.OPENING_HOURS:
            return current_state == proposed_payload
        if domain == ChangeDomain.STORE_STATUS:
            if not isinstance(current_state, dict) or not isinstance(proposed_payload, dict):
                return False
            action = str(proposed_payload.get("status_action") or "")
            if action == "OPEN":
                if "active_interruptions" in current_state:
                    return (
                        current_state.get("status_verified") is True
                        and current_state.get("status_action") == "OPEN"
                    )
                return current_state.get("status_action") == "OPEN"
            if "interruptions" in current_state:
                interruptions = current_state.get("interruptions")
                if not isinstance(interruptions, list) or len(interruptions) != 1:
                    return False
                active = interruptions[0]
                return (
                    active.get("description")
                    == (proposed_payload.get("pause_reason") or "Pausa via Sheets")
                    and active.get("start") == proposed_payload.get("pause_start_iso")
                    and active.get("end") == proposed_payload.get("pause_end_iso")
                )
            return current_state.get("status_action") == action
        if domain == ChangeDomain.CATALOG and isinstance(current_state, list):
            curr_by_id = {
                str(r["item_id"]): r
                for r in current_state
                if isinstance(r, dict) and "item_id" in r
            }
            for item in proposed_payload:
                live = curr_by_id.get(str(item["item_id"]))
                if live is None:
                    return False
                if item.get("price_cents") is not None and live.get("price_cents") != item["price_cents"]:
                    return False
                if item.get("available") is not None and live.get("available") != item["available"]:
                    return False
                # O plano suporta estes campos, mas os patches atuais não os
                # gravam. Falhar o readback evita declarar sucesso incorreto.
                for key in ("item_name", "description", "original_price_cents"):
                    if item.get(key) is not None and live.get(key) != item[key]:
                        return False
            return True
        return current_state == proposed_payload

    def _state_domains_for_store(self, state: StoreDesiredState) -> list[ChangeDomain]:
        domains: list[ChangeDomain] = []
        if state.shifts:
            domains.append(ChangeDomain.OPENING_HOURS)
        if state.catalog_items or self.storage.get_snapshot(
            state.store.store_key, ChangeDomain.CATALOG.value, tenant_key=state.tenant_key
        ) is not None:
            domains.append(ChangeDomain.CATALOG)
        if state.store.status_action:
            domains.append(ChangeDomain.STORE_STATUS)
        if state.store.prep_time_minutes is not None:
            domains.append(ChangeDomain.PREP_TIME)
        return domains

    def _resolve_snapshot_tenant(
        self, snapshot: ParsedSheetSnapshot, requested_tenant_key: str | None
    ) -> str:
        store_tenants = {
            state.store.tenant_key
            for state in snapshot.stores.values()
            if state.store.tenant_key and state.store.tenant_key != "default"
        }
        if len(store_tenants) > 1:
            raise TenantMismatchError(
                f"Snapshot contém múltiplos tenant_key conflitantes: {sorted(store_tenants)}"
            )
        store_tenant = next(iter(store_tenants), None)
        snap_tenant = (
            snapshot.tenant_key
            if snapshot.tenant_key and snapshot.tenant_key != "default"
            else None
        )
        if snap_tenant and store_tenant and snap_tenant != store_tenant:
            raise TenantMismatchError(
                f"tenant_key do snapshot ('{snap_tenant}') diverge da loja ('{store_tenant}')."
            )
        derived = snap_tenant or store_tenant

        if requested_tenant_key is not None:
            cleaned = requested_tenant_key.strip() if isinstance(requested_tenant_key, str) else ""
            if not cleaned:
                raise ValueError("tenant_key não pode ser vazio.")
            if derived and derived != cleaned:
                raise TenantMismatchError(
                    f"tenant_key solicitado ('{cleaned}') diverge do snapshot ('{derived}')."
                )
            return cleaned

        if derived:
            return derived

        if snapshot.source_id:
            matching = [
                t_key
                for t_key, t_cfg in self.config.tenants.items()
                if t_cfg.sheet_id and t_cfg.sheet_id == snapshot.source_id
            ]
            if len(matching) == 1:
                return matching[0]

        if len(self.config.tenants) == 1:
            return next(iter(self.config.tenants))
        return snapshot.tenant_key or "default"

    def _resolve_request_tenant(self, requested_tenant_key: str | None) -> str:
        if requested_tenant_key is not None:
            cleaned = requested_tenant_key.strip() if isinstance(requested_tenant_key, str) else ""
            if not cleaned:
                raise ValueError("tenant_key não pode ser vazio.")
            return cleaned
        if len(self.config.tenants) == 1:
            return next(iter(self.config.tenants))
        return "default"

    def _load_proposal_for_tenant(
        self, proposal_id: str, tenant_key: str
    ) -> dict[str, Any]:
        prop = self.storage.get_proposal(proposal_id, tenant_key=tenant_key)
        if prop is not None:
            if prop.get("tenant_key") != tenant_key:
                raise TenantMismatchError(
                    f"Proposta '{proposal_id}' não pertence ao tenant '{tenant_key}'."
                )
            return prop
        actual_tenant = self.storage.get_proposal_tenant(proposal_id)
        if actual_tenant is not None and actual_tenant != tenant_key:
            raise TenantMismatchError(
                f"Proposta '{proposal_id}' pertence à padaria '{actual_tenant}', não a '{tenant_key}'."
            )
        raise KeyError(f"Proposta '{proposal_id}' não encontrada.")

    def process_snapshot(
        self,
        *,
        snapshot: ParsedSheetSnapshot,
        trigger_source: str,
        actor_id: str,
        tenant_key: str | None = None,
        dry_run: bool = False,
        write_back: bool = True,
    ) -> dict[str, Any]:
        resolved_tenant = self._resolve_snapshot_tenant(snapshot, tenant_key)
        results: list[dict[str, Any]] = []
        auto_candidates: list[tuple[dict[str, Any], int]] = []

        for store_key in sorted(snapshot.stores):
            state = snapshot.stores[store_key]
            store = state.store
            store.tenant_key = resolved_tenant
            state.tenant_key = resolved_tenant
            if not store.enabled:
                continue

            for domain in self._state_domains_for_store(state):
                proposed_payload = state.domain_payload(domain)
                if proposed_payload is None:
                    continue

                unresolved = self.storage.find_unresolved_execution(
                    store_key, domain.value, tenant_key=resolved_tenant
                )
                if unresolved is not None:
                    results.append({
                        "tenant_key": resolved_tenant,
                        "store_key": store_key,
                        "domain": domain.value,
                        "backend_used": unresolved["backend_planned"],
                        "outcome": "UNCERTAIN",
                        "message": "Há execução incerta. Reconcilie a proposta antes de criar outra escrita.",
                        "proposal_id": unresolved["proposal_id"],
                    })
                    continue

                source_sheet = {
                    ChangeDomain.OPENING_HOURS: "Horarios",
                    ChangeDomain.CATALOG: "Cardapio",
                    ChangeDomain.STORE_STATUS: "Lojas",
                    ChangeDomain.PREP_TIME: "Lojas",
                }[domain]
                blocking_errors = [
                    error for error in snapshot.row_errors
                    if error.get("sheet") == source_sheet
                    and (not error.get("store_key") or error.get("store_key") == store_key)
                ]
                if blocking_errors:
                    message = f"Sincronização bloqueada por {len(blocking_errors)} erro(s) na aba {source_sheet}."
                    self.storage.log_audit(
                        tenant_key=resolved_tenant,
                        actor_id=actor_id, store_key=store_key, domain=domain.value,
                        trigger_source=trigger_source, backend_used=BackendUsed.NONE.value,
                        outcome=SyncOutcome.BLOCKED_GUARDRAIL.value, summary=message,
                        details={"row_errors": blocking_errors},
                    )
                    results.append({
                        "tenant_key": resolved_tenant,
                        "store_key": store_key, "domain": domain.value,
                        "backend_used": BackendUsed.NONE.value,
                        "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                        "message": message, "row_errors": blocking_errors,
                        "proposal_id": None,
                    })
                    continue

                proposed_hash = canonical_digest(proposed_payload)
                last_snap = self.storage.get_snapshot(
                    store_key, domain.value, tenant_key=resolved_tenant
                )
                removal_due: list[str] = []
                if domain == ChangeDomain.CATALOG and isinstance(proposed_payload, list) and last_snap:
                    previous_items = last_snap["last_state"] if isinstance(last_snap["last_state"], list) else []
                    waiting, due, new_missing = self.storage.review_catalog_removals(
                        resolved_tenant, store_key, previous_items, proposed_payload
                    )
                    removal_due = due
                    for item_id in new_missing:
                        notify_event(
                            self.config, self.storage, resolved_tenant,
                            f"removal:{store_key}:{item_id}:{snapshot.snapshot_hash}",
                            "REMOVAL_GRACE",
                            f"{store_key}: item {item_id} ausente da planilha. Carência de 30 minutos iniciada; confirmação explícita será exigida.",
                        )
                    previous_by_id = {str(item["item_id"]): item for item in previous_items
                                      if isinstance(item, dict) and item.get("item_id")}
                    for item_id in waiting:
                        proposed_payload.append(dict(previous_by_id[item_id]))
                    for item_id in due:
                        pending_removal = dict(previous_by_id[item_id])
                        pending_removal["available"] = False
                        proposed_payload.append(pending_removal)
                    proposed_payload.sort(key=lambda item: item["item_id"])
                    proposed_hash = canonical_digest(proposed_payload)
                backend, backend_reason = self._select_backend(store, domain, proposed_payload)
                current_state, live_observed = self._observe_current(
                    store=store,
                    domain=domain,
                    backend=backend,
                    desired_payload=proposed_payload,
                    last_snapshot=last_snap,
                )
                api_fallback_reason: str | None = None
                if (
                    backend == BackendUsed.IFOOD_API
                    and not live_observed
                    and (self._last_observation_detail or {}).get("api_auth_error")
                    and store.channel == StoreChannel.IFOOD
                    and store.execution_mode == ExecutionMode.API_FIRST
                    and bool(store.portal_url)
                    and domain.value in self.config.validated_portal_domains
                ):
                    api_fallback_reason = str(
                        self._last_observation_detail.get("reason")
                        or "Falha de autorização na API iFood"
                    )
                    backend = BackendUsed.PLAYWRIGHT
                    backend_reason = None
                    current_state, live_observed = self._observe_current(
                        store=store,
                        domain=domain,
                        backend=backend,
                        desired_payload=proposed_payload,
                        last_snapshot=last_snap,
                    )
                current_hash = (
                    canonical_digest(current_state)
                    if current_state is not None
                    else ""
                )

                # Sem mudança quando já corresponde ao snapshot aplicado e ao estado live observado
                live_matches = live_observed and self._matches_live_state(
                    domain, current_state, proposed_payload
                )
                if (
                    last_snap is not None
                    and last_snap["last_applied_hash"] == proposed_hash
                    and live_observed
                    and live_matches
                ):
                    results.append(
                        {
                            "tenant_key": resolved_tenant,
                            "store_key": store_key,
                            "domain": domain.value,
                            "backend_used": BackendUsed.NONE.value,
                            "outcome": SyncOutcome.NO_CHANGES.value,
                            "message": "Nenhuma alteração detectada em relação à planilha.",
                            "proposal_id": None,
                        }
                    )
                    continue

                if live_matches:
                    self.storage.upsert_snapshot(
                        tenant_key=resolved_tenant,
                        store_key=store_key,
                        domain=domain.value,
                        last_applied_hash=proposed_hash,
                        last_state=proposed_payload,
                        last_backend=backend.value,
                        last_outcome=SyncOutcome.CONFIRMED.value,
                    )
                    results.append(
                        {
                            "tenant_key": resolved_tenant,
                            "store_key": store_key,
                            "domain": domain.value,
                            "backend_used": backend.value,
                            "outcome": SyncOutcome.NO_CHANGES.value,
                            "message": "Estado ao vivo já idêntico à planilha (snapshot sincronizado).",
                            "proposal_id": None,
                        }
                    )
                    continue

                if backend == BackendUsed.NONE:
                    self.storage.log_audit(
                        tenant_key=resolved_tenant,
                        actor_id=actor_id,
                        store_key=store_key,
                        domain=domain.value,
                        trigger_source=trigger_source,
                        backend_used=BackendUsed.NONE.value,
                        outcome=SyncOutcome.UNSUPPORTED.value,
                        summary=backend_reason or "Sem executor disponível.",
                    )
                    results.append(
                        {
                            "tenant_key": resolved_tenant,
                            "store_key": store_key,
                            "domain": domain.value,
                            "backend_used": BackendUsed.NONE.value,
                            "outcome": SyncOutcome.UNSUPPORTED.value,
                            "message": backend_reason or "Sem executor disponível.",
                            "proposal_id": None,
                        }
                    )
                    continue

                if not live_observed:
                    obs_detail = getattr(self, "_last_observation_detail", {}) or {}
                    obs_outcome_raw = str(obs_detail.get("outcome") or "")
                    obs_reason = str(obs_detail.get("reason") or "").strip()
                    if obs_outcome_raw in {
                        SyncOutcome.LOGIN_REQUIRED.value,
                        SyncOutcome.CAPTCHA_OR_BOT_BLOCK.value,
                        SyncOutcome.UNSUPPORTED.value,
                        SyncOutcome.ERROR.value,
                    }:
                        fail_outcome = obs_outcome_raw
                    else:
                        fail_outcome = SyncOutcome.BLOCKED_GUARDRAIL.value
                    message = (
                        obs_reason
                        or "Leitura atual completa e verificável indisponível; proposta e escrita bloqueadas."
                    )
                    self.storage.log_audit(
                        tenant_key=resolved_tenant,
                        actor_id=actor_id, store_key=store_key, domain=domain.value,
                        trigger_source=trigger_source, backend_used=backend.value,
                        outcome=fail_outcome, summary=message,
                        details={"source_version": snapshot.snapshot_hash},
                    )
                    results.append({
                        "tenant_key": resolved_tenant,
                        "store_key": store_key, "domain": domain.value,
                        "backend_used": backend.value,
                        "outcome": fail_outcome,
                        "message": message, "proposal_id": None,
                    })
                    continue

                warnings = self._evaluate_guardrails(
                    domain=domain,
                    current_state=current_state,
                    proposed_state=proposed_payload,
                    backend=backend,
                )
                if backend_reason:
                    warnings.append(backend_reason)
                required_caps = self._required_capabilities(domain, current_state, proposed_payload)
                if domain == ChangeDomain.CATALOG and required_caps and not required_caps.issubset(
                    self.config.write_capabilities
                ):
                    cap_msg = (
                        "Capacidade de escrita não habilitada após validação do contrato e do merchant."
                    )
                    if cap_msg not in warnings:
                        warnings.append(cap_msg)
                verified_caps = self.config.verified_store_capabilities(
                    resolved_tenant, store_key, store.channel.value, store.merchant_id,
                )
                if (store.sync_mode == SyncMode.AUTO_APPLY or store.channel == StoreChannel.FOOD99) and (
                    not required_caps or not required_caps.issubset(verified_caps)
                ):
                    warnings.append("Capacidade da loja/canal não verificada para esta escrita.")
                if removal_due:
                    warnings.append(
                        "Linha removida da planilha após carência de 30 minutos; confirmação explícita obrigatória: "
                        + ", ".join(removal_due)
                    )
                diff = self._compute_diff(domain, current_state, proposed_payload)

                # Política REVIEW obrigatória: origens externas não aprovam escritas.
                # AUTO_APPLY recebido continua criando uma proposta explícita.
                # Cria/reaproveita proposta (REVIEW, dry_run ou bloqueio de guardrail)
                proposal_status = "BLOCKED" if warnings else "PENDING"
                outcome_val = (
                    SyncOutcome.BLOCKED_GUARDRAIL.value
                    if warnings
                    else SyncOutcome.PENDING_REVIEW.value
                )
                summary_msg = (
                    f"Bloqueado por guardrail: {'; '.join(warnings)}"
                    if warnings
                    else f"Proposta criada para revisão ({backend.value})."
                )
                existing = self.storage.find_active_identical_proposal(
                    store_key, domain.value, proposed_hash, tenant_key=resolved_tenant
                )
                if existing is not None:
                    proposal = existing
                else:
                    proposal = self.storage.create_proposal(
                        tenant_key=resolved_tenant,
                        store_key=store_key,
                        merchant_id=store.merchant_id,
                        channel=store.channel.value,
                        domain=domain.value,
                        actor_id=actor_id,
                        trigger_source=trigger_source,
                        execution_mode=store.execution_mode.value,
                        backend_planned=backend.value,
                        status=proposal_status,
                        current_hash=current_hash,
                        proposed_hash=proposed_hash,
                        current_state=current_state,
                        proposed_state=proposed_payload,
                        diff=diff,
                        guardrail_warnings=warnings,
                        metadata={
                            "tenant_key": resolved_tenant,
                            "store_name": store.store_name,
                            "portal_url": store.portal_url,
                            "sheet_row": store.sheet_row,
                            "snapshot_hash": snapshot.snapshot_hash,
                            "source_version": snapshot.snapshot_hash,
                            "source_id": snapshot.source_id,
                            "policy": store.sync_mode.value,
                            "approval_required": bool(warnings) or store.sync_mode != SyncMode.AUTO_APPLY,
                            "api_fallback_reason": api_fallback_reason,
                        },
                        audit_event={
                            "actor_id": actor_id, "store_key": store_key,
                            "domain": domain.value, "trigger_source": trigger_source,
                            "backend_used": backend.value, "outcome": outcome_val,
                            "summary": summary_msg,
                        },
                    )
                if existing is not None:
                    self.storage.log_audit(
                        tenant_key=resolved_tenant,
                        actor_id=actor_id, store_key=store_key, domain=domain.value,
                        trigger_source=trigger_source, backend_used=backend.value,
                        outcome=outcome_val, summary=summary_msg,
                        details={"proposal_id": proposal["proposal_id"],
                                 "source_version": snapshot.snapshot_hash},
                    )
                results.append(
                    {
                        "tenant_key": resolved_tenant,
                        "store_key": store_key,
                        "domain": domain.value,
                        "backend_used": backend.value,
                        "outcome": outcome_val,
                        "message": summary_msg,
                        "proposal_id": proposal["proposal_id"],
                        "guardrail_warnings": warnings,
                    }
                )
                if (not dry_run and not warnings and store.sync_mode == SyncMode.AUTO_APPLY
                        and self.config.default_sync_mode == SyncMode.AUTO_APPLY.value):
                    units = max(1, diff.get("total_changed", 1))
                    auto_candidates.append((results[-1], units))

        auto_notified_proposals: set[str] = set()
        for row, units in auto_candidates:
            if not self.storage.claim_auto_write_budget(
                resolved_tenant, row["store_key"], units,
                self.config.max_auto_writes_per_minute,
            ):
                row.update(outcome=SyncOutcome.BLOCKED_GUARDRAIL.value,
                           message="Limite de escritas automáticas por minuto atingido.")
                self.storage.log_audit(
                    tenant_key=resolved_tenant, actor_id=actor_id,
                    store_key=row["store_key"], domain=row["domain"],
                    trigger_source=trigger_source, backend_used=row["backend_used"],
                    outcome=row["outcome"], summary=row["message"],
                    details={"proposal_id": row["proposal_id"], "automatic": True},
                )
                continue
            try:
                applied = self.confirm_proposal(
                    proposal_id=row["proposal_id"], actor_id=actor_id,
                    tenant_key=resolved_tenant, write_back=False, _automatic=True,
                )
                execution = applied["execution"]
                row.update(outcome=execution["outcome"], message=execution["message"])
                if execution["outcome"] in {
                    SyncOutcome.UNCERTAIN.value, SyncOutcome.LOGIN_REQUIRED.value,
                }:
                    auto_notified_proposals.add(str(row["proposal_id"]))
                else:
                    notify_event(
                        self.config, self.storage, resolved_tenant,
                        f"auto:{row['proposal_id']}:{execution['outcome']}", "AUTO_APPLY",
                        f"{row['store_key']} / {row['domain']}: {execution['outcome']} — {execution['message']}",
                    )
            except (PermissionError, ValueError) as exc:
                row.update(outcome=SyncOutcome.BLOCKED_GUARDRAIL.value,
                           message=f"Aplicação automática bloqueada: {exc}")
                self.storage.log_audit(
                    tenant_key=resolved_tenant, actor_id=actor_id,
                    store_key=row["store_key"], domain=row["domain"],
                    trigger_source=trigger_source, backend_used=row["backend_used"],
                    outcome=row["outcome"], summary=row["message"],
                    details={"proposal_id": row["proposal_id"], "automatic": True},
                )

        for row in results:
            if row.get("proposal_id") and row.get("outcome") in {
                SyncOutcome.PENDING_REVIEW.value, SyncOutcome.BLOCKED_GUARDRAIL.value,
            }:
                notify_event(
                    self.config, self.storage, resolved_tenant,
                    f"proposal:{row['proposal_id']}", "PROPOSAL",
                    f"{row['store_key']} / {row['domain']}: proposta {row['outcome']} criada para revisão.",
                )
            if (
                row.get("outcome") == SyncOutcome.LOGIN_REQUIRED.value
                and str(row.get("proposal_id") or "") not in auto_notified_proposals
            ):
                notify_event(
                    self.config, self.storage, resolved_tenant,
                    f"login:{row['store_key']}:{row['domain']}:{snapshot.snapshot_hash}",
                    "LOGIN_REQUIRED", f"{row['store_key']}: sessão do portal exige novo login.",
                )
        wb_warning = (
            self._write_back_to_sheets(results, trigger_source, tenant_key=resolved_tenant)
            if write_back
            else None
        )
        return {
            "tenant_key": resolved_tenant,
            "snapshot_hash": snapshot.snapshot_hash,
            "trigger_source": trigger_source,
            "row_errors": snapshot.row_errors,
            "results": results,
            "write_back_warning": wb_warning,
            "summary": (
                f"{len(results)} operação(ões) avaliada(s); "
                f"{len(snapshot.row_errors)} erro(s) de linha."
            ),
        }

    def confirm_proposal(
        self,
        *,
        proposal_id: str,
        actor_id: str,
        tenant_key: str | None = None,
        override_guardrail: bool = False,
        write_back: bool = True,
        _automatic: bool = False,
    ) -> dict[str, Any]:
        resolved_tenant = self._resolve_request_tenant(tenant_key)
        prop = self._load_proposal_for_tenant(proposal_id, resolved_tenant)
        if prop["status"] not in {"PENDING", "BLOCKED"}:
            raise ValueError(f"Proposta '{proposal_id}' já foi finalizada ({prop['status']}).")
        auto_authorized = (
            _automatic and self.config.default_sync_mode == SyncMode.AUTO_APPLY.value
            and prop["metadata"].get("policy") == SyncMode.AUTO_APPLY.value
            and not prop["guardrail_warnings"] and prop["status"] == "PENDING"
        )
        if not actor_id or (not auto_authorized and actor_id not in self.config.operator_ids):
            raise PermissionError("Aprovação exige identidade de operador autorizada.")
        if prop["merchant_id"] == "" or prop["channel"] not in {
            StoreChannel.IFOOD.value, StoreChannel.FOOD99.value,
        }:
            raise PermissionError("Canal ou identidade da loja sem capacidade de escrita validada.")
        if canonical_digest(prop["proposed"]) != prop["proposed_hash"]:
            raise ValueError("A proposta foi alterada após a revisão; gere uma nova proposta.")
        if canonical_digest(prop["current"]) != prop["current_hash"]:
            raise ValueError("O estado anterior da proposta não confere; execução bloqueada.")
        store_cfg = SheetStoreConfig(
            tenant_key=resolved_tenant,
            store_key=prop["store_key"],
            channel=StoreChannel(prop["channel"]),
            merchant_id=prop["merchant_id"],
            store_name=str(prop["metadata"].get("store_name") or prop["store_key"]),
            execution_mode=ExecutionMode(prop["execution_mode"]),
            portal_url=str(prop["metadata"].get("portal_url") or ""),
        )
        allowed_backend, reason = self._select_backend(
            store_cfg, ChangeDomain(prop["domain"]), prop["proposed"]
        )
        if reason:
            raise PermissionError(reason)
        if auto_authorized or prop["channel"] == StoreChannel.FOOD99.value:
            required = self._required_capabilities(
                ChangeDomain(prop["domain"]), prop["current"], prop["proposed"],
            )
            verified = self.config.verified_store_capabilities(
                resolved_tenant, prop["store_key"], prop["channel"], prop["merchant_id"],
            )
            if not required or not required.issubset(verified):
                raise PermissionError("Capacidade da loja/canal não verificada para esta escrita.")
        if allowed_backend.value != prop["backend_planned"] and not (
            prop["metadata"].get("api_fallback_reason")
            and prop["backend_planned"] == BackendUsed.PLAYWRIGHT.value
            and store_cfg.execution_mode == ExecutionMode.API_FIRST
            and bool(store_cfg.portal_url)
            and prop["domain"] in self.config.validated_portal_domains
        ):
            raise PermissionError(reason or "Capacidade de escrita não está habilitada.")
        if ChangeDomain(prop["domain"]) == ChangeDomain.CATALOG:
            req_caps = self._required_capabilities(
                ChangeDomain.CATALOG, prop["current"], prop["proposed"],
            )
            if req_caps and not req_caps.issubset(self.config.write_capabilities):
                raise PermissionError(
                    "Capacidade de escrita não habilitada após validação do contrato e do merchant."
                )
        if prop["guardrail_warnings"] and not override_guardrail:
            raise PermissionError(
                "Proposta possui alertas de guardrail; confirme com override_guardrail=True."
            )
        if not self.storage.claim_proposal(proposal_id, tenant_key=resolved_tenant):
            raise ValueError("Proposta expirada ou já em execução.")

        meta = prop["metadata"]
        store = SheetStoreConfig(
            tenant_key=resolved_tenant,
            store_key=prop["store_key"],
            channel=StoreChannel(prop["channel"]),
            merchant_id=prop["merchant_id"],
            store_name=str(meta.get("store_name") or prop["store_key"]),
            execution_mode=ExecutionMode(prop["execution_mode"]),
            portal_url=str(meta.get("portal_url") or ""),
            sheet_row=meta.get("sheet_row"),
        )
        domain = ChangeDomain(prop["domain"])
        backend = BackendUsed(prop["backend_planned"])
        proposed_payload = prop["proposed"]
        # The proposal id is also the durable execution/run id, so a stale
        # lease can be correlated with the operation it protects.
        lock_owner = proposal_id
        if not self.storage.acquire_sync_lock(
            store.store_key, domain.value, lock_owner, tenant_key=resolved_tenant
        ):
            self.storage.resolve_proposal(
                proposal_id, tenant_key=resolved_tenant, status="PENDING",
                outcome={"outcome": "BUSY", "message": "Outra alteração está em execução."},
            )
            raise ValueError("Outra alteração desta loja e domínio está em execução.")
        try:
            if self.storage.has_other_unresolved_execution(
                store.store_key, domain.value, proposal_id, tenant_key=resolved_tenant
            ):
                self.storage.resolve_proposal(
                    proposal_id, tenant_key=resolved_tenant, status="PENDING",
                    outcome={"outcome": "BUSY", "message": "Reconcilie a execução anterior antes de gravar."},
                )
                raise ValueError("Há outra execução ativa ou incerta para esta loja e domínio.")
            current, live = self._observe_current(
                store=store, domain=domain, backend=backend,
                desired_payload=proposed_payload, last_snapshot=None,
            )
            if not live or canonical_digest(current) != prop["current_hash"]:
                message = "Estado atual da loja mudou ou não pôde ser lido. Crie uma nova proposta."
                resolved = self.storage.resolve_proposal(
                    proposal_id, tenant_key=resolved_tenant, status="STALE",
                    outcome={"outcome": "STALE", "message": message},
                )
                raise ValueError(message if resolved else "Proposta perdeu a reserva de execução.")
            with approved_execution(
                execution_id=proposal_id, merchant_id=store.merchant_id,
                channel=store.channel.value, domain=domain,
                payload=proposed_payload, actor_id=actor_id,
            ):
                exec_res = self._execute_write(
                    store=store, domain=domain, backend=backend,
                    proposed_payload=proposed_payload,
                )
        except Exception:
            current_prop = self.storage.get_proposal(proposal_id, tenant_key=resolved_tenant)
            if current_prop and current_prop["status"] == "EXECUTING":
                self.storage.resolve_proposal(
                    proposal_id, tenant_key=resolved_tenant, status="UNCERTAIN",
                    outcome={"outcome": "ERROR", "message": "Execução interrompida; confira a loja antes de repetir."},
                    audit_event={
                        "actor_id": actor_id, "store_key": store.store_key,
                        "domain": domain.value, "trigger_source": (
                            "AUTO_APPLY" if auto_authorized else "MANUAL_CONFIRM"
                        ),
                        "backend_used": backend.value, "outcome": "UNCERTAIN",
                        "summary": "Execução interrompida; reconciliação obrigatória.",
                        "details": {"proposal_id": proposal_id, "approver_id": actor_id},
                    },
                )
                notify_event(
                    self.config, self.storage, resolved_tenant,
                    f"uncertain:{proposal_id}", "UNCERTAIN",
                    f"{store.store_key} / {domain.value}: execução incerta; reconciliação obrigatória.",
                )
            raise
        finally:
            self.storage.release_sync_lock(
                store.store_key, domain.value, lock_owner, tenant_key=resolved_tenant
            )
        outcome_str = exec_res["outcome"]
        effective_backend = (
            BackendUsed.PLAYWRIGHT
            if exec_res.get("fallback_backend") == BackendUsed.PLAYWRIGHT.value
            else backend
        )
        next_status = "APPLIED" if outcome_str == SyncOutcome.CONFIRMED.value else "DIVERGENT"
        if outcome_str in {
            SyncOutcome.LOGIN_REQUIRED.value,
            SyncOutcome.CAPTCHA_OR_BOT_BLOCK.value,
            SyncOutcome.ERROR.value,
            SyncOutcome.UNSUPPORTED.value,
        }:
            next_status = outcome_str
        elif outcome_str == SyncOutcome.UNCERTAIN.value:
            next_status = "UNCERTAIN"

        resolved = self.storage.resolve_proposal(
            proposal_id,
            tenant_key=resolved_tenant,
            status=next_status,
            outcome=exec_res,
            audit_event={
                "actor_id": actor_id, "store_key": store.store_key,
                "domain": domain.value, "trigger_source": (
                    "AUTO_APPLY" if auto_authorized else "MANUAL_CONFIRM"
                ),
                "backend_used": effective_backend.value, "outcome": outcome_str,
                "summary": exec_res["message"],
                "details": {"proposal_id": proposal_id, "approver_id": actor_id,
                            "override_guardrail": override_guardrail,
                            "source": prop["trigger_source"],
                            "source_version": meta.get("source_version"),
                            "current_hash": prop["current_hash"],
                            "proposed_hash": prop["proposed_hash"]},
            },
        )
        if resolved is None:
            raise ValueError("A proposta não está mais reservada para esta execução.")
        if outcome_str in {SyncOutcome.UNCERTAIN.value, SyncOutcome.LOGIN_REQUIRED.value}:
            notify_event(
                self.config, self.storage, resolved_tenant,
                f"execution:{proposal_id}:{outcome_str}", outcome_str,
                f"{store.store_key} / {domain.value}: execução {outcome_str}; confira a sessão e reconcilie antes de repetir.",
            )
        if outcome_str == SyncOutcome.CONFIRMED.value:
            self.storage.upsert_snapshot(
                tenant_key=resolved_tenant,
                store_key=store.store_key, domain=domain.value,
                last_applied_hash=prop["proposed_hash"], last_state=proposed_payload,
                last_backend=effective_backend.value, last_outcome=outcome_str,
            )
        wb_warning = None
        if write_back:
            wb_warning = self._write_back_to_sheets(
                [
                    {
                        "tenant_key": resolved_tenant,
                        "store_key": store.store_key,
                        "domain": domain.value,
                        "backend_used": effective_backend.value,
                        "outcome": outcome_str,
                        "message": exec_res["message"],
                        "proposal_id": proposal_id,
                    }
                ],
                "MANUAL_CONFIRM",
                tenant_key=resolved_tenant,
            )
        return {
            "proposal": resolved,
            "execution": exec_res,
            "write_back_warning": wb_warning,
        }

    def reconcile_uncertain(
        self,
        *,
        proposal_id: str,
        actor_id: str,
        tenant_key: str | None = None,
    ) -> dict[str, Any]:
        """Read-only reconciliation for interrupted or timed-out writes."""
        resolved_tenant = self._resolve_request_tenant(tenant_key)
        prop = self._load_proposal_for_tenant(proposal_id, resolved_tenant)
        if prop["status"] != "UNCERTAIN":
            raise ValueError("Somente execuções UNCERTAIN podem ser reconciliadas.")
        if not actor_id or actor_id not in self.config.operator_ids:
            raise PermissionError("Reconciliação exige identidade de operador autorizada.")
        meta = prop["metadata"]
        store = SheetStoreConfig(
            tenant_key=resolved_tenant,
            store_key=prop["store_key"], channel=StoreChannel(prop["channel"]),
            merchant_id=prop["merchant_id"], store_name=str(meta.get("store_name") or prop["store_key"]),
            execution_mode=ExecutionMode(prop["execution_mode"]),
            portal_url=str(meta.get("portal_url") or ""),
        )
        domain = ChangeDomain(prop["domain"])
        backend = BackendUsed(prop["backend_planned"])
        current, live = self._observe_current(
            store=store, domain=domain, backend=backend,
            desired_payload=prop["proposed"], last_snapshot=None,
        )
        if not live:
            raise ValueError("Leitura de reconciliação indisponível; execução permanece UNCERTAIN.")
        applied = self._matches_live_state(domain, current, prop["proposed"])
        if applied:
            status, outcome = "APPLIED", "CONFIRMED"
        elif canonical_digest(current) == prop["current_hash"]:
            status, outcome = "STALE", "NOT_APPLIED"
        else:
            status, outcome = "DIVERGENT", "DIVERGENT"
        resolved = self.storage.resolve_proposal(
            proposal_id, tenant_key=resolved_tenant, status=status,
            outcome={"outcome": outcome, "observed": current, "reconciled_by": actor_id},
            expected_status="UNCERTAIN",
            audit_event={
                "actor_id": actor_id, "store_key": store.store_key,
                "domain": domain.value, "trigger_source": "MANUAL_RECONCILE",
                "backend_used": backend.value, "outcome": outcome,
                "summary": f"Reconciliação read-only: {outcome}.",
                "details": {"proposal_id": proposal_id, "observed": current},
            },
        )
        if resolved is None:
            raise ValueError("A proposta mudou durante a reconciliação.")
        if applied:
            self.storage.upsert_snapshot(
                tenant_key=resolved_tenant,
                store_key=store.store_key, domain=domain.value,
                last_applied_hash=prop["proposed_hash"], last_state=prop["proposed"],
                last_backend=backend.value, last_outcome="CONFIRMED",
            )
        return {"proposal": resolved, "observed": current, "outcome": outcome}

    def reject_proposal(
        self,
        *,
        proposal_id: str,
        actor_id: str,
        tenant_key: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        resolved_tenant = self._resolve_request_tenant(tenant_key)
        prop = self._load_proposal_for_tenant(proposal_id, resolved_tenant)
        if prop["status"] not in {"PENDING", "BLOCKED"}:
            raise ValueError(f"Proposta '{proposal_id}' não está pendente.")
        outcome = {"outcome": "REJECTED", "reason": reason}
        resolved = self.storage.resolve_proposal(
            proposal_id, tenant_key=resolved_tenant, status="REJECTED", outcome=outcome,
            expected_status=("PENDING", "BLOCKED"),
            audit_event={
                "actor_id": actor_id, "store_key": prop["store_key"],
                "domain": prop["domain"], "trigger_source": "MANUAL_REJECT",
                "backend_used": BackendUsed.NONE.value, "outcome": "REJECTED",
                "summary": reason or "Proposta rejeitada pelo operador.",
                "details": {"proposal_id": proposal_id},
            },
        )
        if resolved is None:
            raise ValueError(f"Proposta '{proposal_id}' já está em execução ou foi finalizada.")
        return resolved or {}


def resolve_hermes_agent_actor() -> str | None:
    actor = os.getenv("IFOOD_LOJA_AGENT_OPERATOR_ID", "").strip()
    return actor or None


def build_portal_session_overview(
    config: Config,
    storage: SyncStorage,
    *,
    tenant_key: str | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Retorna o estado da sessão Playwright no overview por padaria (tenant_key) × canal:
    `OK` / `LOGIN_REQUIRED` / `EXPIRED` / `NÃO CONFIGURADO`, com `last_error`.
    """
    playwright_configured = bool(
        config.playwright_cdp_url or config.playwright_user_data_dir
    )
    tenant_keys = (
        [tenant_key.strip()]
        if tenant_key and tenant_key.strip()
        else sorted(config.tenants.keys() or ["default"])
    )
    channels = (
        StoreChannel.IFOOD.value,
        StoreChannel.FOOD99.value,
        StoreChannel.ANOTA_AI.value,
    )
    overview: dict[str, dict[str, dict[str, Any]]] = {}

    for t_key in tenant_keys:
        persisted_rows = storage.list_portal_session_status(t_key)
        persisted_by_channel = {row["channel"]: row for row in persisted_rows}
        audits = storage.list_audit_events(t_key, limit=100)
        tenant_map: dict[str, dict[str, Any]] = {}

        for ch in channels:
            if ch in persisted_by_channel:
                row = persisted_by_channel[ch]
                tenant_map[ch] = {
                    "tenant_key": t_key,
                    "channel": ch,
                    "state": row["state"],
                    "last_error": row["last_error"],
                    "updated_at": row["updated_at"],
                }
                continue

            ch_audits = [
                ev
                for ev in audits
                if ev.get("backend_used") == BackendUsed.PLAYWRIGHT.value
            ]
            # Se não houver registro persistido em portal_session_status, deriva da configuração/auditoria
            if not playwright_configured or ch != StoreChannel.IFOOD.value:
                tenant_map[ch] = {
                    "tenant_key": t_key,
                    "channel": ch,
                    "state": "NÃO CONFIGURADO",
                    "last_error": None,
                    "updated_at": None,
                }
                continue

            if not ch_audits:
                tenant_map[ch] = {
                    "tenant_key": t_key,
                    "channel": ch,
                    "state": "NÃO CONFIGURADO",
                    "last_error": None,
                    "updated_at": None,
                }
                continue

            latest = ch_audits[0]
            outcome = str(latest.get("outcome") or "")
            summary = str(latest.get("summary") or "")
            if outcome == SyncOutcome.LOGIN_REQUIRED.value:
                state = "EXPIRED" if "expirada" in summary.lower() else "LOGIN_REQUIRED"
                last_err: str | None = summary
            elif outcome in {
                SyncOutcome.CAPTCHA_OR_BOT_BLOCK.value,
                SyncOutcome.ERROR.value,
            }:
                state = "EXPIRED"
                last_err = summary
            elif outcome in {
                SyncOutcome.CONFIRMED.value,
                SyncOutcome.PENDING_REVIEW.value,
            }:
                state = "OK"
                last_err = None
            else:
                state = "OK"
                last_err = summary or None

            tenant_map[ch] = {
                "tenant_key": t_key,
                "channel": ch,
                "state": state,
                "last_error": last_err,
                "updated_at": latest.get("timestamp_utc"),
            }

        overview[t_key] = tenant_map

    return overview
