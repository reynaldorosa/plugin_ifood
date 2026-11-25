"""Configuração do plugin iFood/99Food com Google Sheets como fonte da verdade."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DEFAULT_PORTAL_DOMAINS: tuple[str, ...] = (
    "portal.ifood.com.br",
    "gestor.ifood.com.br",
    "merchant.99app.com",
)
_WRITE_CAPABILITIES = frozenset(
    {
        "opening_hours",
        "store_pause",
        "store_open",
        "catalog_price",
        "catalog_availability",
        "prep_time",
    }
)
_FORBIDDEN_TENANT_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "client_secret",
        "google_access_token",
        "google_service_account_file",
        "google_service_account_json",
        "password",
        "private_key",
        "secret",
        "sheets_webhook_secret",
        "token",
        "webhook_secret",
    }
)


def _default_data_dir_str() -> str:
    env_dir = os.getenv("IFOOD_LOJA_DATA_DIR", "").strip()
    if env_dir:
        return env_dir
    hermes_home = os.getenv("HERMES_HOME", "").strip()
    if hermes_home:
        return str(Path(hermes_home) / "data" / "ifood-loja")
    return str(Path(__file__).resolve().parent.parent / ".data" / "ifood-loja")


def _reject_secrets_in_mapping(obj: Any, path: str = "root") -> None:
    if isinstance(obj, dict):
        for raw_key, value in obj.items():
            key_norm = str(raw_key).strip().lower()
            if key_norm in _FORBIDDEN_TENANT_SECRET_KEYS or "secret" in key_norm or "token" in key_norm or "password" in key_norm or "private_key" in key_norm:
                raise ValueError(
                    f"Segredo proibido '{raw_key}' detectado em '{path}' na configuração de tenants."
                )
            _reject_secrets_in_mapping(value, f"{path}.{raw_key}")
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            _reject_secrets_in_mapping(item, f"{path}[{idx}]")


@dataclass(frozen=True)
class TenantConfig:
    """Configuração não-sensível de uma padaria (tenant)."""

    tenant_key: str
    sheet_id: str = ""
    telegram_chat_id: str = ""
    stores: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key in {"tenant_key", "sheet_id", "telegram_chat_id", "stores"}:
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in {"tenant_key", "sheet_id", "telegram_chat_id", "stores"}:
            return getattr(self, key)
        return default

    def __contains__(self, key: object) -> bool:
        return key in {"tenant_key", "sheet_id", "telegram_chat_id", "stores"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_key": self.tenant_key,
            "sheet_id": self.sheet_id,
            "telegram_chat_id": self.telegram_chat_id,
            "stores": dict(self.stores),
        }


def load_tenants_config(
    *,
    tenants_file: str | Path = "",
    tenants_json: str = "",
    default_sheet_id: str = "",
    default_merchant_id: str = "",
) -> dict[str, TenantConfig]:
    """Carrega configuração multi-padaria de arquivo JSON ou env JSON sem segredos."""
    raw_text = (tenants_json or "").strip()
    if not raw_text and tenants_file and str(tenants_file).strip():
        file_path = Path(str(tenants_file).strip())
        raw_text = file_path.read_text(encoding="utf-8").strip()

    if not raw_text:
        default_stores: dict[str, dict[str, Any]] = {}
        if default_merchant_id.strip():
            default_stores[default_merchant_id.strip()] = {
                "merchant_id": default_merchant_id.strip()
            }
        return {
            "default": TenantConfig(
                tenant_key="default",
                sheet_id=default_sheet_id.strip(),
                stores=default_stores,
            )
        }

    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("Configuração de tenants deve ser um objeto JSON não-vazio.")

    _reject_secrets_in_mapping(parsed)

    tenants: dict[str, TenantConfig] = {}
    for raw_tenant_key, raw_cfg in parsed.items():
        tenant_key = str(raw_tenant_key).strip()
        if not tenant_key:
            raise ValueError("Chave de tenant (padaria) não pode ser vazia.")
        if not isinstance(raw_cfg, dict):
            raise ValueError(f"Configuração do tenant '{tenant_key}' deve ser um objeto JSON.")
        allowed_keys = {"sheet_id", "telegram_chat_id", "stores"}
        unknown_keys = set(raw_cfg) - allowed_keys
        if unknown_keys:
            raise ValueError(
                f"Campos desconhecidos no tenant '{tenant_key}': {sorted(unknown_keys)}"
            )
        sheet_id = str(raw_cfg.get("sheet_id") or "").strip()
        raw_stores = raw_cfg.get("stores")
        if raw_stores is None:
            stores_dict: dict[str, dict[str, Any]] = {}
        elif isinstance(raw_stores, dict):
            stores_dict = {}
            for s_key, s_val in raw_stores.items():
                sk = str(s_key).strip()
                if not sk:
                    raise ValueError(f"store_key vazia em '{tenant_key}'.")
                if not isinstance(s_val, dict):
                    raise ValueError(f"Configuração da loja '{sk}' em '{tenant_key}' deve ser objeto.")
                stores_dict[sk] = dict(s_val)
        else:
            raise ValueError(f"Campo 'stores' do tenant '{tenant_key}' deve ser um objeto.")

        tenants[tenant_key] = TenantConfig(
            tenant_key=tenant_key,
            sheet_id=sheet_id,
            telegram_chat_id=str(raw_cfg.get("telegram_chat_id") or "").strip(),
            stores=stores_dict,
        )
    return tenants


@dataclass(frozen=True)
class Config:
    """Configuração imutável do plugin, mantendo compatibilidade posicional do piloto."""

    enabled: bool
    operator_ids: frozenset[str]
    merchant_id: str
    api_base: str
    access_token: str
    client_id: str = ""
    client_secret: str = ""
    data_dir: str = ""
    sheets_spreadsheet_id: str = ""
    sheets_webhook_secret: str = ""
    google_service_account_file: str = ""
    google_service_account_json: str = ""
    google_access_token: str = ""
    default_sync_mode: str = "REVIEW"
    default_execution_mode: str = "API_FIRST"
    max_price_change_pct: float = 40.0
    playwright_cdp_url: str = ""
    playwright_user_data_dir: str = ""
    playwright_headless: bool = True
    allowed_portal_domains: tuple[str, ...] = _DEFAULT_PORTAL_DOMAINS
    allow_http_for_tests: bool = False
    sheets_poll_seconds: int = 0
    agent_operator_id: str = ""
    # Empty by default: each value must be explicitly enabled only after its
    # contract and merchant authorization have been verified by an operator.
    write_capabilities: frozenset[str] = frozenset()
    validated_portal_domains: frozenset[str] = frozenset()
    tenants_file: str = ""
    tenants_json: str = ""
    tenants: dict[str, TenantConfig] = field(default_factory=dict)
    telegram_chat_id: str = ""
    portal_selectors_file: str = ""
    max_auto_writes_per_minute: int = 10
    anota_ai_api_token: str = ""
    anota_ai_api_base: str = "https://api-parceiros.anota.ai"

    def __post_init__(self) -> None:
        if not self.tenants:
            loaded = load_tenants_config(
                tenants_file=self.tenants_file,
                tenants_json=self.tenants_json,
                default_sheet_id=self.sheets_spreadsheet_id,
                default_merchant_id=self.merchant_id,
            )
            object.__setattr__(self, "tenants", loaded)

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir.strip() else Path(_default_data_dir_str())

    @property
    def db_path(self) -> Path:
        return self.resolved_data_dir / "ifood_sync.sqlite3"

    def get_tenant(self, tenant_key: str) -> TenantConfig:
        key = (tenant_key or "").strip()
        if not key or key not in self.tenants:
            raise KeyError(f"Tenant '{tenant_key}' não configurado.")
        return self.tenants[key]

    def verified_store_capabilities(self, tenant_key: str, store_key: str,
                                    channel: str, merchant_id: str) -> frozenset[str]:
        """Capacidades vinculadas à identidade exata da loja e ao canal."""
        tenant = self.tenants.get(tenant_key)
        entry = tenant.stores.get(store_key) if tenant else None
        if not isinstance(entry, dict):
            return frozenset()
        entry_channel = str(entry.get("channel") or "").strip().upper()
        if entry_channel != channel.strip().upper() or entry.get("merchant_id") != merchant_id:
            return frozenset()
        configured = entry.get("write_capabilities")
        if not isinstance(configured, list) or any(not isinstance(item, str) for item in configured):
            return frozenset()
        normalized = frozenset(item.strip().lower() for item in configured if item.strip())
        return normalized & self.write_capabilities

    @classmethod
    def from_env(cls) -> Config:
        sync_mode = os.getenv("IFOOD_LOJA_DEFAULT_SYNC_MODE", "REVIEW").strip().upper()
        if sync_mode not in {"REVIEW", "AUTO_APPLY"}:
            sync_mode = "REVIEW"

        exec_mode = os.getenv("IFOOD_LOJA_DEFAULT_EXECUTION_MODE", "API_FIRST").strip().upper()
        if exec_mode not in {"DISABLED", "API_FIRST", "API_ONLY", "PLAYWRIGHT_ONLY"}:
            exec_mode = "API_FIRST"

        try:
            max_pct = float(os.getenv("IFOOD_LOJA_MAX_PRICE_CHANGE_PCT", "40.0"))
        except ValueError:
            max_pct = 40.0
        max_pct = max(1.0, min(max_pct, 500.0))

        try:
            poll_sec = int(os.getenv("IFOOD_LOJA_SHEETS_POLL_SECONDS", "0"))
        except ValueError:
            poll_sec = 0
        poll_sec = max(0, poll_sec)
        try:
            auto_limit = int(os.getenv("IFOOD_LOJA_MAX_AUTO_WRITES_PER_MINUTE", "10"))
        except ValueError:
            auto_limit = 10
        auto_limit = max(1, min(auto_limit, 100))

        domains_raw = os.getenv("IFOOD_LOJA_ALLOWED_PORTAL_DOMAINS", "")
        domains = (
            tuple(d.strip().lower() for d in domains_raw.split(",") if d.strip())
            or _DEFAULT_PORTAL_DOMAINS
        )
        requested_capabilities = frozenset(
            item.strip().lower()
            for item in os.getenv("IFOOD_LOJA_VERIFIED_WRITE_CAPABILITIES", "").split(",")
            if item.strip()
        )
        validated_portal_domains = frozenset(
            item.strip().upper()
            for item in os.getenv("IFOOD_LOJA_VALIDATED_PORTAL_DOMAINS", "").split(",")
            if item.strip()
        )
        # Capacidade aprovada e domínio do portal são permissões independentes.
        write_capabilities = requested_capabilities & _WRITE_CAPABILITIES

        headless_raw = os.getenv("IFOOD_LOJA_PLAYWRIGHT_HEADLESS", "true").strip().lower()
        merchant_id = os.getenv("IFOOD_LOJA_MERCHANT_ID", "").strip()
        sheets_id = os.getenv("IFOOD_LOJA_SHEETS_ID", "").strip()
        tenants_file = os.getenv("IFOOD_LOJA_TENANTS_FILE", "").strip()
        tenants_json = os.getenv("IFOOD_LOJA_TENANTS_JSON", "").strip()
        tenants = load_tenants_config(
            tenants_file=tenants_file,
            tenants_json=tenants_json,
            default_sheet_id=sheets_id,
            default_merchant_id=merchant_id,
        )
        return cls(
            enabled=os.getenv("IFOOD_LOJA_ENABLED", "false").strip().lower() in {"1", "true", "yes"},
            operator_ids=frozenset(
                x.strip() for x in os.getenv("IFOOD_LOJA_OPERATORS", "").split(",") if x.strip()
            ),
            merchant_id=merchant_id,
            api_base=os.getenv("IFOOD_LOJA_API_BASE", "https://merchant-api.ifood.com.br").rstrip(
                "/"
            ),
            access_token=os.getenv("IFOOD_LOJA_ACCESS_TOKEN", "").strip(),
            client_id=os.getenv("IFOOD_LOJA_CLIENT_ID", "").strip(),
            client_secret=os.getenv("IFOOD_LOJA_CLIENT_SECRET", "").strip(),
            data_dir=_default_data_dir_str(),
            sheets_spreadsheet_id=sheets_id,
            sheets_webhook_secret=os.getenv("IFOOD_LOJA_SHEETS_WEBHOOK_SECRET", "").strip(),
            google_service_account_file=os.getenv(
                "IFOOD_LOJA_GOOGLE_SERVICE_ACCOUNT_FILE", ""
            ).strip(),
            google_service_account_json=os.getenv(
                "IFOOD_LOJA_GOOGLE_SERVICE_ACCOUNT_JSON", ""
            ).strip(),
            google_access_token=os.getenv("IFOOD_LOJA_GOOGLE_ACCESS_TOKEN", "").strip(),
            default_sync_mode=sync_mode,
            default_execution_mode=exec_mode,
            max_price_change_pct=max_pct,
            playwright_cdp_url=os.getenv("IFOOD_LOJA_PLAYWRIGHT_CDP_URL", "").strip(),
            playwright_user_data_dir=os.getenv(
                "IFOOD_LOJA_PLAYWRIGHT_USER_DATA_DIR", ""
            ).strip(),
            playwright_headless=headless_raw in {"1", "true", "yes"},
            allowed_portal_domains=domains,
            allow_http_for_tests=False,
            sheets_poll_seconds=poll_sec,
            agent_operator_id=os.getenv("IFOOD_LOJA_AGENT_OPERATOR_ID", "").strip(),
            write_capabilities=write_capabilities,
            validated_portal_domains=validated_portal_domains,
            tenants_file=tenants_file,
            tenants_json=tenants_json,
            tenants=tenants,
            telegram_chat_id=os.getenv("IFOOD_LOJA_TELEGRAM_CHAT_ID", "").strip(),
            portal_selectors_file=os.getenv("IFOOD_LOJA_PORTAL_SELECTORS_FILE", "").strip(),
            max_auto_writes_per_minute=auto_limit,
            anota_ai_api_token=os.getenv("IFOOD_LOJA_ANOTA_AI_TOKEN", "").strip(),
            anota_ai_api_base=os.getenv(
                "IFOOD_LOJA_ANOTA_AI_API_BASE", "https://api-parceiros.anota.ai"
            ).strip().rstrip("/"),
        )
