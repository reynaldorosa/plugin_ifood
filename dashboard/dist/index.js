(function() {
'use strict';
const modules = {
"./index": function(module, exports, require) {
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const IfoodStorePage_1 = require("./IfoodStorePage");
window.__HERMES_PLUGINS__?.register?.('ifood-loja', IfoodStorePage_1.IfoodStorePage);

},
"./IfoodStorePage": function(module, exports, require) {
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.IfoodStorePage = IfoodStorePage;
const api_1 = require("./api");
const hermesReact_1 = require("./hermesReact");
const EXAMPLE_SNAPSHOT = JSON.stringify({
    stores_rows: [
        {
            store_key: 'loja-centro',
            channel: 'IFOOD',
            merchant_id: '550e8400-e29b-41d4-a716-446655440000',
            store_name: 'Loja Centro (iFood)',
            execution_mode: 'API_FIRST',
            sync_mode: 'REVIEW',
            enabled: true,
            prep_time_minutes: 25,
            status_action: 'OPEN',
            portal_url: 'https://portal.ifood.com.br',
        },
    ],
    hours_rows: [
        { store_key: 'loja-centro', day_of_week: 'SEGUNDA', start_time: '09:00', end_time: '18:00' },
        { store_key: 'loja-centro', day_of_week: 'TERCA', start_time: '09:00', end_time: '18:00' },
        { store_key: 'loja-centro', day_of_week: 'QUARTA', start_time: '09:00', end_time: '18:00' },
        { store_key: 'loja-centro', day_of_week: 'QUINTA', start_time: '09:00', end_time: '18:00' },
        { store_key: 'loja-centro', day_of_week: 'SEXTA', start_time: '09:00', end_time: '18:00' },
        { store_key: 'loja-centro', day_of_week: 'SABADO', start_time: '09:00', end_time: '18:00' },
        { store_key: 'loja-centro', day_of_week: 'DOMINGO', start_time: '09:00', end_time: '18:00' },
    ],
    catalog_rows: [
        {
            store_key: 'loja-centro',
            item_id: 'combo-01',
            item_name: 'Combo Clássico',
            price_brl: '34,90',
            available: true,
        },
    ],
    dry_run: false,
}, null, 2);
function badgeClass(value) {
    const upper = value.toUpperCase();
    if (upper === 'CONFIRMED' || upper === 'APPLIED' || upper === 'OK')
        return 'ifood-badge is-ok';
    if (upper.includes('BLOCKED') || upper.includes('CAPTCHA') || upper === 'ERROR' || upper === 'DIVERGENT') {
        return 'ifood-badge is-danger';
    }
    if (upper.includes('PENDING') || upper.includes('LOGIN') || upper === 'REVIEW') {
        return 'ifood-badge is-warn';
    }
    return 'ifood-badge is-neutral';
}
function IfoodStorePage() {
    const [tab, setTab] = (0, hermesReact_1.useState)('queue');
    const [overview, setOverview] = (0, hermesReact_1.useState)(null);
    const [tenants, setTenants] = (0, hermesReact_1.useState)([]);
    const [tenantKey, setTenantKey] = (0, hermesReact_1.useState)('');
    const [appsScript, setAppsScript] = (0, hermesReact_1.useState)('');
    const [loading, setLoading] = (0, hermesReact_1.useState)(true);
    const [busy, setBusy] = (0, hermesReact_1.useState)(false);
    const [error, setError] = (0, hermesReact_1.useState)('');
    const [notice, setNotice] = (0, hermesReact_1.useState)('');
    const [rowErrors, setRowErrors] = (0, hermesReact_1.useState)([]);
    const [showInlineSync, setShowInlineSync] = (0, hermesReact_1.useState)(false);
    const [inlineJson, setInlineJson] = (0, hermesReact_1.useState)(EXAMPLE_SNAPSHOT);
    const loadAll = (0, hermesReact_1.useCallback)(async (targetTenant = tenantKey) => {
        if (!targetTenant)
            return;
        setLoading(true);
        setError('');
        try {
            const data = await (0, api_1.fetchSyncOverview)(targetTenant);
            setOverview(data);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha ao carregar sincronização.');
        }
        finally {
            setLoading(false);
        }
    }, [tenantKey]);
    (0, hermesReact_1.useEffect)(() => {
        void (0, api_1.fetchTenants)().then(async (items) => {
            setTenants(items);
            const first = items[0]?.tenant_key || '';
            setTenantKey((current) => current || first);
            if (first) {
                await loadAll(first);
            }
            else {
                setLoading(false);
            }
        }).catch((e) => {
            setError(e instanceof Error ? e.message : 'Falha ao listar padarias.');
            setLoading(false);
        });
    }, []);
    const handleLoadAppsScript = async () => {
        if (appsScript)
            return;
        try {
            const res = await (0, api_1.fetchAppsScript)();
            setAppsScript(res.content || '');
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha ao obter Apps Script.');
        }
    };
    const handleSyncPull = async () => {
        setBusy(true);
        setError('');
        setNotice('');
        setRowErrors([]);
        try {
            const res = await (0, api_1.triggerSheetSync)({ tenant_key: tenantKey });
            setNotice(res.summary);
            setRowErrors(res.row_errors || []);
            await loadAll();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha ao sincronizar com Google Sheets.');
        }
        finally {
            setBusy(false);
        }
    };
    const handleInlineSync = async () => {
        setBusy(true);
        setError('');
        setNotice('');
        setRowErrors([]);
        try {
            const parsed = JSON.parse(inlineJson);
            const res = await (0, api_1.triggerSheetSync)({ ...parsed, tenant_key: tenantKey });
            setNotice(res.summary);
            setRowErrors(res.row_errors || []);
            await loadAll();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Snapshot JSON inválido.');
        }
        finally {
            setBusy(false);
        }
    };
    const handleConfirmProposal = async (proposalId, overrideGuardrail = false) => {
        setBusy(true);
        setError('');
        setNotice('');
        try {
            const res = await (0, api_1.confirmSyncProposal)(proposalId, tenantKey, overrideGuardrail);
            const outcome = String(res.execution.outcome || '');
            setNotice(`Proposta ${proposalId.slice(0, 8)} executada: ${outcome} — ${String(res.execution.message || '')}`);
            await loadAll();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha ao confirmar proposta.');
        }
        finally {
            setBusy(false);
        }
    };
    const handleRejectProposal = async (proposalId) => {
        setBusy(true);
        setError('');
        setNotice('');
        try {
            await (0, api_1.rejectSyncProposal)(proposalId, 'Rejeitado manualmente no Dashboard Hermes', tenantKey);
            setNotice(`Proposta ${proposalId.slice(0, 8)} rejeitada.`);
            await loadAll();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha ao rejeitar proposta.');
        }
        finally {
            setBusy(false);
        }
    };
    const handleReconcileProposal = async (proposalId) => {
        setBusy(true);
        setError('');
        setNotice('');
        try {
            const result = await (0, api_1.reconcileSyncProposal)(proposalId, tenantKey);
            setNotice(`Reconciliação somente leitura: ${result.outcome}.`);
            await loadAll();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha na reconciliação.');
        }
        finally {
            setBusy(false);
        }
    };
    const proposals = overview?.proposals || [];
    const activeProposals = proposals.filter((p) => p.status === 'PENDING' || p.status === 'BLOCKED');
    const snapshots = overview?.snapshots || [];
    const auditEvents = overview?.audit_events || [];
    const readiness = overview?.readiness;
    return (hermesReact_1.React.createElement("main", { className: "ifood-sync-root" },
        hermesReact_1.React.createElement("header", { className: "ifood-header" },
            hermesReact_1.React.createElement("div", null,
                hermesReact_1.React.createElement("span", { className: "ifood-eyebrow" }, "HERMES / OPERA\u00C7\u00C3O DE LOJAS"),
                hermesReact_1.React.createElement("h1", null, "Central de sincroniza\u00E7\u00E3o"),
                hermesReact_1.React.createElement("p", { className: "ifood-subtitle" }, "Propostas auditadas para revis\u00E3o. Escritas permanecem bloqueadas at\u00E9 valida\u00E7\u00E3o de cada capacidade, merchant e executor.")),
            hermesReact_1.React.createElement("div", { className: "ifood-header-actions" },
                hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-primary", disabled: busy || loading, onClick: () => void handleSyncPull() }, busy ? 'Sincronizando…' : 'Sincronizar Planilha Agora'),
                hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", disabled: busy, onClick: () => setShowInlineSync((prev) => !prev) }, showInlineSync ? 'Ocultar Snapshot JSON' : 'Simular / Enviar Snapshot'))),
        hermesReact_1.React.createElement("section", { className: "ifood-card", "aria-label": "Padaria selecionada" },
            hermesReact_1.React.createElement("label", { htmlFor: "ifood-tenant-select" }, "Padaria"),
            hermesReact_1.React.createElement("select", { id: "ifood-tenant-select", "aria-label": "Selecionar padaria", value: tenantKey, onChange: (e) => {
                    const next = e.target.value;
                    setOverview(null);
                    setTenantKey(next);
                    void loadAll(next);
                } }, tenants.map((tenant) => hermesReact_1.React.createElement("option", { key: tenant.tenant_key, value: tenant.tenant_key }, tenant.tenant_key)))),
        hermesReact_1.React.createElement("section", { className: "ifood-summary-grid", "aria-label": "Resumo da opera\u00E7\u00E3o" },
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Para revisar"),
                hermesReact_1.React.createElement("strong", null, activeProposals.length),
                hermesReact_1.React.createElement("small", null, "propostas aguardando decis\u00E3o")),
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Estados registrados"),
                hermesReact_1.React.createElement("strong", null, snapshots.length),
                hermesReact_1.React.createElement("small", null, "dom\u00EDnios com hist\u00F3rico")),
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Resultado incerto"),
                hermesReact_1.React.createElement("strong", null, proposals.filter((p) => p.status === 'UNCERTAIN').length),
                hermesReact_1.React.createElement("small", null, "exigem reconcilia\u00E7\u00E3o"))),
        readiness && (hermesReact_1.React.createElement("section", { className: "ifood-integrations", "aria-label": "Status das integra\u00E7\u00F5es" },
            hermesReact_1.React.createElement("div", { className: "ifood-section-heading" },
                hermesReact_1.React.createElement("span", null, "Integra\u00E7\u00F5es"),
                hermesReact_1.React.createElement("small", null, "Configura\u00E7\u00E3o e capacidade de execu\u00E7\u00E3o")),
            hermesReact_1.React.createElement("div", { className: "ifood-readiness-bar" },
                hermesReact_1.React.createElement("div", { className: readiness.sheets_api_configured ? 'ifood-pill is-ready' : 'ifood-pill' },
                    "Google Sheets API: ",
                    readiness.sheets_api_configured ? 'Configurada' : 'Pendente'),
                hermesReact_1.React.createElement("div", { className: readiness.webhook_configured ? 'ifood-pill is-ready' : 'ifood-pill' },
                    "Webhook HMAC: ",
                    readiness.webhook_configured ? 'Ativo' : 'Pendente'),
                hermesReact_1.React.createElement("div", { className: readiness.sheets_poller_enabled ? 'ifood-pill is-ready' : 'ifood-pill' },
                    "Poller Google Sheets: ",
                    readiness.sheets_poller_enabled ? 'Ativo' : 'Desativado'),
                hermesReact_1.React.createElement("div", { className: readiness.ifood_api_configured ? 'ifood-pill is-ready' : 'ifood-pill' },
                    "iFood API: ",
                    readiness.ifood_api_configured ? 'Credencial configurada; escritas exigem contrato validado' : 'Sem credencial'),
                hermesReact_1.React.createElement("div", { className: readiness.playwright_configured ? 'ifood-pill is-ready' : 'ifood-pill' },
                    "Playwright: ",
                    readiness.playwright_configured ? `Configurado; domínios validados: ${readiness.validated_portal_domains.join(', ') || 'nenhum'}` : 'Não configurado'),
                hermesReact_1.React.createElement("div", { className: "ifood-pill is-neutral" },
                    "Pol\u00EDtica: REVIEW \u00B7 Capacidades de escrita: ",
                    readiness.write_capabilities.join(', ') || 'todas bloqueadas'),
                hermesReact_1.React.createElement("div", { className: "ifood-pill is-neutral" },
                    "Poller runtime: ",
                    readiness.sheets_poller_runtime.state,
                    readiness.sheets_poller_runtime.detail ? ` · ${readiness.sheets_poller_runtime.detail}` : '')))),
        overview?.portal_sessions?.[tenantKey] && (hermesReact_1.React.createElement("section", { className: "ifood-card", "aria-label": "Sess\u00F5es dos portais" },
            hermesReact_1.React.createElement("h2", null, "Sess\u00F5es dos portais"),
            hermesReact_1.React.createElement("div", { className: "ifood-readiness-bar" }, Object.entries(overview.portal_sessions[tenantKey]).map(([channel, session]) => (hermesReact_1.React.createElement("div", { key: channel, className: badgeClass(session.state) },
                channel,
                ": ",
                session.state,
                session.last_error ? ` · ${session.last_error}` : '')))))),
        loading && hermesReact_1.React.createElement("p", { className: "ifood-banner is-info" }, "Carregando painel de sincroniza\u00E7\u00E3o\u2026"),
        error && (hermesReact_1.React.createElement("p", { role: "alert", className: "ifood-banner is-error" }, error)),
        notice && (hermesReact_1.React.createElement("p", { role: "status", className: "ifood-banner is-success" }, notice)),
        rowErrors.length > 0 && (hermesReact_1.React.createElement("section", { className: "ifood-card", "aria-label": "Erros de valida\u00E7\u00E3o da planilha" },
            hermesReact_1.React.createElement("h2", null, "Sincroniza\u00E7\u00E3o bloqueada por erros de linha"),
            hermesReact_1.React.createElement("ul", null, rowErrors.map((item, index) => (hermesReact_1.React.createElement("li", { key: index },
                String(item.sheet || 'Aba desconhecida'),
                " \u00B7 linha ",
                String(item.row || '?'),
                ": ",
                String(item.message || 'Erro de validação'))))))),
        showInlineSync && (hermesReact_1.React.createElement("section", { className: "ifood-card" },
            hermesReact_1.React.createElement("h2", null, "Testar / Enviar Snapshot de Planilha (JSON)"),
            hermesReact_1.React.createElement("p", { className: "ifood-muted" }, "Snapshot sint\u00E9tico para validar parser, regras de diff e bloqueios. Nenhum portal real \u00E9 acessado por este modo."),
            hermesReact_1.React.createElement("textarea", { "aria-label": "Snapshot JSON", rows: 10, className: "ifood-textarea", value: inlineJson, onChange: (e) => setInlineJson(e.target.value) }),
            hermesReact_1.React.createElement("div", { className: "ifood-row-actions" },
                hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-primary", disabled: busy, onClick: () => void handleInlineSync() }, "Processar Snapshot")))),
        hermesReact_1.React.createElement("nav", { className: "ifood-tabs", "aria-label": "Navega\u00E7\u00E3o do plugin" },
            hermesReact_1.React.createElement("button", { type: "button", className: tab === 'queue' ? 'ifood-tab is-active' : 'ifood-tab', onClick: () => setTab('queue') },
                "Fila de Mudan\u00E7as (",
                activeProposals.length,
                ")"),
            hermesReact_1.React.createElement("button", { type: "button", className: tab === 'stores' ? 'ifood-tab is-active' : 'ifood-tab', onClick: () => setTab('stores') },
                "Lojas Sincronizadas (",
                snapshots.length,
                ")"),
            hermesReact_1.React.createElement("button", { type: "button", className: tab === 'setup' ? 'ifood-tab is-active' : 'ifood-tab', onClick: () => {
                    setTab('setup');
                    void handleLoadAppsScript();
                } }, "Google Sheets & Auditoria")),
        hermesReact_1.React.createElement("div", { className: "ifood-content-heading" },
            hermesReact_1.React.createElement("div", null,
                hermesReact_1.React.createElement("span", { className: "ifood-eyebrow" }, "VIS\u00C3O ATUAL"),
                hermesReact_1.React.createElement("h2", null, tab === 'queue' ? 'Fila de mudanças' : tab === 'stores' ? 'Estados sincronizados' : 'Planilha e auditoria')),
            hermesReact_1.React.createElement("span", { className: "ifood-muted" }, tab === 'queue' ? `${proposals.length} registros` : tab === 'stores' ? `${snapshots.length} registros` : `${auditEvents.length} eventos recentes`)),
        tab === 'queue' && (hermesReact_1.React.createElement("section", { className: "ifood-stack" }, proposals.length === 0 ? (hermesReact_1.React.createElement("div", { className: "ifood-card" },
            hermesReact_1.React.createElement("p", { className: "ifood-muted" },
                "Nenhuma mudan\u00E7a pendente da planilha. Edite sua planilha Google Sheets ou clique em",
                hermesReact_1.React.createElement("strong", null, " Sincronizar Planilha Agora"),
                "."))) : (proposals.map((prop) => {
            const isActionable = prop.status === 'PENDING' || prop.status === 'BLOCKED';
            const isUncertain = prop.status === 'UNCERTAIN';
            const hasWarnings = prop.guardrail_warnings.length > 0;
            return (hermesReact_1.React.createElement("article", { key: prop.proposal_id, className: "ifood-card" },
                hermesReact_1.React.createElement("div", { className: "ifood-card-top" },
                    hermesReact_1.React.createElement("div", null,
                        hermesReact_1.React.createElement("h3", null,
                            prop.store_key,
                            " \u00B7 ",
                            prop.domain),
                        hermesReact_1.React.createElement("span", { className: "ifood-meta" },
                            "Canal: ",
                            prop.channel,
                            " \u00B7 Merchant: ",
                            prop.merchant_id || 'não identificado',
                            " \u00B7 Origem: ",
                            prop.trigger_source,
                            " \u00B7 Executor:",
                            ' ',
                            hermesReact_1.React.createElement("strong", null, prop.backend_planned),
                            hermesReact_1.React.createElement("br", null),
                            "Vers\u00E3o: ",
                            String(prop.metadata.source_version || '—'),
                            " \u00B7 Expira: ",
                            new Date(prop.expires_at).toLocaleString())),
                    hermesReact_1.React.createElement("span", { className: badgeClass(prop.status) }, prop.status)),
                hasWarnings && (hermesReact_1.React.createElement("div", { className: "ifood-guardrail-box" },
                    hermesReact_1.React.createElement("strong", null, "Alertas de Guardrail:"),
                    hermesReact_1.React.createElement("ul", null, prop.guardrail_warnings.map((w, idx) => (hermesReact_1.React.createElement("li", { key: idx }, w)))))),
                hermesReact_1.React.createElement("div", { className: "ifood-diff-grid" },
                    hermesReact_1.React.createElement("div", null,
                        hermesReact_1.React.createElement("h4", null, "Estado Atual / Anterior"),
                        hermesReact_1.React.createElement("pre", { className: "ifood-pre" }, JSON.stringify(prop.current, null, 2))),
                    hermesReact_1.React.createElement("div", null,
                        hermesReact_1.React.createElement("h4", null, "Proposta da Planilha (Source of Truth)"),
                        hermesReact_1.React.createElement("pre", { className: "ifood-pre" }, JSON.stringify(prop.proposed, null, 2)))),
                isActionable && (hermesReact_1.React.createElement("div", { className: "ifood-row-actions" },
                    !hasWarnings ? (hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-primary", disabled: busy, onClick: () => void handleConfirmProposal(prop.proposal_id, false) }, "Confirmar e Aplicar")) : (hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-danger", disabled: busy, onClick: () => void handleConfirmProposal(prop.proposal_id, true) }, "Confirmar mesmo com Guardrail")),
                    hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", disabled: busy, onClick: () => void handleRejectProposal(prop.proposal_id) }, "Rejeitar"))),
                isUncertain && (hermesReact_1.React.createElement("div", { className: "ifood-guardrail-box" },
                    hermesReact_1.React.createElement("strong", null, "Resultado incerto."),
                    " A escrita n\u00E3o ser\u00E1 repetida automaticamente. Reconcilie o estado atual da loja antes de criar outra proposta.",
                    hermesReact_1.React.createElement("div", { className: "ifood-row-actions" },
                        hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", disabled: busy, onClick: () => void handleReconcileProposal(prop.proposal_id) }, "Consultar e reconciliar"))))));
        })))),
        tab === 'stores' && (hermesReact_1.React.createElement("section", { className: "ifood-stack" }, snapshots.length === 0 ? (hermesReact_1.React.createElement("div", { className: "ifood-card" },
            hermesReact_1.React.createElement("p", { className: "ifood-muted" }, "Nenhum snapshot aplicado registrado ainda."))) : (snapshots.map((snap) => (hermesReact_1.React.createElement("article", { key: `${snap.store_key}-${snap.domain}`, className: "ifood-card" },
            hermesReact_1.React.createElement("div", { className: "ifood-card-top" },
                hermesReact_1.React.createElement("div", null,
                    hermesReact_1.React.createElement("h3", null,
                        snap.store_key,
                        " \u00B7 ",
                        snap.domain),
                    hermesReact_1.React.createElement("span", { className: "ifood-meta" },
                        "Executor: ",
                        snap.last_backend,
                        " \u00B7 Atualizado: ",
                        snap.updated_at)),
                hermesReact_1.React.createElement("span", { className: badgeClass(snap.last_outcome) }, snap.last_outcome)),
            hermesReact_1.React.createElement("pre", { className: "ifood-pre" }, JSON.stringify(snap.last_state, null, 2)))))))),
        tab === 'setup' && (hermesReact_1.React.createElement("section", { className: "ifood-stack" },
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Modelo das 4 Abas na Planilha Google Sheets"),
                hermesReact_1.React.createElement("ul", { className: "ifood-schema-list" },
                    hermesReact_1.React.createElement("li", null,
                        hermesReact_1.React.createElement("strong", null, "Lojas:"),
                        ' ',
                        hermesReact_1.React.createElement("code", null, "store_key, channel (ANOTA_AI|IFOOD|99FOOD), merchant_id, store_name, execution_mode (DISABLED|API_FIRST|API_ONLY|PLAYWRIGHT_ONLY), sync_mode (REVIEW; AUTO_APPLY ignorado), portal_url, enabled, prep_time_minutes, status_action (OPEN|PAUSE), pause_reason, pause_start_iso, pause_end_iso")),
                    hermesReact_1.React.createElement("li", null,
                        hermesReact_1.React.createElement("strong", null, "Horarios:"),
                        ' ',
                        hermesReact_1.React.createElement("code", null, "store_key, day_of_week, start_time, end_time, duration_minutes, enabled")),
                    hermesReact_1.React.createElement("li", null,
                        hermesReact_1.React.createElement("strong", null, "Cardapio:"),
                        ' ',
                        hermesReact_1.React.createElement("code", null, "store_key, item_id, item_name, description, price_brl, original_price_brl, available")),
                    hermesReact_1.React.createElement("li", null,
                        hermesReact_1.React.createElement("strong", null, "Auditoria_Sync:"),
                        " preenchida automaticamente com timestamp, loja, dom\u00EDnio, executor e resultado.")),
                hermesReact_1.React.createElement("h3", null, "Google Apps Script (Webhook HMAC em tempo real)"),
                hermesReact_1.React.createElement("pre", { className: "ifood-pre" }, appsScript || 'Carregando apps_script/Code.gs…')),
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Auditoria Recente"),
                hermesReact_1.React.createElement("p", { className: "ifood-muted" },
                    "Runtime do poller: ",
                    readiness?.sheets_poller_runtime.state || 'não observado',
                    " \u00B7 Poller: ",
                    overview?.sheets_poller?.last_success_at
                        ? `última reconciliação ${new Date(overview.sheets_poller.last_success_at).toLocaleString()}`
                        : 'nenhuma reconciliação automática registrada',
                    overview?.sheets_poller?.last_error
                        ? ` · ${overview.sheets_poller.last_error} (${overview.sheets_poller.consecutive_failures} falhas)`
                        : ''),
                auditEvents.length === 0 ? (hermesReact_1.React.createElement("p", { className: "ifood-muted" }, "Nenhum evento de auditoria registrado.")) : (hermesReact_1.React.createElement("div", { className: "ifood-table-wrap" },
                    hermesReact_1.React.createElement("table", { className: "ifood-table" },
                        hermesReact_1.React.createElement("thead", null,
                            hermesReact_1.React.createElement("tr", null,
                                hermesReact_1.React.createElement("th", null, "Data/Hora UTC"),
                                hermesReact_1.React.createElement("th", null, "Loja"),
                                hermesReact_1.React.createElement("th", null, "Dom\u00EDnio"),
                                hermesReact_1.React.createElement("th", null, "Executor"),
                                hermesReact_1.React.createElement("th", null, "Resultado"),
                                hermesReact_1.React.createElement("th", null, "Resumo"))),
                        hermesReact_1.React.createElement("tbody", null, auditEvents.map((ev) => (hermesReact_1.React.createElement("tr", { key: ev.id },
                            hermesReact_1.React.createElement("td", null, ev.timestamp_utc),
                            hermesReact_1.React.createElement("td", null, ev.store_key),
                            hermesReact_1.React.createElement("td", null, ev.domain),
                            hermesReact_1.React.createElement("td", null, ev.backend_used),
                            hermesReact_1.React.createElement("td", null,
                                hermesReact_1.React.createElement("span", { className: badgeClass(ev.outcome) }, ev.outcome)),
                            hermesReact_1.React.createElement("td", null, ev.summary)))))))))))));
}

},
"./api": function(module, exports, require) {
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.confirmLegacyOpeningHours = exports.proposeLegacyOpeningHours = exports.fetchLegacyOpeningHours = exports.fetchAppsScript = exports.reconcileSyncProposal = exports.rejectSyncProposal = exports.confirmSyncProposal = exports.triggerSheetSync = exports.fetchSyncOverview = exports.fetchTenants = exports.API_BASE = void 0;
const hermesReact_1 = require("./hermesReact");
exports.API_BASE = '/api/plugins/ifood-loja';
const fetchTenants = () => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/tenants`);
exports.fetchTenants = fetchTenants;
const fetchSyncOverview = (tenant_key) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/sync/overview?tenant_key=${encodeURIComponent(tenant_key)}`);
exports.fetchSyncOverview = fetchSyncOverview;
const triggerSheetSync = (payload) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/sheets/sync`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
});
exports.triggerSheetSync = triggerSheetSync;
const confirmSyncProposal = (proposal_id, tenant_key, override_guardrail = false) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/sync/proposals/${proposal_id}/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ override_guardrail, tenant_key }),
});
exports.confirmSyncProposal = confirmSyncProposal;
const rejectSyncProposal = (proposal_id, reason, tenant_key) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/sync/proposals/${proposal_id}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason, tenant_key }),
});
exports.rejectSyncProposal = rejectSyncProposal;
const reconcileSyncProposal = (proposal_id, tenant_key) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/sync/proposals/${proposal_id}/reconcile?tenant_key=${encodeURIComponent(tenant_key)}`, { method: 'POST' });
exports.reconcileSyncProposal = reconcileSyncProposal;
const fetchAppsScript = () => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/sheets/apps-script`);
exports.fetchAppsScript = fetchAppsScript;
const fetchLegacyOpeningHours = () => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/opening-hours`);
exports.fetchLegacyOpeningHours = fetchLegacyOpeningHours;
const proposeLegacyOpeningHours = (shifts) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/opening-hours/proposals`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shifts }),
});
exports.proposeLegacyOpeningHours = proposeLegacyOpeningHours;
const confirmLegacyOpeningHours = (proposal_id) => (0, hermesReact_1.fetchJSON)(`${exports.API_BASE}/opening-hours/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ proposal_id }),
});
exports.confirmLegacyOpeningHours = confirmLegacyOpeningHours;

},
"./hermesReact": function(module, exports, require) {
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.fetchJSON = exports.useCallback = exports.useEffect = exports.useState = exports.React = void 0;
function sdk() {
    const value = window.__HERMES_PLUGIN_SDK__;
    if (!value)
        throw new Error('Hermes SDK indisponível');
    return value;
}
exports.React = {
    createElement: ((...args) => sdk().React.createElement(...args)),
    get Fragment() {
        return sdk().React.Fragment;
    },
};
exports.useState = ((...args) => sdk().hooks.useState(...args));
exports.useEffect = ((...args) => sdk().hooks.useEffect(...args));
exports.useCallback = ((...args) => sdk().hooks.useCallback(...args));
const fetchJSON = (path, init) => sdk().fetchJSON(path, init, { allowUnauthorized: true });
exports.fetchJSON = fetchJSON;

}
};
const cache = Object.create(null);
function load(id) {
  if (cache[id]) return cache[id].exports;
  const factory = modules[id];
  if (!factory) throw new Error('Módulo não empacotado: ' + id);
  const module = { exports: {} };
  cache[id] = module;
  factory(module, module.exports, (specifier) => load(resolve(id, specifier)));
  return module.exports;
}
function resolve(from, specifier) {
  const parts = from.slice(2).split('/'); parts.pop();
  for (const part of specifier.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') parts.pop(); else parts.push(part);
  }
  return './' + parts.join('/');
}
load("./index");
})();
