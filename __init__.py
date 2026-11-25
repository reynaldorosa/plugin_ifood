"""Ponto de entrada do plugin `ifood-loja` para registro de tools no Hermes Agent."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from ifood.config import Config
from ifood.hermes_tools import TOOL_SPECS
from ifood.poller import poller_enabled, run_sheets_poller, set_poller_runtime_state

_LOGGER = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Registra ferramentas de sincronização Google Sheets -> iFood/99Food no runtime do Hermes."""
    for spec in TOOL_SPECS:
        ctx.register_tool(
            name=spec["name"],
            toolset="ifood_loja",
            schema=spec["schema"],
            handler=spec["handler"],
        )

    config = Config.from_env()
    if poller_enabled(config):
        task_coro = run_sheets_poller(config)
        try:
            spawn = getattr(ctx, "spawn_task", None)
            if not callable(spawn):
                task_coro.close()
                set_poller_runtime_state("FAILED", "Runtime Hermes sem spawn_task")
                return
            set_poller_runtime_state("STARTING")
            spawn(task_coro, name="ifood-google-sheets-poller")
        except (AttributeError, RuntimeError):
            task_coro.close()
            set_poller_runtime_state("FAILED", "Falha ao registrar tarefa")
            _LOGGER.exception("Não foi possível iniciar o poller de Google Sheets no Hermes.")
    else:
        set_poller_runtime_state("DISABLED")
