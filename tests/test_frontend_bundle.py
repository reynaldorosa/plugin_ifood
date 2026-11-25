"""Testes do bundle frontend (`dashboard/dist/index.js` + `style.css`) em desktop e mobile."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_JS = ROOT / "dashboard" / "dist" / "index.js"
STYLE_CSS = ROOT / "dashboard" / "dist" / "style.css"


def test_dashboard_bundle_passes_node_syntax_check() -> None:
    proc = subprocess.run(
        ["node", "--check", str(BUNDLE_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"node --check falhou: {proc.stderr}"


def test_dashboard_bundle_renders_desktop_and_mobile_without_errors() -> None:
    playwright_sync = pytest.importorskip("playwright.sync_api")

    js_source = BUNDLE_JS.read_text(encoding="utf-8")
    css_source = STYLE_CSS.read_text(encoding="utf-8")

    mock_overview = {
        "readiness": {
            "enabled": True,
            "sheets_api_configured": True,
            "sheets_spreadsheet_id": "sheet-123",
            "webhook_configured": True,
            "ifood_api_configured": True,
            "playwright_configured": True,
            "default_sync_mode": "REVIEW",
            "default_execution_mode": "API_FIRST",
            "max_price_change_pct": 40.0,
                "validated_portal_domains": [],
                "write_capabilities": [],
                "sheets_poller_runtime": {"state": "DISABLED"},
        },
        "snapshots": [
            {
                "store_key": "loja-centro",
                "domain": "CATALOG",
                "last_applied_hash": "abc",
                "last_state": [{"item_id": "combo-01", "price_cents": 3490}],
                "last_backend": "IFOOD_API",
                "last_outcome": "CONFIRMED",
                "updated_at": "2026-11-19T12:00:00+00:00",
            }
        ],
        "proposals": [
            {
                "proposal_id": "prop-1",
                "store_key": "loja-99",
                "merchant_id": "",
                "channel": "99FOOD",
                "domain": "OPENING_HOURS",
                "actor_id": "google-sheets-webhook",
                "trigger_source": "APPS_SCRIPT_ON_EDIT",
                "execution_mode": "API_FIRST",
                "backend_planned": "PLAYWRIGHT",
                "status": "PENDING",
                "current": [],
                "proposed": [{"dayOfWeek": "MONDAY", "start": "09:00:00", "duration": 540}],
                "diff": {"changed": True},
                "guardrail_warnings": [],
                "created_at": "2026-11-19T12:05:00+00:00",
                    "expires_at": "2026-11-19T13:05:00+00:00",
                    "metadata": {"source_version": "fixture-1"},
                "resolved_at": None,
                "outcome": None,
            }
        ],
        "audit_events": [
            {
                "id": "aud-1",
                "timestamp_utc": "2026-11-19T12:05:00+00:00",
                "actor_id": "google-sheets-webhook",
                "store_key": "loja-99",
                "domain": "OPENING_HOURS",
                "trigger_source": "APPS_SCRIPT_ON_EDIT",
                "backend_used": "PLAYWRIGHT",
                "outcome": "PENDING_REVIEW",
                "summary": "Proposta criada via planilha.",
            }
        ],
    }

    harness_html = f"""
    <!doctype html>
    <html lang="pt-BR">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <style>{css_source}</style>
      </head>
      <body style="background:#111827;margin:0;">
        <div id="root"></div>
        <script>
          (function() {{
            const mockOverview = {json.dumps(mock_overview)};
            const Fragment = Symbol("Fragment");
            let states = [];
            let hookIdx = 0;
            let effectRan = false;
            let rootComp = null;

            function createElement(type, props, ...children) {{
              return {{ type, props: props || {{}}, children: children.flat(Infinity) }};
            }}

            function renderNode(vnode) {{
              if (vnode === null || vnode === undefined || vnode === false || vnode === true) {{
                return document.createTextNode("");
              }}
              if (typeof vnode === "string" || typeof vnode === "number") {{
                return document.createTextNode(String(vnode));
              }}
              if (Array.isArray(vnode)) {{
                const frag = document.createDocumentFragment();
                for (const c of vnode) frag.appendChild(renderNode(c));
                return frag;
              }}
              if (vnode.type === Fragment) {{
                const frag = document.createDocumentFragment();
                for (const c of vnode.children) frag.appendChild(renderNode(c));
                return frag;
              }}
              if (typeof vnode.type === "function") {{
                return renderNode(vnode.type({{ ...(vnode.props || {{}}), children: vnode.children }}));
              }}
              const el = document.createElement(vnode.type);
              for (const [k, val] of Object.entries(vnode.props || {{}})) {{
                if (k === "children" || k === "key" || val === undefined || val === null) continue;
                if (k === "className") el.className = val;
                else if (k === "value") el.value = val;
                else if (k === "disabled") el.disabled = Boolean(val);
                else if (k.startsWith("on") && typeof val === "function") {{
                  el.addEventListener(k.slice(2).toLowerCase(), val);
                }} else {{
                  el.setAttribute(k, String(val));
                }}
              }}
              for (const c of vnode.children) el.appendChild(renderNode(c));
              return el;
            }}

            function rerender() {{
              hookIdx = 0;
              const rootEl = document.getElementById("root");
              rootEl.innerHTML = "";
              rootEl.appendChild(renderNode(createElement(rootComp, {{}})));
            }}

            window.__HERMES_PLUGIN_SDK__ = {{
              React: {{ createElement, Fragment }},
              hooks: {{
                useState(init) {{
                  const idx = hookIdx++;
                  if (!(idx in states)) states[idx] = typeof init === "function" ? init() : init;
                  const setter = (val) => {{
                    states[idx] = typeof val === "function" ? val(states[idx]) : val;
                    queueMicrotask(rerender);
                  }};
                  return [states[idx], setter];
                }},
                useEffect(fn) {{
                  if (!effectRan) {{
                    effectRan = true;
                    queueMicrotask(() => fn());
                  }}
                }},
                useCallback(fn) {{ return fn; }},
              }},
              async fetchJSON(url, _init, options) {{
                window.__LAST_FETCH_OPTIONS__ = options;
                if (!options || options.allowUnauthorized !== true) {{
                  throw new Error("allowUnauthorized missing");
                }}
                if (url.includes("/sync/overview")) return mockOverview;
                if (url.includes("/tenants")) return [{{tenant_key: "default", sheet_configured: true}}];
                if (url.includes("/sheets/apps-script")) return {{ filename: "Code.gs", content: "function onOpen() {{}}" }};
                if (url.includes("/confirm")) return {{ proposal: mockOverview.proposals[0], execution: {{ outcome: "CONFIRMED", message: "Aplicado" }} }};
                return {{ summary: "OK" }};
              }},
            }};

            window.__HERMES_PLUGINS__ = {{
              register(_name, comp) {{
                rootComp = comp;
              }},
            }};

            {js_source}
            rerender();
          }})();
        </script>
      </body>
    </html>
    """

    with playwright_sync.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for width, height in ((1440, 900), (1280, 800), (375, 812), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                errors: list[str] = []
                page.on("pageerror", lambda err, errs=errors: errs.append(str(err)))
                page.on(
                    "console",
                    lambda msg, errs=errors: errs.append(msg.text)
                    if msg.type == "error"
                    else None,
                )
                page.set_content(harness_html, wait_until="load")
                page.wait_for_timeout(300)
                body_text = page.locator("body").inner_text()
                assert "Loja iFood / Sheets" in body_text, (body_text, errors)
                assert "A planilha é o comando" in body_text
                assert "prontidão das integrações" in body_text.lower()
                assert "Anota AI API (export)" in body_text
                assert "política de escrita" in body_text.lower()
                assert "sessões dos portais" in body_text.lower()
                assert "último lote" in body_text.lower()
                assert "Confirmar e Aplicar" not in body_text
                assert page.evaluate("window.__LAST_FETCH_OPTIONS__?.allowUnauthorized === true")

                overflow_overview = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
                )
                assert not overflow_overview, f"Overflow horizontal na Visão geral em {width}x{height}"

                # Navega para a aba Fila de mudanças e verifica o modelo novo (sem Confirmar/Aplicar)
                page.locator("button", has_text="Fila de mudanças (1)").click()
                page.wait_for_timeout(150)
                changes_text = page.locator("body").inner_text()
                assert "Horários · loja-99" in changes_text, (changes_text, errors)
                assert "seg 09:00:00+540min" in changes_text
                assert "Descartar" in changes_text
                assert "Confirmar e Aplicar" not in changes_text

                # Navega para a aba Auditoria
                page.locator("button", has_text="Auditoria").click()
                page.wait_for_timeout(150)
                audit_text = page.locator("body").inner_text()
                assert "Proposta criada via planilha." in audit_text, (audit_text, errors)

                # Navega para a aba Planilha e verifica o botão de sincronização
                page.locator("button", has_text="Planilha").click()
                page.wait_for_timeout(150)
                sheet_text = page.locator("body").inner_text()
                assert "Sincronizar agora" in sheet_text, (sheet_text, errors)
                assert "Carregar exportação" in sheet_text

                overflow = page.evaluate(
                    "document.documentElement.scrollWidth > document.documentElement.clientWidth + 2"
                )
                assert not overflow, f"Overflow horizontal detectado em {width}x{height}"
                assert errors == [], f"Erros de console em {width}x{height}: {errors}"
                page.close()
        finally:
            browser.close()
