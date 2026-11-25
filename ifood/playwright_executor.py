"""Executor assistido via Playwright para leitura, proposta e escrita com conferência (read-back).

Regras estritas de segurança:
- Anti-SSRF: aceita apenas HTTPS nos domínios permitidos (ou HTTP loopback quando allow_http_for_tests=True).
- CDP restrito a loopback (127.0.0.1 / localhost).
- Reutiliza sessão já autenticada pelo operador; nunca tenta resolver CAPTCHA/WAF/2FA nem esconder automação.
- Leitura separada da escrita (`read_domain_state`): valida `detect_portal_gate()`, confirma a
  identidade inequívoca da loja (merchant_id e store_name quando presente) e só retorna
  `observed=True` quando a leitura for completa e inequívoca.
- Escrita (`execute_domain_update`): exige domínio validado em `validated_portal_domains`,
  capability explícita, estado observado antes de gravar, aplica uma alteração por vez e
  executa releitura imediata (read-back). Divergência ou erro/timeout durante a escrita ⇒ `UNCERTAIN`.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ifood.config import Config
from ifood.models import ChangeDomain, SheetStoreConfig, SyncOutcome
from ifood.operations import normalize as normalize_shifts
from ifood.portal_selectors import PortalChannelSelectors, get_portal_selectors
from ifood.write_policy import require_approved_write

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

_BOT_MARKERS = (
    "cf-turnstile",
    "challenge-platform",
    "just a moment",
    "attention required! | cloudflare",
    "incapsula",
    "imperva",
    "g-recaptcha",
    "h-captcha",
    "verify you are human",
)

_LOGIN_URL_PARTS = ("/login", "/signin", "/sign-in", "/auth", "/otp", "/2fa")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_portal_url(url: str, config: Config) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL do portal da loja não configurada.")
    parsed = urlparse(raw)
    if parsed.username or parsed.password:
        raise ValueError("URL do portal não pode conter credenciais embutidas.")
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("URL do portal sem hostname válido.")
    if not (
        scheme == "https"
        or (scheme == "http" and config.allow_http_for_tests and host in _LOOPBACK_HOSTS)
    ):
        raise ValueError(f"Esquema '{scheme or '?'}' não permitido para '{host}'.")

    allowed = False
    for domain in config.allowed_portal_domains:
        d = domain.strip().lower()
        if not d:
            continue
        if _is_ip_literal(d) or d == "localhost":
            if host == d:
                allowed = True
                break
        elif host == d or host.endswith(f".{d}"):
            allowed = True
            break
    if not allowed:
        raise ValueError(
            f"Domínio '{host}' fora da allowlist do portal ({', '.join(config.allowed_portal_domains)})."
        )
    return raw


def validate_cdp_url(cdp_url: str) -> str:
    raw = (cdp_url or "").strip()
    parsed = urlparse(raw)
    if parsed.username or parsed.password:
        raise ValueError("CDP URL não pode conter credenciais.")
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "ws"} or host not in _LOOPBACK_HOSTS or parsed.port is None:
        raise ValueError("CDP URL deve apontar apenas para loopback local com porta.")
    return raw


def detect_portal_gate(
    *,
    url: str,
    title: str,
    html: str,
    http_status: int | None = None,
) -> tuple[SyncOutcome | None, str | None]:
    title_l = (title or "").lower()
    body_l = (html or "").lower()
    combined = f"{title_l}\n{body_l}"

    if http_status in (403, 429):
        return (
            SyncOutcome.CAPTCHA_OR_BOT_BLOCK,
            f"Portal retornou HTTP {http_status} (anti-bot/rate limit). Interrompido sem contorno.",
        )

    for marker in _BOT_MARKERS:
        if marker in combined:
            return (
                SyncOutcome.CAPTCHA_OR_BOT_BLOCK,
                f"Desafio anti-bot/CAPTCHA detectado ({marker}). Interrompido sem contorno.",
            )

    parsed_url = urlparse((url or "").lower())
    if any(seg in parsed_url.path for seg in _LOGIN_URL_PARTS):
        return (
            SyncOutcome.LOGIN_REQUIRED,
            "Sessão do portal expirada ou redirecionada para login/2FA. Faça login manual e tente novamente.",
        )

    has_pwd = 'type="password"' in body_l or "type='password'" in body_l
    has_2fa = (
        "codigo de verificacao" in body_l
        or "verification code" in body_l
        or 'name="otp"' in body_l
    )
    if has_pwd or has_2fa:
        return (
            SyncOutcome.LOGIN_REQUIRED,
            "Portal exigiu autenticação ou 2FA. Faça login manualmente na sessão do operador.",
        )

    return None, None


class PlaywrightStoreExecutor:
    """Executor assistido de fallback no portal web (leitura e escrita com read-back)."""

    def __init__(self, config: Config, timeout_ms: int = 20000) -> None:
        self.config = config
        self.timeout_ms = max(5000, min(timeout_ms, 90000))

    def _profile_dir(self, store: SheetStoreConfig) -> str:
        profile_key = hashlib.sha256(
            f"{store.tenant_key}\0{store.store_key}\0{store.channel.value}".encode()
        ).hexdigest()[:24]
        return str(Path(self.config.playwright_user_data_dir).resolve() / profile_key)

    def _selectors(self, store: SheetStoreConfig, validated_url: str) -> PortalChannelSelectors:
        parsed = urlparse(validated_url)
        mock = self.config.allow_http_for_tests and parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS
        selectors = get_portal_selectors(
            store.channel.value, self.config.portal_selectors_file, mock_portal=mock,
        )
        if not mock and not selectors.validated_real_portal:
            raise ValueError(f"Seletores reais de {store.channel.value} não homologados.")
        return selectors

    @staticmethod
    def _verify_page_store_identity(
        *,
        page: Any,
        store: SheetStoreConfig,
        selectors: PortalChannelSelectors,
    ) -> str | None:
        """Valida a identidade da loja na página (merchant_id e store_name se presente).

        Retorna None quando a identidade é inequívoca, ou a mensagem de erro em caso de divergência.
        """
        if not selectors.merchant_id_selector or not selectors.merchant_id_attr:
            return (
                f"Seletores de identidade não configurados para o canal {store.channel.value}."
            )
        identity = page.locator(selectors.merchant_id_selector)
        count = identity.count()
        if count != 1:
            return (
                "O portal não identificou inequivocamente o merchant esperado "
                f"(elementos encontrados: {count})."
            )
        page_merchant = (
            identity.first.get_attribute(selectors.merchant_id_attr) or ""
        ).strip()
        if page_merchant != store.merchant_id:
            return (
                f"Identidade da loja divergente no portal: esperado merchant_id "
                f"'{store.merchant_id}', encontrado '{page_merchant or 'vazio'}'."
            )

        if selectors.store_name_selector and selectors.store_name_attr and store.store_name.strip():
            name_loc = page.locator(selectors.store_name_selector)
            if name_loc.count() > 0:
                page_store_name = (
                    name_loc.first.get_attribute(selectors.store_name_attr) or ""
                ).strip()
                if page_store_name and page_store_name != store.store_name.strip():
                    return (
                        f"Nome da loja divergente no portal: esperado '{store.store_name.strip()}', "
                        f"encontrado '{page_store_name}'."
                    )
        return None

    def read_domain_state(
        self,
        *,
        store: SheetStoreConfig,
        domain: ChangeDomain,
        desired_payload: Any = None,
    ) -> dict[str, Any]:
        """Operação de leitura separada da escrita.

        Retorna dicionário:
          - state: estado canônico observado (ou None)
          - observed: True somente quando a leitura é completa e inequívoca
          - outcome: SyncOutcome.CONFIRMED.value quando observada, ou código de bloqueio/erro
          - reason: motivo explícito quando observed=False
          - session_status: 'OK' | 'LOGIN_REQUIRED' | 'EXPIRED' | 'NÃO CONFIGURADO'
        """
        if store.channel.value not in {"IFOOD", "99FOOD"}:
            return {
                "state": None,
                "observed": False,
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "reason": f"Canal {store.channel.value} não possui adaptador Playwright suportado.",
                "session_status": "NÃO CONFIGURADO",
            }
        if domain.value not in self.config.validated_portal_domains:
            return {
                "state": None,
                "observed": False,
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "reason": (
                    f"Domínio '{domain.value}' não consta em validated_portal_domains; "
                    "leitura de portal bloqueada."
                ),
                "session_status": "NÃO CONFIGURADO",
            }
        if not store.merchant_id:
            return {
                "state": None,
                "observed": False,
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "reason": "merchant_id ausente; identidade da loja não pode ser verificada no portal.",
                "session_status": "NÃO CONFIGURADO",
            }
        try:
            validated_url = validate_portal_url(store.portal_url, self.config)
            validated_cdp = (
                validate_cdp_url(self.config.playwright_cdp_url)
                if self.config.playwright_cdp_url
                else None
            )
        except ValueError as exc:
            return {
                "state": None,
                "observed": False,
                "outcome": SyncOutcome.ERROR.value,
                "reason": str(exc),
                "session_status": "NÃO CONFIGURADO",
            }

        try:
            selectors = self._selectors(store, validated_url)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return {"state": None, "observed": False, "outcome": SyncOutcome.UNSUPPORTED.value,
                    "reason": str(exc), "session_status": "NÃO CONFIGURADO"}

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {
                "state": None,
                "observed": False,
                "outcome": SyncOutcome.ERROR.value,
                "reason": "Playwright não está instalado neste ambiente Python.",
                "session_status": "NÃO CONFIGURADO",
            }

        try:
            with sync_playwright() as p:
                browser = None
                context = None
                page = None
                close_context = False
                close_browser = False
                try:
                    if validated_cdp:
                        browser = p.chromium.connect_over_cdp(
                            validated_cdp, timeout=self.timeout_ms
                        )
                        context = (
                            browser.contexts[0] if browser.contexts else browser.new_context()
                        )
                        page = context.new_page()
                    elif self.config.playwright_user_data_dir:
                        context = p.chromium.launch_persistent_context(
                            self._profile_dir(store),
                            headless=self.config.playwright_headless,
                        )
                        close_context = True
                        page = context.new_page()
                    else:
                        browser = p.chromium.launch(headless=self.config.playwright_headless)
                        close_browser = True
                        context = browser.new_context()
                        close_context = True
                        page = context.new_page()

                    resp = page.goto(
                        validated_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    http_status = resp.status if resp is not None else None
                    page_url = page.url or validated_url
                    gate, gate_msg = detect_portal_gate(
                        url=page_url,
                        title=page.title() or "",
                        html=page.content() or "",
                        http_status=http_status,
                    )
                    if gate is not None:
                        is_redirect_expired = (
                            gate == SyncOutcome.LOGIN_REQUIRED
                            and page_url.rstrip("/") != validated_url.rstrip("/")
                        )
                        sess_state = (
                            "EXPIRED"
                            if is_redirect_expired
                            else (
                                "LOGIN_REQUIRED"
                                if gate == SyncOutcome.LOGIN_REQUIRED
                                else "EXPIRED"
                            )
                        )
                        return {
                            "state": None,
                            "observed": False,
                            "outcome": gate.value,
                            "reason": gate_msg or "Portal bloqueado por login/desafio.",
                            "session_status": sess_state,
                        }

                    identity_err = self._verify_page_store_identity(
                        page=page, store=store, selectors=selectors
                    )
                    if identity_err is not None:
                        return {
                            "state": None,
                            "observed": False,
                            "outcome": SyncOutcome.UNSUPPORTED.value,
                            "reason": identity_err,
                            "session_status": "OK",
                        }

                    state, observed, reason = self._extract_domain_state_from_page(
                        page=page,
                        domain=domain,
                        desired_payload=desired_payload,
                        selectors=selectors,
                    )
                    return {
                        "state": state if observed else None,
                        "observed": observed,
                        "outcome": (
                            SyncOutcome.CONFIRMED.value
                            if observed
                            else SyncOutcome.BLOCKED_GUARDRAIL.value
                        ),
                        "reason": reason,
                        "session_status": "OK",
                    }
                finally:
                    if page is not None:
                        with contextlib.suppress(Exception):
                            page.close()
                    if close_context and context is not None:
                        with contextlib.suppress(Exception):
                            context.close()
                    if close_browser and browser is not None:
                        with contextlib.suppress(Exception):
                            browser.close()
        except (PlaywrightError, TimeoutError, OSError) as exc:
            return {
                "state": None,
                "observed": False,
                "outcome": SyncOutcome.ERROR.value,
                "reason": f"Erro no navegador durante leitura Playwright ({type(exc).__name__}).",
                "session_status": "EXPIRED",
            }

    def _extract_domain_state_from_page(
        self,
        *,
        page: Any,
        domain: ChangeDomain,
        desired_payload: Any,
        selectors: PortalChannelSelectors,
    ) -> tuple[Any, bool, str | None]:
        """Extrai o estado canônico do domínio a partir da página já autenticada e identificada."""
        if domain == ChangeDomain.PREP_TIME:
            if not selectors.prep_time_input:
                return None, False, "Seletor de tempo de preparo não configurado para o canal."
            prep_input = page.locator(selectors.prep_time_input)
            if prep_input.count() != 1:
                return (
                    None,
                    False,
                    f"Campo de tempo de preparo ('{selectors.prep_time_input}') ausente ou ambíguo no portal.",
                )
            raw_val = (prep_input.first.input_value() or "").strip()
            if not raw_val:
                return None, False, "Campo de tempo de preparo está vazio no portal."
            try:
                minutes = int(raw_val)
            except ValueError:
                return (
                    None,
                    False,
                    f"Valor de tempo de preparo inválido no portal: '{raw_val}'.",
                )
            return {"prep_time_minutes": minutes}, True, None

        if domain == ChangeDomain.STORE_STATUS:
            if not selectors.store_status_input:
                return None, False, "Seletor de status da loja não configurado para o canal."
            select_el = page.locator(selectors.store_status_input)
            if select_el.count() != 1:
                return (
                    None,
                    False,
                    f"Campo de status da loja ('{selectors.store_status_input}') ausente no portal.",
                )
            status_val = (select_el.first.input_value() or "").strip().upper()
            if status_val not in {"OPEN", "PAUSE"}:
                return (
                    None,
                    False,
                    f"Status da loja inválido ou ilegível no portal: '{status_val}'.",
                )
            pause_reason = ""
            if selectors.pause_reason_input:
                reason_el = page.locator(selectors.pause_reason_input)
                if reason_el.count() > 0:
                    pause_reason = (reason_el.first.input_value() or "").strip()
            desired_dict = desired_payload if isinstance(desired_payload, dict) else {}
            return (
                {
                    "status_action": status_val,
                    "pause_reason": pause_reason,
                    "pause_start_iso": str(desired_dict.get("pause_start_iso") or ""),
                    "pause_end_iso": str(desired_dict.get("pause_end_iso") or ""),
                },
                True,
                None,
            )

        if domain == ChangeDomain.OPENING_HOURS:
            if not selectors.opening_hours_input:
                return None, False, "Seletor de horários não configurado para o canal."
            hours_input = page.locator(selectors.opening_hours_input)
            if hours_input.count() != 1:
                return (
                    None,
                    False,
                    f"Campo de horários ('{selectors.opening_hours_input}') ausente no portal.",
                )
            raw_json = (hours_input.first.input_value() or "").strip()
            if not raw_json:
                return None, False, "Campo de horários está vazio no portal."
            try:
                parsed = json.loads(raw_json)
            except ValueError:
                return None, False, "JSON de horários inválido no portal."
            if not isinstance(parsed, list) or not parsed:
                return None, False, "Lista de horários vazia ou inválida no portal."
            weekdays = {
                s.get("dayOfWeek") for s in parsed if isinstance(s, dict) and s.get("dayOfWeek")
            }
            if len(weekdays) != 7:
                return (
                    None,
                    False,
                    "Grade de horários incompleta no portal (menos de 7 dias lidos).",
                )
            return normalize_shifts(parsed), True, None

        if domain == ChangeDomain.CATALOG:
            if not isinstance(desired_payload, list) or not desired_payload:
                return None, False, "Lista de itens desejados vazia para leitura de catálogo."
            if not selectors.catalog_item_row_template:
                return None, False, "Seletor de item de catálogo não configurado para o canal."

            observed_items: list[dict[str, Any]] = []
            for desired_item in desired_payload:
                if not isinstance(desired_item, dict) or not desired_item.get("item_id"):
                    return None, False, "Item sem item_id no payload desejado."
                item_id = str(desired_item["item_id"])
                row_selector = selectors.catalog_item_row_template.format(item_id=item_id)
                row = page.locator(row_selector)
                if row.count() != 1:
                    return (
                        None,
                        False,
                        f"Item '{item_id}' não encontrado ou duplicado no DOM do portal.",
                    )

                # Se a proposta inclui preço, o campo de preço deve existir e ser numérico
                price_el = (
                    row.locator(selectors.catalog_price_input)
                    if selectors.catalog_price_input
                    else None
                )
                obs_price_cents: int | None = None
                if price_el is not None and price_el.count() == 1:
                    raw_price = (price_el.first.input_value() or "").strip()
                    if not raw_price:
                        return (
                            None,
                            False,
                            f"Campo de preço do item '{item_id}' está vazio no portal.",
                        )
                    try:
                        obs_price_cents = round(float(raw_price.replace(",", ".")) * 100)
                    except ValueError:
                        return (
                            None,
                            False,
                            f"Preço inválido ('{raw_price}') para o item '{item_id}' no portal.",
                        )
                elif desired_item.get("price_cents") is not None:
                    return (
                        None,
                        False,
                        f"Campo de preço ('{selectors.catalog_price_input}') ausente para o item '{item_id}' no portal.",
                    )

                avail_el = (
                    row.locator(selectors.catalog_available_input)
                    if selectors.catalog_available_input
                    else None
                )
                obs_avail: bool | None = None
                if avail_el is not None and avail_el.count() == 1:
                    obs_avail = bool(avail_el.first.is_checked())
                elif desired_item.get("available") is not None:
                    return (
                        None,
                        False,
                        f"Campo de disponibilidade ('{selectors.catalog_available_input}') ausente para o item '{item_id}' no portal.",
                    )

                if obs_price_cents is None and obs_avail is None:
                    return (
                        None,
                        False,
                        f"Nenhum campo (preço ou disponibilidade) encontrado para o item '{item_id}' no portal.",
                    )

                # Nome, descrição e preço original (opcionais; se presentes no DOM usa o DOM, senão preserva referência canônica)
                item_name = str(desired_item.get("item_name") or "")
                if selectors.catalog_item_name:
                    name_el = row.locator(selectors.catalog_item_name)
                    if name_el.count() == 1:
                        item_name = (
                            name_el.first.get_attribute("value")
                            or name_el.first.inner_text()
                            or ""
                        ).strip()

                description = desired_item.get("description")
                if selectors.catalog_item_description:
                    desc_el = row.locator(selectors.catalog_item_description)
                    if desc_el.count() == 1:
                        description = (
                            desc_el.first.get_attribute("value")
                            or desc_el.first.inner_text()
                            or ""
                        ).strip()

                orig_price_cents = desired_item.get("original_price_cents")
                if selectors.catalog_original_price_input:
                    orig_el = row.locator(selectors.catalog_original_price_input)
                    if orig_el.count() == 1:
                        raw_orig = (orig_el.first.input_value() or "").strip()
                        if raw_orig:
                            try:
                                orig_price_cents = round(
                                    float(raw_orig.replace(",", ".")) * 100
                                )
                            except ValueError:
                                return (
                                    None,
                                    False,
                                    f"Preço original inválido ('{raw_orig}') para o item '{item_id}'.",
                                )

                observed_items.append(
                    {
                        "item_id": item_id,
                        "item_name": item_name,
                        "description": description,
                        "price_cents": obs_price_cents,
                        "original_price_cents": orig_price_cents,
                        "available": obs_avail,
                    }
                )

            observed_sorted = sorted(observed_items, key=lambda i: i["item_id"])
            return observed_sorted, True, None

        return None, False, f"Domínio {domain.value} não suportado para leitura Playwright."

    def execute_domain_update(
        self,
        *,
        store: SheetStoreConfig,
        domain: ChangeDomain,
        proposed_payload: Any,
    ) -> dict[str, Any]:
        if store.channel.value not in {"IFOOD", "99FOOD"}:
            return {
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "message": f"Canal {store.channel.value} não possui adaptador Playwright homologado.",
                "observed": None,
            }
        if domain.value not in self.config.validated_portal_domains:
            return {
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "message": "Portal sem inspeção autorizada e validação específica; escrita bloqueada.",
                "observed": None,
            }
        if not store.merchant_id:
            return {
                "outcome": SyncOutcome.UNSUPPORTED.value,
                "message": "merchant_id ausente; identidade da loja não pode ser verificada.",
                "observed": None,
            }
        status_action = (
            proposed_payload.get("status_action")
            if isinstance(proposed_payload, dict)
            else None
        )
        required_capability = {
            ChangeDomain.OPENING_HOURS: "opening_hours",
            ChangeDomain.STORE_STATUS: "store_open" if status_action == "OPEN" else "store_pause",
            ChangeDomain.CATALOG: "catalog_price",
            ChangeDomain.PREP_TIME: "prep_time",
        }.get(domain)
        if domain != ChangeDomain.CATALOG and (
            not required_capability or required_capability not in self.config.write_capabilities
        ):
            return {
                "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                "message": (
                    f"Capacidade de escrita '{required_capability or domain.value}' "
                    "não está habilitada em write_capabilities."
                ),
                "observed": None,
            }
        if domain != ChangeDomain.CATALOG:
            try:
                require_approved_write(
                    capability=required_capability or "unsupported",
                    merchant_id=store.merchant_id,
                    payload=proposed_payload,
                )
            except PermissionError as exc:
                return {
                    "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                    "message": str(exc),
                    "observed": None,
                }
        try:
            validated_url = validate_portal_url(store.portal_url, self.config)
            validated_cdp = (
                validate_cdp_url(self.config.playwright_cdp_url)
                if self.config.playwright_cdp_url
                else None
            )
        except ValueError as exc:
            return {
                "outcome": SyncOutcome.ERROR.value,
                "message": str(exc),
                "observed": None,
            }

        try:
            selectors = self._selectors(store, validated_url)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return {"outcome": SyncOutcome.UNSUPPORTED.value, "message": str(exc), "observed": None}

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {
                "outcome": SyncOutcome.ERROR.value,
                "message": "Playwright não está instalado neste ambiente Python.",
                "observed": None,
            }

        write_started = False
        try:
            with sync_playwright() as p:
                browser = None
                context = None
                page = None
                close_context = False
                close_browser = False
                try:
                    if validated_cdp:
                        browser = p.chromium.connect_over_cdp(
                            validated_cdp, timeout=self.timeout_ms
                        )
                        context = (
                            browser.contexts[0] if browser.contexts else browser.new_context()
                        )
                        page = context.new_page()
                    elif self.config.playwright_user_data_dir:
                        context = p.chromium.launch_persistent_context(
                            self._profile_dir(store),
                            headless=self.config.playwright_headless,
                        )
                        close_context = True
                        page = context.new_page()
                    else:
                        browser = p.chromium.launch(headless=self.config.playwright_headless)
                        close_browser = True
                        context = browser.new_context()
                        close_context = True
                        page = context.new_page()

                    resp = page.goto(
                        validated_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    http_status = resp.status if resp is not None else None
                    gate, gate_msg = detect_portal_gate(
                        url=page.url or validated_url,
                        title=page.title() or "",
                        html=page.content() or "",
                        http_status=http_status,
                    )
                    if gate is not None:
                        return {
                            "outcome": gate.value,
                            "message": gate_msg or "Portal bloqueado.",
                            "observed": None,
                        }

                    identity_err = self._verify_page_store_identity(
                        page=page, store=store, selectors=selectors
                    )
                    if identity_err is not None:
                        return {
                            "outcome": SyncOutcome.UNSUPPORTED.value,
                            "message": identity_err,
                            "observed": None,
                        }

                    # Exige estado observado completo antes de iniciar qualquer escrita na página
                    pre_state, pre_observed, pre_reason = self._extract_domain_state_from_page(
                        page=page,
                        domain=domain,
                        desired_payload=proposed_payload,
                        selectors=selectors,
                    )
                    if not pre_observed:
                        return {
                            "outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                            "message": (
                                pre_reason
                                or "Estado atual não pôde ser observado no portal antes da escrita."
                            ),
                            "observed": None,
                        }

                    if domain == ChangeDomain.CATALOG:
                        previous = {str(item["item_id"]): item for item in pre_state}
                        required = set()
                        for item in proposed_payload:
                            old = previous.get(str(item["item_id"]), {})
                            if item.get("price_cents") is not None and item.get("price_cents") != old.get("price_cents"):
                                required.add("catalog_price")
                            if item.get("available") is not None and item.get("available") != old.get("available"):
                                required.add("catalog_availability")
                        if not required.issubset(self.config.write_capabilities):
                            return {"outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                                    "message": "Capacidade de catálogo não habilitada para esta alteração.",
                                    "observed": None}
                        try:
                            for capability in required:
                                require_approved_write(
                                    capability=capability, merchant_id=store.merchant_id,
                                    payload=proposed_payload,
                                )
                        except PermissionError as exc:
                            return {"outcome": SyncOutcome.BLOCKED_GUARDRAIL.value,
                                    "message": str(exc), "observed": None}

                    write_started = True
                    return self._apply_and_readback_on_page(
                        page=page,
                        domain=domain,
                        current_observed=pre_state,
                        proposed_payload=proposed_payload,
                        selectors=selectors,
                    )
                finally:
                    if page is not None:
                        with contextlib.suppress(Exception):
                            page.close()
                    if close_context and context is not None:
                        with contextlib.suppress(Exception):
                            context.close()
                    if close_browser and browser is not None:
                        with contextlib.suppress(Exception):
                            browser.close()
        except (PlaywrightError, TimeoutError, OSError) as exc:
            if write_started:
                return {
                    "outcome": SyncOutcome.UNCERTAIN.value,
                    "message": (
                        f"Erro/timeout no navegador durante escrita ou conferência Playwright "
                        f"({type(exc).__name__}); reconciliação obrigatória."
                    ),
                    "observed": None,
                }
            return {
                "outcome": SyncOutcome.ERROR.value,
                "message": f"Erro no navegador antes da escrita Playwright ({type(exc).__name__}).",
                "observed": None,
            }

    def _apply_and_readback_on_page(
        self,
        *,
        page: Any,
        domain: ChangeDomain,
        current_observed: Any,
        proposed_payload: Any,
        selectors: PortalChannelSelectors,
    ) -> dict[str, Any]:
        if domain == ChangeDomain.PREP_TIME:
            prep_input = page.locator(selectors.prep_time_input)
            save_btn = page.locator(selectors.prep_time_save_button)
            if prep_input.count() == 0 or save_btn.count() == 0:
                return {
                    "outcome": SyncOutcome.UNSUPPORTED.value,
                    "message": "Seletores de tempo de preparo não encontrados no portal aberto.",
                    "observed": None,
                }
            target_minutes = int(proposed_payload["prep_time_minutes"])
            prep_input.first.fill(str(target_minutes))
            save_btn.first.click()
            page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            observed, ok, reason = self._extract_domain_state_from_page(
                page=page,
                domain=domain,
                desired_payload=proposed_payload,
                selectors=selectors,
            )
            if not ok or observed != proposed_payload:
                return {
                    "outcome": SyncOutcome.UNCERTAIN.value,
                    "message": (
                        reason
                        or "Releitura do tempo de preparo divergiu do proposto; item bloqueado em UNCERTAIN até reconciliação."
                    ),
                    "observed": observed,
                }
            return {
                "outcome": SyncOutcome.CONFIRMED.value,
                "message": "Tempo de preparo aplicado e verificado via Playwright.",
                "observed": observed,
            }

        if domain == ChangeDomain.STORE_STATUS:
            select_el = page.locator(selectors.store_status_input)
            reason_el = (
                page.locator(selectors.pause_reason_input)
                if selectors.pause_reason_input
                else None
            )
            save_btn = page.locator(selectors.store_status_save_button)
            if select_el.count() == 0 or save_btn.count() == 0:
                return {
                    "outcome": SyncOutcome.UNSUPPORTED.value,
                    "message": "Seletores de status da loja não encontrados no portal.",
                    "observed": None,
                }
            target_status = str(proposed_payload.get("status_action") or "OPEN")
            select_el.first.select_option(target_status)
            if reason_el is not None and reason_el.count() > 0:
                reason_el.first.fill(str(proposed_payload.get("pause_reason") or ""))
            save_btn.first.click()
            page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            observed, ok, reason = self._extract_domain_state_from_page(
                page=page,
                domain=domain,
                desired_payload=proposed_payload,
                selectors=selectors,
            )
            if not ok or not isinstance(observed, dict) or observed.get("status_action") != target_status:
                return {
                    "outcome": SyncOutcome.UNCERTAIN.value,
                    "message": (
                        reason
                        or "Releitura do status da loja divergiu do proposto; bloqueado em UNCERTAIN até reconciliação."
                    ),
                    "observed": observed,
                }
            return {
                "outcome": SyncOutcome.CONFIRMED.value,
                "message": "Status da loja atualizado e conferido via Playwright.",
                "observed": observed,
            }

        if domain == ChangeDomain.CATALOG:
            if not isinstance(proposed_payload, list):
                return {
                    "outcome": SyncOutcome.ERROR.value,
                    "message": "Payload de cardápio inválido para Playwright.",
                    "observed": None,
                }
            current_by_id: dict[str, dict[str, Any]] = {}
            if isinstance(current_observed, list):
                for row_item in current_observed:
                    if isinstance(row_item, dict) and row_item.get("item_id"):
                        current_by_id[str(row_item["item_id"])] = row_item

            unsupported: list[str] = []
            changed_items: list[dict[str, Any]] = []
            for item in proposed_payload:
                item_id = str(item.get("item_id") or "?")
                curr_item = current_by_id.get(item_id) or {}
                name_changed = (
                    bool(item.get("item_name"))
                    and item.get("item_name") != item_id
                    and item.get("item_name") != curr_item.get("item_name")
                )
                desc_changed = (
                    item.get("description") is not None
                    and item.get("description") != curr_item.get("description")
                )
                orig_changed = (
                    item.get("original_price_cents") is not None
                    and item.get("original_price_cents") != curr_item.get("original_price_cents")
                )
                if name_changed or desc_changed or orig_changed:
                    unsupported.append(item_id)
                price_changed = (
                    item.get("price_cents") is not None
                    and item.get("price_cents") != curr_item.get("price_cents")
                )
                avail_changed = (
                    item.get("available") is not None
                    and item.get("available") != curr_item.get("available")
                )
                if price_changed or avail_changed:
                    changed_items.append(item)

            if unsupported:
                return {
                    "outcome": SyncOutcome.UNSUPPORTED.value,
                    "message": (
                        "Playwright ainda não suporta alteração de preço original, nome ou descrição; itens: "
                        + ", ".join(unsupported)
                    ),
                    "observed": None,
                }

            item_results: list[dict[str, str]] = []
            observed_by_id: dict[str, dict[str, Any]] = {
                k: dict(v) for k, v in current_by_id.items()
            }
            for item in changed_items:
                item_id = str(item["item_id"])
                old = current_by_id.get(item_id, {})
                try:
                    row = page.locator(selectors.catalog_item_row_template.format(item_id=item_id))
                    if row.count() != 1:
                        raise ValueError("item não encontrado no portal")
                    if item.get("price_cents") is not None and item.get("price_cents") != old.get("price_cents"):
                        price_input = row.locator(selectors.catalog_price_input)
                        if price_input.count() != 1:
                            raise ValueError("campo de preço ausente")
                        price_input.first.fill(f"{item['price_cents'] / 100:.2f}")
                    if item.get("available") is not None and item.get("available") != old.get("available"):
                        avail_chk = row.locator(selectors.catalog_available_input)
                        if avail_chk.count() != 1:
                            raise ValueError("campo de disponibilidade ausente")
                        if item["available"]:
                            avail_chk.first.check()
                        else:
                            avail_chk.first.uncheck()
                    save_btn = row.locator(selectors.catalog_item_save_button)
                    if save_btn.count() != 1:
                        raise ValueError("botão de salvar ausente")
                    save_btn.first.click()
                    page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
                    observed, ok, reason = self._extract_domain_state_from_page(
                        page=page, domain=domain, desired_payload=[item], selectors=selectors,
                    )
                    actual = observed[0] if ok and isinstance(observed, list) and len(observed) == 1 else {}
                    if (not ok or
                            (item.get("price_cents") is not None and actual.get("price_cents") != item["price_cents"]) or
                            (item.get("available") is not None and actual.get("available") != item["available"])):
                        raise ValueError(reason or "releitura divergiu do valor enviado")
                    observed_by_id[item_id] = actual
                    item_results.append({"item_id": item_id, "outcome": "CONFIRMED"})
                except Exception as exc:
                    # Outro item ainda pode ser processado; o lote inteiro permanece incerto.
                    item_results.append({"item_id": item_id, "outcome": "UNCERTAIN",
                                         "reason": type(exc).__name__})
            full_observed = [
                observed_by_id[str(it["item_id"])]
                for it in proposed_payload
                if str(it.get("item_id") or "") in observed_by_id
            ]
            if any(result["outcome"] != "CONFIRMED" for result in item_results):
                return {"outcome": SyncOutcome.UNCERTAIN.value,
                        "message": "Alguns itens não foram confirmados; reconciliação obrigatória.",
                        "observed": full_observed, "item_results": item_results}
            return {"outcome": SyncOutcome.CONFIRMED.value,
                    "message": "Cardápio aplicado e verificado item a item via Playwright.",
                    "observed": full_observed, "item_results": item_results}

        if domain == ChangeDomain.OPENING_HOURS:
            hours_input = page.locator(selectors.opening_hours_input)
            save_btn = page.locator(selectors.opening_hours_save_button)
            if hours_input.count() == 0 or save_btn.count() == 0:
                return {
                    "outcome": SyncOutcome.UNSUPPORTED.value,
                    "message": "Seletores de horários não encontrados no portal aberto.",
                    "observed": None,
                }
            hours_input.first.fill(json.dumps(proposed_payload, ensure_ascii=False))
            save_btn.first.click()
            page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            observed, ok, reason = self._extract_domain_state_from_page(
                page=page,
                domain=domain,
                desired_payload=proposed_payload,
                selectors=selectors,
            )
            if not ok or observed != proposed_payload:
                return {
                    "outcome": SyncOutcome.UNCERTAIN.value,
                    "message": (
                        reason
                        or "Releitura dos horários divergiu do proposto; bloqueado em UNCERTAIN até reconciliação."
                    ),
                    "observed": observed,
                }
            return {
                "outcome": SyncOutcome.CONFIRMED.value,
                "message": "Horários aplicados e verificados via Playwright.",
                "observed": observed,
            }

        return {
            "outcome": SyncOutcome.UNSUPPORTED.value,
            "message": f"Domínio {domain} sem suporte no executor Playwright.",
            "observed": None,
        }
