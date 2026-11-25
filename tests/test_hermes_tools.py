"""Testes das ferramentas Hermes Agent (`__init__.py` e `ifood/hermes_tools.py`)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from ifood.hermes_tools import (
    tool_list_proposals,
    tool_sync_from_sheet,
    tool_sync_status,
)


class _FakeHermesContext:
    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(
        self, *, name: str, toolset: str, schema: dict[str, Any], handler: Any
    ) -> None:
        self.tools.append(
            {"name": name, "toolset": toolset, "schema": schema, "handler": handler}
        )


def test_plugin_root_register_exposes_sync_tools() -> None:
    plugin_init = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("ifood_loja_root", plugin_init)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ctx = _FakeHermesContext()
    mod.register(ctx)
    assert {t["name"] for t in ctx.tools} == {
        "ifood_sync_status",
        "ifood_sync_from_sheet",
        "ifood_list_proposals",
        "ifood_confirm_proposal",
        "ifood_reject_proposal",
    }
    assert all(t["toolset"] == "ifood_loja" for t in ctx.tools)


def test_hermes_tools_enforce_default_deny_and_create_review_proposals(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IFOOD_LOJA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IFOOD_LOJA_ENABLED", "false")
    monkeypatch.delenv("IFOOD_LOJA_AGENT_OPERATOR_ID", raising=False)
    monkeypatch.delenv("IFOOD_LOJA_OPERATORS", raising=False)

    res_401 = json.loads(tool_sync_status())
    assert res_401["ok"] is False
    assert res_401["status_code"] == 401

    monkeypatch.setenv("IFOOD_LOJA_AGENT_OPERATOR_ID", "hermes-op")
    res_403 = json.loads(tool_sync_status())
    assert res_403["ok"] is False
    assert res_403["status_code"] == 403

    monkeypatch.setenv("IFOOD_LOJA_ENABLED", "true")
    monkeypatch.setenv("IFOOD_LOJA_OPERATORS", "hermes-op")

    sync_res = json.loads(
        tool_sync_from_sheet(
            stores_rows=[
                {
                    "store_key": "loja-99",
                    "channel": "99FOOD",
                    "execution_mode": "API_FIRST",
                    "sync_mode": "REVIEW",
                    "portal_url": "https://merchant.99app.com",
                    "prep_time_minutes": 30,
                }
            ]
        )
    )
    assert sync_res["ok"] is True
    assert sync_res["data"]["results"][0]["outcome"] == "UNSUPPORTED"

    listed = json.loads(tool_list_proposals(status="PENDING"))
    assert listed["ok"] is True
    assert listed["data"] == []
