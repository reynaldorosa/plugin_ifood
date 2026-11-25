"""OAuth centralizado do iFood, com token somente em memória."""

import hashlib
import threading
import time

import httpx

from .config import Config

_lock = threading.Lock()
_cached: tuple[str, float, str] | None = None


def access_token(
    config: Config, transport: httpx.BaseTransport | None = None, *, force_refresh: bool = False
) -> str:
    global _cached
    if config.access_token:
        return config.access_token
    if not config.client_id or not config.client_secret:
        raise ValueError("Credenciais OAuth não configuradas")
    with _lock:
        cache_key = config.client_id + ":" + hashlib.sha256(config.client_secret.encode()).hexdigest()
        if not force_refresh and _cached and _cached[2] == cache_key and _cached[1] > time.monotonic() + 60:
            return _cached[0]
        with httpx.Client(transport=transport, follow_redirects=False, timeout=15) as client:
            response = client.post(
                "https://merchant-api.ifood.com.br/authentication/v1.0/oauth/token",
                data={"grantType": "client_credentials", "clientId": config.client_id, "clientSecret": config.client_secret},
            )
            response.raise_for_status()
            data = response.json()
        token = data.get("accessToken")
        lifetime = data.get("expiresIn")
        if not isinstance(token, str) or not token or not isinstance(lifetime, int) or lifetime <= 60:
            raise ValueError("Resposta OAuth inválida")
        _cached = (token, time.monotonic() + lifetime, cache_key)
        return token
