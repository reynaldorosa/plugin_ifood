"""Mapa de seletores de portal web por canal para leitura e escrita assistida via Playwright.

IMPORTANTE:
- Os seletores de portais reais (iFood, 99Food, Anota AI) NÃO foram inventados e
  permanecem intencionalmente vazios/não-validados até inspeção autorizada em conta piloto.
- Os seletores default (`data-hermes-*`) são os seletores canônicos do mock HTTP local
  usado pelos testes automatizados de leitura, proposta, execução e conferência (read-back).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True)
class PortalChannelSelectors:
    """Seletores DOM necessários para identidade da loja, leitura e escrita com read-back."""

    channel: str
    validated_real_portal: bool
    notes: str
    merchant_id_attr: str = ""
    merchant_id_selector: str = ""
    store_name_attr: str = ""
    store_name_selector: str = ""
    prep_time_input: str = ""
    prep_time_save_button: str = ""
    store_status_input: str = ""
    pause_reason_input: str = ""
    store_status_save_button: str = ""
    opening_hours_input: str = ""
    opening_hours_save_button: str = ""
    catalog_item_row_template: str = ""
    catalog_price_input: str = ""
    catalog_available_input: str = ""
    catalog_item_name: str = ""
    catalog_item_description: str = ""
    catalog_original_price_input: str = ""
    catalog_item_save_button: str = ""


# Seletores do portal mock local usado na suíte de testes automatizados (data-hermes-*).
MOCK_HERMES_SELECTORS: Final[PortalChannelSelectors] = PortalChannelSelectors(
    channel="MOCK_HERMES",
    validated_real_portal=False,
    notes="Seletores canônicos data-hermes-* do servidor mock HTTP local de testes.",
    merchant_id_attr="data-hermes-merchant-id",
    merchant_id_selector="[data-hermes-merchant-id]",
    store_name_attr="data-hermes-store-name",
    store_name_selector="[data-hermes-store-name]",
    prep_time_input='[data-hermes-field="prep_time_minutes"]',
    prep_time_save_button='[data-hermes-action="save-prep-time"]',
    store_status_input='[data-hermes-field="store_status"]',
    pause_reason_input='[data-hermes-field="pause_reason"]',
    store_status_save_button='[data-hermes-action="save-status"]',
    opening_hours_input='[data-hermes-field="opening_hours_json"]',
    opening_hours_save_button='[data-hermes-action="save-opening-hours"]',
    catalog_item_row_template='[data-hermes-item-id="{item_id}"]',
    catalog_price_input='[data-hermes-field="price_brl"]',
    catalog_available_input='[data-hermes-field="available"]',
    catalog_item_name='[data-hermes-field="item_name"]',
    catalog_item_description='[data-hermes-field="description"]',
    catalog_original_price_input='[data-hermes-field="original_price_brl"]',
    catalog_item_save_button='[data-hermes-action="save-item"]',
)


# Mapas dos portais reais: mantidos explicitamente vazios e documentados como NÃO VALIDADOS.
# Nunca inventar seletores de produção sem inspeção autorizada.
REAL_PORTAL_SELECTORS_UNVALIDATED: Final[dict[str, PortalChannelSelectors]] = {
    "IFOOD": PortalChannelSelectors(
        channel="IFOOD",
        validated_real_portal=False,
        notes=(
            "NÃO VALIDADO: Seletores reais do Portal do Parceiro iFood não estão mapeados. "
            "Requer inspeção autorizada em conta piloto antes de habilitar domínio real."
        ),
    ),
    "99FOOD": PortalChannelSelectors(
        channel="99FOOD",
        validated_real_portal=False,
        notes="NÃO VALIDADO: Seletores reais do portal 99Food não estão mapeados.",
    ),
    "ANOTA_AI": PortalChannelSelectors(
        channel="ANOTA_AI",
        validated_real_portal=False,
        notes="NÃO VALIDADO: Seletores reais do portal Anota AI não estão mapeados.",
    ),
}


# Por padrão, o executor usa os seletores data-hermes-* (mock local) para os canais em teste,
# enquanto os seletores reais permanecem documentados em REAL_PORTAL_SELECTORS_UNVALIDATED.
CHANNEL_PORTAL_SELECTORS: Final[dict[str, PortalChannelSelectors]] = {
    "IFOOD": PortalChannelSelectors(
        channel="IFOOD",
        validated_real_portal=False,
        notes=(
            "Default de teste local (data-hermes-*). Seletores do portal real iFood "
            "não estão validados (ver REAL_PORTAL_SELECTORS_UNVALIDATED['IFOOD'])."
        ),
        **{
            k: getattr(MOCK_HERMES_SELECTORS, k)
            for k in MOCK_HERMES_SELECTORS.__dataclass_fields__
            if k not in {"channel", "validated_real_portal", "notes"}
        },
    ),
    "99FOOD": REAL_PORTAL_SELECTORS_UNVALIDATED["99FOOD"],
    "ANOTA_AI": REAL_PORTAL_SELECTORS_UNVALIDATED["ANOTA_AI"],
}


def get_portal_selectors(
    channel: str, selectors_file: str = "", *, mock_portal: bool = False,
) -> PortalChannelSelectors:
    """Carrega mapa versionado. Seletores sintéticos só servem ao portal local de teste."""
    key = (channel or "").strip().upper()
    if mock_portal and key in {"IFOOD", "99FOOD", "ANOTA_AI"}:
        return PortalChannelSelectors(
            channel=key, validated_real_portal=False, notes="Portal local de teste.",
            **{name: getattr(MOCK_HERMES_SELECTORS, name)
               for name in PortalChannelSelectors.__dataclass_fields__
               if name not in {"channel", "validated_real_portal", "notes"}},
        )
    if not selectors_file:
        return REAL_PORTAL_SELECTORS_UNVALIDATED.get(
            key, PortalChannelSelectors(key or "UNKNOWN", False, "Canal sem seletores configurados."),
        )
    data = json.loads(Path(selectors_file).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("channels"), dict):
        raise ValueError("Arquivo de seletores deve ter version=1 e objeto channels.")
    entry = data["channels"].get(key)
    if entry is None:
        return REAL_PORTAL_SELECTORS_UNVALIDATED.get(
            key, PortalChannelSelectors(key or "UNKNOWN", False, "Canal sem seletores configurados."),
        )
    if not isinstance(entry, dict) or entry.get("channel") != key:
        raise ValueError(f"Seletores do canal {key} inválidos.")
    allowed = set(PortalChannelSelectors.__dataclass_fields__)
    if set(entry) - allowed or type(entry.get("validated_real_portal")) is not bool:
        raise ValueError(f"Campos ou homologação dos seletores de {key} inválidos.")
    if any(not isinstance(value, str) for name, value in entry.items() if name != "validated_real_portal"):
        raise ValueError(f"Seletores de {key} devem ser textos.")
    return PortalChannelSelectors(**entry)
