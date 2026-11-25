"""Cliente oficial Google Sheets API v4 (leitura e write-back de status/auditoria).

Implementação leve usando `httpx` + `cryptography` (RS256 Service Account JWT),
sem depender de pacotes externos de SDK Google.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ifood.config import Config

_SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

_token_lock = threading.Lock()
_cached_google_token: tuple[str, float, str] | None = None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_service_account_info(config: Config) -> dict[str, Any] | None:
    if config.google_service_account_json:
        info = json.loads(config.google_service_account_json)
        if not isinstance(info, dict):
            raise ValueError("IFOOD_LOJA_GOOGLE_SERVICE_ACCOUNT_JSON inválido.")
        return info
    if config.google_service_account_file:
        path = Path(config.google_service_account_file)
        info = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            raise ValueError("Arquivo de Service Account Google inválido.")
        return info
    return None


def is_sheets_api_configured(config: Config) -> bool:
    if not config.sheets_spreadsheet_id:
        return False
    return bool(
        config.google_access_token
        or config.google_service_account_json
        or config.google_service_account_file
    )


def mint_service_account_token(
    sa_info: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
    now_epoch: int | None = None,
) -> str:
    """Assina assertion JWT RS256 com cryptography e obtém access_token da Google OAuth."""
    global _cached_google_token
    client_email = str(sa_info.get("client_email") or "").strip()
    private_key_pem = str(sa_info.get("private_key") or "").strip()
    token_uri = str(sa_info.get("token_uri") or _GOOGLE_TOKEN_URL).strip()
    parsed_token_uri = urlparse(token_uri)
    if (
        parsed_token_uri.scheme != "https"
        or parsed_token_uri.hostname not in {"oauth2.googleapis.com", "accounts.google.com"}
        or parsed_token_uri.username
        or parsed_token_uri.password
        or parsed_token_uri.port not in (None, 443)
        or parsed_token_uri.path != "/token"
        or parsed_token_uri.query
        or parsed_token_uri.fragment
    ):
        raise ValueError("token_uri deve ser o endpoint HTTPS oficial do Google OAuth.")
    if not client_email or not private_key_pem:
        raise ValueError("Service Account JSON sem client_email ou private_key.")

    if now_epoch is None:
        with _token_lock:
            if (
                _cached_google_token
                and _cached_google_token[2] == client_email
                and _cached_google_token[1] > time.monotonic() + 60
            ):
                return _cached_google_token[0]

    issued_at = int(time.time()) if now_epoch is None else now_epoch
    header = _b64url(
        json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8")
    )
    claims = _b64url(
        json.dumps(
            {
                "iss": client_email,
                "scope": _SHEETS_SCOPE,
                "aud": token_uri,
                "iat": issued_at,
                "exp": issued_at + 3600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signing_input = f"{header}.{claims}".encode("ascii")

    loaded_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"),
        password=None,
    )
    if not isinstance(loaded_key, rsa.RSAPrivateKey):
        raise ValueError("Chave privada da Service Account deve ser RSA.")
    signature = loaded_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{header}.{claims}.{_b64url(signature)}"

    with httpx.Client(transport=transport, follow_redirects=False, timeout=15) as client:
        resp = client.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        payload = resp.json()

    token = payload.get("access_token")
    expires_in = int(payload.get("expires_in") or 3600)
    if not isinstance(token, str) or not token:
        raise ValueError("Resposta OAuth Google sem access_token válido.")

    if now_epoch is None:
        with _token_lock:
            _cached_google_token = (token, time.monotonic() + expires_in, client_email)
    return token


def google_access_token(
    config: Config, transport: httpx.BaseTransport | None = None
) -> str:
    if config.google_access_token:
        return config.google_access_token
    sa_info = load_service_account_info(config)
    if sa_info is None:
        raise ValueError("Credenciais Google Sheets API não configuradas.")
    return mint_service_account_token(sa_info, transport=transport)


class GoogleSheetsClient:
    """Leitura batch e gravação de retorno de sincronização na planilha Google Sheets."""

    def __init__(
        self,
        config: Config,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport

    def fetch_snapshot_tables(self) -> dict[str, list[list[Any]]]:
        spreadsheet_id = self.config.sheets_spreadsheet_id
        if not spreadsheet_id:
            raise ValueError("IFOOD_LOJA_SHEETS_ID não configurado.")
        token = google_access_token(self.config, transport=self.transport)
        ranges = ["Lojas!A1:P500", "Horarios!A1:F2000", "Cardapio!A1:K5000"]
        with httpx.Client(
            base_url=_SHEETS_API_BASE,
            transport=self.transport,
            follow_redirects=False,
            timeout=20,
        ) as client:
            resp = client.get(
                f"/{spreadsheet_id}/values:batchGet",
                params={"ranges": ranges},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        value_ranges = data.get("valueRanges") or []
        tables: dict[str, list[list[Any]]] = {"Lojas": [], "Horarios": [], "Cardapio": []}
        for vr in value_ranges:
            rng = str(vr.get("range") or "")
            tab = rng.split("!", 1)[0].strip("'")
            if tab in tables:
                tables[tab] = list(vr.get("values") or [])
        return tables

    def write_sync_feedback(
        self,
        *,
        status_updates: list[dict[str, Any]] | None = None,
        audit_rows: list[list[Any]] | None = None,
    ) -> None:
        """Grava resumo de sincronização em Auditoria_Sync e, quando informado, batchUpdate."""
        spreadsheet_id = self.config.sheets_spreadsheet_id
        if not spreadsheet_id:
            return
        if not status_updates and not audit_rows:
            return
        token = google_access_token(self.config, transport=self.transport)
        with httpx.Client(
            base_url=_SHEETS_API_BASE,
            transport=self.transport,
            follow_redirects=False,
            timeout=20,
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            if status_updates:
                client.post(
                    f"/{spreadsheet_id}/values:batchUpdate",
                    json={"valueInputOption": "USER_ENTERED", "data": status_updates},
                    headers=headers,
                ).raise_for_status()
            if audit_rows:
                client.post(
                    f"/{spreadsheet_id}/values/Auditoria_Sync!A:H:append",
                    params={"valueInputOption": "USER_ENTERED"},
                    json={"values": audit_rows},
                    headers=headers,
                ).raise_for_status()
