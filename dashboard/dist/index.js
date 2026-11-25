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
const DOMAIN = {
    OPENING_HOURS: 'Horários',
    CATALOG: 'Cardápio',
    STORE_STATUS: 'Status da loja',
    PREP_TIME: 'Tempo de preparo',
};
const CHANNEL = { IFOOD: 'iFood', '99FOOD': '99Food', ANOTA_AI: 'Anota AI' };
const STATUS = {
    PENDING: 'Aguardando revisão',
    EXECUTING: 'Em execução',
    EXECUTED: 'Aplicado',
    APPLIED: 'Aplicado',
    CONFIRMED: 'Confirmado',
    UNCERTAIN: 'Resultado incerto',
    BLOCKED: 'Bloqueado',
    FAILED: 'Falhou',
    REJECTED: 'Rejeitado',
    STALE: 'Expirado',
    EXPIRED: 'Expirado',
};
const WEEKDAY = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];
function badge(value) {
    const v = String(value || '').toUpperCase();
    if (['CONFIRMED', 'APPLIED', 'EXECUTED', 'OK'].includes(v))
        return 'ifood-badge is-ok';
    if (['BLOCKED', 'FAILED', 'DIVERGENT', 'ERROR'].includes(v) || v.includes('CAPTCHA'))
        return 'ifood-badge is-danger';
    if (v.includes('PENDING') || v.includes('LOGIN') || v === 'REVIEW' || v === 'UNCERTAIN')
        return 'ifood-badge is-warn';
    return 'ifood-badge is-neutral';
}
function when(iso) {
    if (!iso)
        return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}
