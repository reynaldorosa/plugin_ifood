"""Modelos de domínio para sincronização Google Sheets → iFood / 99Food."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StoreChannel(StrEnum):
    ANOTA_AI = "ANOTA_AI"
    IFOOD = "IFOOD"
    FOOD99 = "99FOOD"


class ExecutionMode(StrEnum):
    DISABLED = "DISABLED"
    API_FIRST = "API_FIRST"
    API_ONLY = "API_ONLY"
    PLAYWRIGHT_ONLY = "PLAYWRIGHT_ONLY"


class SyncMode(StrEnum):
    REVIEW = "REVIEW"
    AUTO_APPLY = "AUTO_APPLY"


class ChangeDomain(StrEnum):
    OPENING_HOURS = "OPENING_HOURS"
    CATALOG = "CATALOG"
    STORE_STATUS = "STORE_STATUS"
    PREP_TIME = "PREP_TIME"


class SyncOutcome(StrEnum):
    NO_CHANGES = "NO_CHANGES"
    PENDING_REVIEW = "PENDING_REVIEW"
    CONFIRMED = "CONFIRMED"
    DIVERGENT = "DIVERGENT"
    BLOCKED_GUARDRAIL = "BLOCKED_GUARDRAIL"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_OR_BOT_BLOCK = "CAPTCHA_OR_BOT_BLOCK"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"
    UNCERTAIN = "UNCERTAIN"


class BackendUsed(StrEnum):
    IFOOD_API = "IFOOD_API"
    PLAYWRIGHT = "PLAYWRIGHT"
    NONE = "NONE"


def canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_non_blank_tenant(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("tenant_key não pode ser vazio.")
    return cleaned


class SheetStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_key: str = Field(min_length=1, max_length=100)
    tenant_key: str = Field(default="default", min_length=1, max_length=100)
    channel: StoreChannel = StoreChannel.IFOOD
    merchant_id: str = ""
    store_name: str = ""
    execution_mode: ExecutionMode = ExecutionMode.API_FIRST
    sync_mode: SyncMode = SyncMode.REVIEW
    portal_url: str = ""
    enabled: bool = True
    prep_time_minutes: int | None = Field(default=None, ge=1, le=240)
    status_action: str | None = None  # "OPEN" | "PAUSE" | None
    pause_reason: str = ""
    pause_start_iso: str = ""
    pause_end_iso: str = ""
    sheet_row: int | None = None

    _check_tenant = field_validator("tenant_key")(_validate_non_blank_tenant)


class SheetCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_key: str = Field(min_length=1, max_length=100)
    tenant_key: str = Field(default="default", min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=128)
    item_name: str = ""
    description: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    original_price_cents: int | None = Field(default=None, ge=0)
    available: bool | None = None
    sheet_row: int | None = None

    _check_tenant = field_validator("tenant_key")(_validate_non_blank_tenant)


class StoreDesiredState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_key: str = Field(default="default", min_length=1, max_length=100)
    store: SheetStoreConfig
    shifts: list[dict[str, Any]] = Field(default_factory=list)
    catalog_items: list[SheetCatalogItem] = Field(default_factory=list)

    _check_tenant = field_validator("tenant_key")(_validate_non_blank_tenant)

    @model_validator(mode="after")
    def _sync_tenant_key(self) -> StoreDesiredState:
        if self.tenant_key == "default" and self.store.tenant_key != "default":
            self.tenant_key = self.store.tenant_key
        elif self.tenant_key != "default" and self.store.tenant_key == "default":
            self.store.tenant_key = self.tenant_key
        elif self.tenant_key != self.store.tenant_key:
            raise ValueError("tenant_key do estado diverge do tenant_key da loja.")
        for item in self.catalog_items:
            if item.tenant_key == "default":
                item.tenant_key = self.tenant_key
            elif item.tenant_key != self.tenant_key:
                raise ValueError("tenant_key do item diverge do tenant_key da loja.")
        return self

    def domain_payload(self, domain: ChangeDomain) -> Any:
        if domain == ChangeDomain.OPENING_HOURS:
            return sorted(
                (
                    {
                        "dayOfWeek": s["dayOfWeek"],
                        "start": s["start"],
                        "duration": s["duration"],
                    }
                    for s in self.shifts
                ),
                key=lambda s: (s["dayOfWeek"], s["start"], s["duration"]),
            )
        if domain == ChangeDomain.CATALOG:
            return sorted(
                (
                    {
                        "item_id": item.item_id,
                        "item_name": item.item_name,
                        "description": item.description,
                        "price_cents": item.price_cents,
                        "original_price_cents": item.original_price_cents,
                        "available": item.available,
                    }
                    for item in self.catalog_items
                ),
                key=lambda i: i["item_id"],
            )
        if domain == ChangeDomain.STORE_STATUS:
            if not self.store.status_action:
                return None
            return {
                "status_action": self.store.status_action,
                "pause_reason": self.store.pause_reason,
                "pause_start_iso": self.store.pause_start_iso,
                "pause_end_iso": self.store.pause_end_iso,
            }
        if domain == ChangeDomain.PREP_TIME:
            if self.store.prep_time_minutes is None:
                return None
            return {"prep_time_minutes": self.store.prep_time_minutes}
        return None


class SnapshotRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_key: str = Field(default="default", min_length=1, max_length=100)
    store_key: str = Field(min_length=1, max_length=100)
    domain: ChangeDomain
    last_applied_hash: str
    last_state: Any
    last_backend: BackendUsed = BackendUsed.NONE
    last_outcome: SyncOutcome = SyncOutcome.CONFIRMED
    updated_at: str = ""

    _check_tenant = field_validator("tenant_key")(_validate_non_blank_tenant)


class ProposalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=100)
    tenant_key: str = Field(default="default", min_length=1, max_length=100)
    store_key: str = Field(min_length=1, max_length=100)
    merchant_id: str = ""
    channel: StoreChannel = StoreChannel.IFOOD
    domain: ChangeDomain
    actor_id: str = Field(min_length=1, max_length=100)
    trigger_source: str = Field(min_length=1, max_length=100)
    execution_mode: ExecutionMode = ExecutionMode.API_FIRST
    backend_planned: BackendUsed = BackendUsed.NONE
    status: str = "PENDING"
    current_hash: str = ""
    proposed_hash: str = ""
    current: Any = None
    proposed: Any = None
    diff: dict[str, Any] = Field(default_factory=dict)
    guardrail_warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    expires_at: str | None = None
    resolved_at: str | None = None
    outcome: dict[str, Any] | None = None

    _check_tenant = field_validator("tenant_key")(_validate_non_blank_tenant)


class ParsedSheetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_key: str = Field(default="default", min_length=1, max_length=100)
    stores: dict[str, StoreDesiredState] = Field(default_factory=dict)
    row_errors: list[dict[str, Any]] = Field(default_factory=list)
    snapshot_hash: str = ""
    source_id: str = ""

    _check_tenant = field_validator("tenant_key")(_validate_non_blank_tenant)