function changeLines(p) {
    const cur = p.current;
    const pro = p.proposed;
    if (p.domain === 'OPENING_HOURS') {
        const fmt = (rows) => (rows || []).map((s) => `${WEEKDAY[Number(s.dayOfWeek)] ?? s.dayOfWeek} ${s.start}+${s.duration}min`).join(' · ') || 'sem turnos';
        return [`antes: ${fmt(cur)}`, `depois: ${fmt(pro)}`];
    }
    if (p.domain === 'CATALOG') {
        const before = new Map((cur || []).map((i) => [String(i.item_id), i]));
        const out = [];
        for (const item of pro || []) {
            const old = before.get(String(item.item_id));
            const bits = [];
            if (!old)
                bits.push('novo no cardápio');
            if (old && old.price_cents !== item.price_cents)
                bits.push(`R$ ${(old.price_cents / 100).toFixed(2)} → R$ ${(item.price_cents / 100).toFixed(2)}`);
            if (old && old.available !== item.available)
                bits.push(item.available ? 'volta ao cardápio' : 'sai do cardápio');
            if (bits.length)
                out.push(`${item.item_name || item.item_id}: ${bits.join(' · ')}`);
            before.delete(String(item.item_id));
        }
        for (const rest of before.values())
            out.push(`${rest.item_name || rest.item_id}: fora da proposta`);
        return out.length ? out : ['sem diferença em relação ao estado atual'];
    }
    if (p.domain === 'STORE_STATUS') {
        const acts = { PAUSE: 'pausar a loja', RESUME: 'reabrir a loja', OPEN: 'abrir a loja', CLOSE: 'fechar a loja' };
        const label = acts[String(pro?.status_action || '').toUpperCase()] || `ação ${pro?.status_action || 'não informada'}`;
        const bits = [label];
        if (pro?.pause_reason)
            bits.push(`motivo: ${pro.pause_reason}`);
        if (pro?.pause_end_iso)
            bits.push(`até ${when(pro.pause_end_iso)}`);
        return [`depois: ${bits.join(' · ')}`];
    }
    if (p.domain === 'PREP_TIME') {
        return [
            `antes: ${cur?.prep_time_minutes != null ? `${cur.prep_time_minutes} min` : 'não informado'}`,
            `depois: ${pro?.prep_time_minutes != null ? `${pro.prep_time_minutes} min` : 'não informado'}`,
        ];
    }
    return [JSON.stringify(pro)];
}
function IfoodStorePage() {
    const [tab, setTab] = (0, hermesReact_1.useState)('overview');
    const [ov, setOv] = (0, hermesReact_1.useState)(null);
    const [tenants, setTenants] = (0, hermesReact_1.useState)([]);
    const [tenant, setTenant] = (0, hermesReact_1.useState)('');
    const [appScript, setAppScript] = (0, hermesReact_1.useState)('');
    const [loading, setLoading] = (0, hermesReact_1.useState)(true);
    const [busy, setBusy] = (0, hermesReact_1.useState)('');
    const [error, setError] = (0, hermesReact_1.useState)('');
    const [notice, setNotice] = (0, hermesReact_1.useState)('');
    const [dryRun, setDryRun] = (0, hermesReact_1.useState)(true);
    const load = (0, hermesReact_1.useCallback)(async (key) => {
        if (!key)
            return;
        setLoading(true);
        setError('');
        try {
            setOv(await (0, api_1.fetchSyncOverview)(key));
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha ao carregar a sincronização.');
        }
        finally {
            setLoading(false);
        }
    }, []);
    (0, hermesReact_1.useEffect)(() => {
        void (0, api_1.fetchTenants)()
            .then(async (items) => {
            setTenants(items);
            const first = items[0]?.tenant_key || 'default';
            setTenant(first);
            await load(first);
        })
            .catch((e) => {
            setError(e instanceof Error ? e.message : 'Falha ao listar as lojas.');
            setLoading(false);
        });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    const ready = ov?.readiness;
    const proposals = ov?.proposals || [];
    const events = ov?.audit_events || [];
    const snaps = ov?.snapshots || [];
    const sessions = (ov?.portal_sessions || {})[tenant] || {};
    const pending = proposals.filter((p) => p.status === 'PENDING' || p.status === 'BLOCKED');
    const applied = proposals.filter((p) => ['EXECUTED', 'APPLIED', 'CONFIRMED'].includes(p.status));
    const uncertain = proposals.filter((p) => p.status === 'UNCERTAIN');
    const policy = ready?.automation_policy || ready?.default_sync_mode || 'REVIEW';
    const caps = ready?.write_capabilities || [];
    const act = async (key, fn) => {
        setBusy(key);
        setError('');
        try {
            await fn();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : 'Falha na operação.');
        }
        finally {
            setBusy('');
        }
    };
    const sync = () => act('sync', async () => {
        const out = await (0, api_1.triggerSheetSync)({ tenant_key: tenant, dry_run: dryRun });
        setNotice(`${dryRun ? 'Simulação' : 'Sincronização'}: ${out.summary || 'concluída'}`);
        await load(tenant);
    });
    return (hermesReact_1.React.createElement("main", { className: "ifood-sync-root" },
        hermesReact_1.React.createElement("header", { className: "ifood-header" },
            hermesReact_1.React.createElement("div", null,
                hermesReact_1.React.createElement("span", { className: "ifood-eyebrow" }, "HERMES / OPERA\u00C7\u00C3O DE LOJAS"),
                hermesReact_1.React.createElement("h1", null, "Loja iFood / Sheets"),
                hermesReact_1.React.createElement("p", { className: "ifood-subtitle" }, "A planilha \u00E9 o comando. A diverg\u00EAncia \u00E9 aplicada e registrada com autor \u2014 o Telegram avisa, n\u00E3o autoriza.")),
            hermesReact_1.React.createElement("div", { className: "ifood-header-actions" },
                tenants.length > 1 ? (hermesReact_1.React.createElement("select", { "aria-label": "Selecionar loja", value: tenant, onChange: (e) => { setTenant(e.target.value); setOv(null); void load(e.target.value); } }, tenants.map((t) => hermesReact_1.React.createElement("option", { key: t.tenant_key, value: t.tenant_key }, t.tenant_key)))) : (hermesReact_1.React.createElement("span", { className: "ifood-pill is-neutral" }, tenant || 'default')),
                hermesReact_1.React.createElement("span", { className: policy === 'AUTO_APPLY' ? 'ifood-badge is-ok' : 'ifood-badge is-warn' }, policy === 'AUTO_APPLY' ? 'AUTO_APPLY · a planilha aplica' : 'REVIEW · revisão ativa'),
                hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", disabled: loading, onClick: () => void load(tenant) }, "Atualizar"))),
        hermesReact_1.React.createElement("section", { className: "ifood-summary-grid" },
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Aguardando revis\u00E3o"),
                hermesReact_1.React.createElement("strong", null, pending.length),
                hermesReact_1.React.createElement("small", null, "bloqueadas por guardrail ou capacidade")),
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Aplicadas"),
                hermesReact_1.React.createElement("strong", null, applied.length),
                hermesReact_1.React.createElement("small", null, "mudan\u00E7as que chegaram ao canal")),
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Resultado incerto"),
                hermesReact_1.React.createElement("strong", null, uncertain.length),
                hermesReact_1.React.createElement("small", null, "exigem reconcilia\u00E7\u00E3o")),
            hermesReact_1.React.createElement("div", { className: "ifood-summary-card" },
                hermesReact_1.React.createElement("span", null, "Lojas monitoradas"),
                hermesReact_1.React.createElement("strong", null, tenants.length || 1),
                hermesReact_1.React.createElement("small", null,
                    snaps.length,
                    " estados registrados"))),
        loading ? hermesReact_1.React.createElement("p", { className: "ifood-banner is-info" }, "Carregando painel de sincroniza\u00E7\u00E3o\u2026") : null,
        error ? hermesReact_1.React.createElement("p", { role: "alert", className: "ifood-banner is-error" }, error) : null,
        notice ? hermesReact_1.React.createElement("p", { role: "status", className: "ifood-banner is-success" }, notice) : null,
        hermesReact_1.React.createElement("nav", { className: "ifood-tabs" }, [
            ['overview', 'Visão geral'],
            ['changes', `Mudanças (${pending.length + applied.length})`],
            ['audit', 'Auditoria'],
            ['sheet', 'Planilha'],
        ].map(([id, label]) => (hermesReact_1.React.createElement("button", { key: id, type: "button", className: tab === id ? 'ifood-tab is-active' : 'ifood-tab', onClick: () => setTab(id) }, label)))),
        tab === 'overview' && ready ? (hermesReact_1.React.createElement("section", { className: "ifood-stack" },
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Prontid\u00E3o das integra\u00E7\u00F5es"),
                hermesReact_1.React.createElement("ul", { className: "ifood-schema-list" },
                    hermesReact_1.React.createElement("li", null,
                        "Plugin: ",
                        hermesReact_1.React.createElement("strong", null, ready.enabled ? 'habilitado' : 'desligado')),
                    hermesReact_1.React.createElement("li", null,
                        "Google Sheets API: ",
                        hermesReact_1.React.createElement("strong", null, ready.sheets_api_configured ? 'configurado' : 'pendente')),
                    hermesReact_1.React.createElement("li", null,
                        "Planilha da loja: ",
                        hermesReact_1.React.createElement("strong", null, ready.sheets_spreadsheet_id ? 'informada' : 'não informada')),
                    hermesReact_1.React.createElement("li", null,
                        "Webhook HMAC: ",
                        hermesReact_1.React.createElement("strong", null, ready.webhook_configured ? 'configurado' : 'pendente')),
                    hermesReact_1.React.createElement("li", null,
                        "Poller: ",
                        hermesReact_1.React.createElement("strong", null, ready.sheets_poller_enabled ? 'ativo' : `desligado (${ready.sheets_poller_runtime?.state || '—'})`)),
                    hermesReact_1.React.createElement("li", null,
                        "iFood API: ",
                        hermesReact_1.React.createElement("strong", null, ready.ifood_api_configured ? 'configurado' : 'pendente')),
                    hermesReact_1.React.createElement("li", null,
                        "Playwright (portais): ",
                        hermesReact_1.React.createElement("strong", null, ready.playwright_configured ? 'configurado' : 'pendente')))),
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Pol\u00EDtica de escrita"),
                hermesReact_1.React.createElement("ul", { className: "ifood-schema-list" },
                    hermesReact_1.React.createElement("li", null,
                        "Modo: ",
                        hermesReact_1.React.createElement("strong", null, policy)),
                    hermesReact_1.React.createElement("li", null,
                        "Backend preferido: ",
                        hermesReact_1.React.createElement("strong", null, ready.default_execution_mode)),
                    hermesReact_1.React.createElement("li", null,
                        "Capacidades liberadas: ",
                        hermesReact_1.React.createElement("strong", null, caps.length ? caps.join(', ') : 'todas bloqueadas')),
                    hermesReact_1.React.createElement("li", null,
                        "Limite de varia\u00E7\u00E3o de pre\u00E7o: ",
                        hermesReact_1.React.createElement("strong", null,
                            ready.max_price_change_pct,
                            "%")))),
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Sess\u00F5es dos portais"),
                hermesReact_1.React.createElement("div", { className: "ifood-readiness-bar" }, ['IFOOD', '99FOOD', 'ANOTA_AI'].map((ch) => {
                    const s = sessions[ch] || {};
                    const st = s.state || 'não configurado';
                    return hermesReact_1.React.createElement("div", { key: ch, className: badge(st) },
                        CHANNEL[ch],
                        ": ",
                        st,
                        s.last_error ? ` · ${s.last_error}` : '');
                }))))) : null,
        tab === 'changes' ? (hermesReact_1.React.createElement("section", { className: "ifood-stack" }, proposals.length === 0 ? (hermesReact_1.React.createElement("div", { className: "ifood-card" },
            hermesReact_1.React.createElement("p", { className: "ifood-muted" }, "Nenhuma mudan\u00E7a registrada. Editar a planilha cria a mudan\u00E7a; ela aparece aqui aplicada ou com o motivo do bloqueio."))) : (proposals.map((p) => (hermesReact_1.React.createElement("article", { className: "ifood-card", key: p.proposal_id },
            hermesReact_1.React.createElement("div", { className: "ifood-card-top" },
                hermesReact_1.React.createElement("div", null,
                    hermesReact_1.React.createElement("h3", null,
                        DOMAIN[p.domain] || p.domain,
                        " \u00B7 ",
                        p.store_key),
                    hermesReact_1.React.createElement("span", { className: "ifood-meta" },
                        CHANNEL[p.channel] || p.channel,
                        " \u00B7 autor ",
                        hermesReact_1.React.createElement("strong", null, p.actor_id),
                        " \u00B7 ",
                        when(p.created_at),
                        " \u00B7 backend ",
                        p.backend_planned)),
                hermesReact_1.React.createElement("span", { className: badge(p.status) }, STATUS[p.status] || p.status)),
            hermesReact_1.React.createElement("ul", { className: "ifood-schema-list" }, changeLines(p).map((l, i) => hermesReact_1.React.createElement("li", { key: i }, l))),
            (p.guardrail_warnings || []).length ? (hermesReact_1.React.createElement("div", { className: "ifood-guardrail-box" },
                "Avisos: ",
                p.guardrail_warnings.join(' | '))) : null,
            hermesReact_1.React.createElement("div", { className: "ifood-row-actions" },
                p.status === 'UNCERTAIN' ? (hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", disabled: busy === `rec-${p.proposal_id}`, onClick: () => void act(`rec-${p.proposal_id}`, async () => { const r = await (0, api_1.reconcileSyncProposal)(p.proposal_id, tenant); setNotice(`Reconciliação: ${r.outcome}`); await load(tenant); }) }, "Reconciliar")) : null,
                p.status === 'PENDING' || p.status === 'BLOCKED' ? (hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-danger", disabled: busy === `rj-${p.proposal_id}`, onClick: () => void act(`rj-${p.proposal_id}`, async () => { await (0, api_1.rejectSyncProposal)(p.proposal_id, 'descartada pelo operador', tenant); setNotice('Mudança descartada.'); await load(tenant); }) }, "Descartar")) : null,
                hermesReact_1.React.createElement("span", { className: "ifood-meta" }, p.proposal_id)))))))) : null,
        tab === 'audit' ? (hermesReact_1.React.createElement("section", { className: "ifood-card" }, events.length === 0 ? hermesReact_1.React.createElement("p", { className: "ifood-muted" }, "Nenhum evento de auditoria registrado.") : (hermesReact_1.React.createElement("div", { className: "ifood-table-wrap" },
            hermesReact_1.React.createElement("table", { className: "ifood-table" },
                hermesReact_1.React.createElement("thead", null,
                    hermesReact_1.React.createElement("tr", null,
                        hermesReact_1.React.createElement("th", null, "Quando"),
                        hermesReact_1.React.createElement("th", null, "Loja"),
                        hermesReact_1.React.createElement("th", null, "Dom\u00EDnio"),
                        hermesReact_1.React.createElement("th", null, "Backend"),
                        hermesReact_1.React.createElement("th", null, "Resultado"),
                        hermesReact_1.React.createElement("th", null, "Resumo"))),
                hermesReact_1.React.createElement("tbody", null, events.map((ev) => (hermesReact_1.React.createElement("tr", { key: ev.id },
                    hermesReact_1.React.createElement("td", null, when(ev.timestamp_utc)),
                    hermesReact_1.React.createElement("td", null, ev.store_key),
                    hermesReact_1.React.createElement("td", null, DOMAIN[ev.domain] || ev.domain),
                    hermesReact_1.React.createElement("td", null, ev.backend_used),
                    hermesReact_1.React.createElement("td", null,
                        hermesReact_1.React.createElement("span", { className: badge(ev.outcome) }, ev.outcome)),
                    hermesReact_1.React.createElement("td", null, ev.summary)))))))))) : null,
        tab === 'sheet' ? (hermesReact_1.React.createElement("section", { className: "ifood-stack" },
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Sincronizar a planilha"),
                hermesReact_1.React.createElement("p", { className: "ifood-muted" }, "L\u00EA a planilha (fonte da verdade), compara com o estado da loja e aplica quando a pol\u00EDtica \u00E9 AUTO_APPLY e a capacidade est\u00E1 liberada. Sem capacidade, fica registrado com o motivo."),
                hermesReact_1.React.createElement("div", { className: "ifood-row-actions" },
                    hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", onClick: () => setDryRun((v) => !v) },
                        "Modo: ",
                        dryRun ? 'simulação' : 'aplicar'),
                    hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-primary", disabled: busy === 'sync', onClick: () => void sync() }, busy === 'sync' ? 'Sincronizando…' : 'Sincronizar agora'))),
            hermesReact_1.React.createElement("div", { className: "ifood-card" },
                hermesReact_1.React.createElement("h2", null, "Apps Script (webhook assinado)"),
                hermesReact_1.React.createElement("pre", { className: "ifood-pre" }, appScript || 'Clique em atualizar para carregar apps_script/Code.gs'),
                hermesReact_1.React.createElement("div", { className: "ifood-row-actions" },
                    hermesReact_1.React.createElement("button", { type: "button", className: "ifood-btn-secondary", disabled: busy === 'gs', onClick: () => void act('gs', async () => { const r = await (0, api_1.fetchAppsScript)(); setAppScript(r.content || ''); }) }, "Carregar Apps Script"))))) : null));
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
