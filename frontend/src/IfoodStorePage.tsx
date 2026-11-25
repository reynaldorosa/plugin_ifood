import {
  confirmSyncProposal,
  fetchAppsScript,
  fetchSyncOverview,
  fetchTenants,
  reconcileSyncProposal,
  rejectSyncProposal,
  SyncOverviewResponse,
  triggerSheetSync,
} from './api';
import { React, useCallback, useEffect, useState } from './hermesReact';

type ActiveTab = 'queue' | 'stores' | 'setup';

const EXAMPLE_SNAPSHOT = JSON.stringify(
  {
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
  },
  null,
  2,
);

function badgeClass(value: string): string {
  const upper = value.toUpperCase();
  if (upper === 'CONFIRMED' || upper === 'APPLIED' || upper === 'OK') return 'ifood-badge is-ok';
  if (upper.includes('BLOCKED') || upper.includes('CAPTCHA') || upper === 'ERROR' || upper === 'DIVERGENT') {
    return 'ifood-badge is-danger';
  }
  if (upper.includes('PENDING') || upper.includes('LOGIN') || upper === 'REVIEW') {
    return 'ifood-badge is-warn';
  }
  return 'ifood-badge is-neutral';
}

export function IfoodStorePage() {
  const [tab, setTab] = useState<ActiveTab>('queue');
  const [overview, setOverview] = useState<SyncOverviewResponse | null>(null);
  const [tenants, setTenants] = useState<Array<{tenant_key: string; sheet_configured: boolean}>>([]);
  const [tenantKey, setTenantKey] = useState<string>('');
  const [appsScript, setAppsScript] = useState<string>('');
  const [loading, setLoading] = useState<boolean>(true);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string>('');
  const [notice, setNotice] = useState<string>('');
  const [rowErrors, setRowErrors] = useState<Array<Record<string, unknown>>>([]);
  const [showInlineSync, setShowInlineSync] = useState<boolean>(false);
  const [inlineJson, setInlineJson] = useState<string>(EXAMPLE_SNAPSHOT);


  const loadAll = useCallback(async (targetTenant = tenantKey) => {
    if (!targetTenant) return;
    setLoading(true);
    setError('');
    try {
      const data = await fetchSyncOverview(targetTenant);
      setOverview(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao carregar sincronização.');
    } finally {
      setLoading(false);
    }
  }, [tenantKey]);

  useEffect(() => {
    void fetchTenants().then(async (items) => {
      setTenants(items);
      const first = items[0]?.tenant_key || '';
      setTenantKey((current) => current || first);
      if (first) {
        await loadAll(first);
      } else {
        setLoading(false);
      }
    }).catch((e) => {
      setError(e instanceof Error ? e.message : 'Falha ao listar padarias.');
      setLoading(false);
    });
  }, []);

  const handleLoadAppsScript = async () => {
    if (appsScript) return;
    try {
      const res = await fetchAppsScript();
      setAppsScript(res.content || '');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao obter Apps Script.');
    }
  };

  const handleSyncPull = async () => {
    setBusy(true);
    setError('');
    setNotice('');
    setRowErrors([]);
    try {
      const res = await triggerSheetSync({tenant_key: tenantKey});
      setNotice(res.summary);
      setRowErrors(res.row_errors || []);
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao sincronizar com Google Sheets.');
    } finally {
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
      const res = await triggerSheetSync({...parsed, tenant_key: tenantKey});
      setNotice(res.summary);
      setRowErrors(res.row_errors || []);
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Snapshot JSON inválido.');
    } finally {
      setBusy(false);
    }
  };

  const handleConfirmProposal = async (proposalId: string, overrideGuardrail = false) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const res = await confirmSyncProposal(proposalId, tenantKey, overrideGuardrail);
      const outcome = String(res.execution.outcome || '');
      setNotice(`Proposta ${proposalId.slice(0, 8)} executada: ${outcome} — ${String(res.execution.message || '')}`);
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao confirmar proposta.');
    } finally {
      setBusy(false);
    }
  };

  const handleRejectProposal = async (proposalId: string) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await rejectSyncProposal(proposalId, 'Rejeitado manualmente no Dashboard Hermes', tenantKey);
      setNotice(`Proposta ${proposalId.slice(0, 8)} rejeitada.`);
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao rejeitar proposta.');
    } finally {
      setBusy(false);
    }
  };

  const handleReconcileProposal = async (proposalId: string) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await reconcileSyncProposal(proposalId, tenantKey);
      setNotice(`Reconciliação somente leitura: ${result.outcome}.`);
      await loadAll();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha na reconciliação.');
    } finally {
      setBusy(false);
    }
  };

  const proposals = overview?.proposals || [];
  const activeProposals = proposals.filter(
    (p) => p.status === 'PENDING' || p.status === 'BLOCKED',
  );
  const snapshots = overview?.snapshots || [];
  const auditEvents = overview?.audit_events || [];
  const readiness = overview?.readiness;

  return (
    <main className="ifood-sync-root">
      <header className="ifood-header">
        <div>
          <span className="ifood-eyebrow">HERMES / OPERAÇÃO DE LOJAS</span>
          <h1>Central de sincronização</h1>
          <p className="ifood-subtitle">
            Propostas auditadas para revisão. Escritas permanecem bloqueadas até validação de cada
            capacidade, merchant e executor.
          </p>
        </div>
        <div className="ifood-header-actions">
          <button
            type="button"
            className="ifood-btn-primary"
            disabled={busy || loading}
            onClick={() => void handleSyncPull()}
          >
            {busy ? 'Sincronizando…' : 'Sincronizar Planilha Agora'}
          </button>
          <button
            type="button"
            className="ifood-btn-secondary"
            disabled={busy}
            onClick={() => setShowInlineSync((prev) => !prev)}
          >
            {showInlineSync ? 'Ocultar Snapshot JSON' : 'Simular / Enviar Snapshot'}
          </button>
        </div>
      </header>

      <section className="ifood-card" aria-label="Padaria selecionada">
        <label htmlFor="ifood-tenant-select">Padaria</label>
        <select id="ifood-tenant-select" aria-label="Selecionar padaria" value={tenantKey}
          onChange={(e) => {
            const next = e.target.value;
            setOverview(null);
            setTenantKey(next);
            void loadAll(next);
          }}>
          {tenants.map((tenant) => <option key={tenant.tenant_key} value={tenant.tenant_key}>{tenant.tenant_key}</option>)}
        </select>
      </section>

      <section className="ifood-summary-grid" aria-label="Resumo da operação">
        <div className="ifood-summary-card"><span>Para revisar</span><strong>{activeProposals.length}</strong><small>propostas aguardando decisão</small></div>
        <div className="ifood-summary-card"><span>Estados registrados</span><strong>{snapshots.length}</strong><small>domínios com histórico</small></div>
        <div className="ifood-summary-card"><span>Resultado incerto</span><strong>{proposals.filter((p) => p.status === 'UNCERTAIN').length}</strong><small>exigem reconciliação</small></div>
      </section>

      {readiness && (
        <section className="ifood-integrations" aria-label="Status das integrações">
          <div className="ifood-section-heading"><span>Integrações</span><small>Configuração e capacidade de execução</small></div>
          <div className="ifood-readiness-bar">
          <div className={readiness.sheets_api_configured ? 'ifood-pill is-ready' : 'ifood-pill'}>
            Google Sheets API: {readiness.sheets_api_configured ? 'Configurada' : 'Pendente'}
          </div>
          <div className={readiness.webhook_configured ? 'ifood-pill is-ready' : 'ifood-pill'}>
            Webhook HMAC: {readiness.webhook_configured ? 'Ativo' : 'Pendente'}
          </div>
          <div className={readiness.sheets_poller_enabled ? 'ifood-pill is-ready' : 'ifood-pill'}>
            Poller Google Sheets: {readiness.sheets_poller_enabled ? 'Ativo' : 'Desativado'}
          </div>
          <div className={readiness.ifood_api_configured ? 'ifood-pill is-ready' : 'ifood-pill'}>
            iFood API: {readiness.ifood_api_configured ? 'Credencial configurada; escritas exigem contrato validado' : 'Sem credencial'}
          </div>
          <div className={readiness.playwright_configured ? 'ifood-pill is-ready' : 'ifood-pill'}>
            Playwright: {readiness.playwright_configured ? `Configurado; domínios validados: ${readiness.validated_portal_domains.join(', ') || 'nenhum'}` : 'Não configurado'}
          </div>
          <div className="ifood-pill is-neutral">
            Política: REVIEW · Capacidades de escrita: {readiness.write_capabilities.join(', ') || 'todas bloqueadas'}
          </div>
          <div className="ifood-pill is-neutral">
            Poller runtime: {readiness.sheets_poller_runtime.state}
            {readiness.sheets_poller_runtime.detail ? ` · ${readiness.sheets_poller_runtime.detail}` : ''}
          </div>
          </div>
        </section>
      )}

      {overview?.portal_sessions?.[tenantKey] && (
        <section className="ifood-card" aria-label="Sessões dos portais">
          <h2>Sessões dos portais</h2>
          <div className="ifood-readiness-bar">
            {Object.entries(overview.portal_sessions[tenantKey]).map(([channel, session]) => (
              <div key={channel} className={badgeClass(session.state)}>
                {channel}: {session.state}{session.last_error ? ` · ${session.last_error}` : ''}
              </div>
            ))}
          </div>
        </section>
      )}

      {loading && <p className="ifood-banner is-info">Carregando painel de sincronização…</p>}
      {error && (
        <p role="alert" className="ifood-banner is-error">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="ifood-banner is-success">
          {notice}
        </p>
      )}
      {rowErrors.length > 0 && (
        <section className="ifood-card" aria-label="Erros de validação da planilha">
          <h2>Sincronização bloqueada por erros de linha</h2>
          <ul>
            {rowErrors.map((item, index) => (
              <li key={index}>
                {String(item.sheet || 'Aba desconhecida')} · linha {String(item.row || '?')}: {String(item.message || 'Erro de validação')}
              </li>
            ))}
          </ul>
        </section>
      )}

      {showInlineSync && (
        <section className="ifood-card">
          <h2>Testar / Enviar Snapshot de Planilha (JSON)</h2>
          <p className="ifood-muted">
            Snapshot sintético para validar parser, regras de diff e bloqueios. Nenhum portal real
            é acessado por este modo.
          </p>
          <textarea
            aria-label="Snapshot JSON"
            rows={10}
            className="ifood-textarea"
            value={inlineJson}
            onChange={(e) => setInlineJson(e.target.value)}
          />
          <div className="ifood-row-actions">
            <button
              type="button"
              className="ifood-btn-primary"
              disabled={busy}
              onClick={() => void handleInlineSync()}
            >
              Processar Snapshot
            </button>
          </div>
        </section>
      )}

      <nav className="ifood-tabs" aria-label="Navegação do plugin">
        <button
          type="button"
          className={tab === 'queue' ? 'ifood-tab is-active' : 'ifood-tab'}
          onClick={() => setTab('queue')}
        >
          Fila de Mudanças ({activeProposals.length})
        </button>
        <button
          type="button"
          className={tab === 'stores' ? 'ifood-tab is-active' : 'ifood-tab'}
          onClick={() => setTab('stores')}
        >
          Lojas Sincronizadas ({snapshots.length})
        </button>
        <button
          type="button"
          className={tab === 'setup' ? 'ifood-tab is-active' : 'ifood-tab'}
          onClick={() => {
            setTab('setup');
            void handleLoadAppsScript();
          }}
        >
          Google Sheets & Auditoria
        </button>
      </nav>

      <div className="ifood-content-heading">
        <div><span className="ifood-eyebrow">VISÃO ATUAL</span><h2>{tab === 'queue' ? 'Fila de mudanças' : tab === 'stores' ? 'Estados sincronizados' : 'Planilha e auditoria'}</h2></div>
        <span className="ifood-muted">{tab === 'queue' ? `${proposals.length} registros` : tab === 'stores' ? `${snapshots.length} registros` : `${auditEvents.length} eventos recentes`}</span>
      </div>

      {tab === 'queue' && (
        <section className="ifood-stack">
          {proposals.length === 0 ? (
            <div className="ifood-card">
              <p className="ifood-muted">
                Nenhuma mudança pendente da planilha. Edite sua planilha Google Sheets ou clique em
                <strong> Sincronizar Planilha Agora</strong>.
              </p>
            </div>
          ) : (
            proposals.map((prop) => {
              const isActionable = prop.status === 'PENDING' || prop.status === 'BLOCKED';
              const isUncertain = prop.status === 'UNCERTAIN';
              const hasWarnings = prop.guardrail_warnings.length > 0;
              return (
                <article key={prop.proposal_id} className="ifood-card">
                  <div className="ifood-card-top">
                    <div>
                      <h3>
                        {prop.store_key} · {prop.domain}
                      </h3>
                      <span className="ifood-meta">
                      Canal: {prop.channel} · Merchant: {prop.merchant_id || 'não identificado'} · Origem: {prop.trigger_source} · Executor:{' '}
                        <strong>{prop.backend_planned}</strong>
                      <br />Versão: {String(prop.metadata.source_version || '—')} · Expira: {new Date(prop.expires_at).toLocaleString()}
                      </span>
                    </div>
                    <span className={badgeClass(prop.status)}>{prop.status}</span>
                  </div>

                  {hasWarnings && (
                    <div className="ifood-guardrail-box">
                      <strong>Alertas de Guardrail:</strong>
                      <ul>
                        {prop.guardrail_warnings.map((w, idx) => (
                          <li key={idx}>{w}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <div className="ifood-diff-grid">
                    <div>
                      <h4>Estado Atual / Anterior</h4>
                      <pre className="ifood-pre">{JSON.stringify(prop.current, null, 2)}</pre>
                    </div>
                    <div>
                      <h4>Proposta da Planilha (Source of Truth)</h4>
                      <pre className="ifood-pre">{JSON.stringify(prop.proposed, null, 2)}</pre>
                    </div>
                  </div>

                  {isActionable && (
                    <div className="ifood-row-actions">
                      {!hasWarnings ? (
                        <button
                          type="button"
                          className="ifood-btn-primary"
                          disabled={busy}
                          onClick={() => void handleConfirmProposal(prop.proposal_id, false)}
                        >
                          Confirmar e Aplicar
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="ifood-btn-danger"
                          disabled={busy}
                          onClick={() => void handleConfirmProposal(prop.proposal_id, true)}
                        >
                          Confirmar mesmo com Guardrail
                        </button>
                      )}
                      <button
                        type="button"
                        className="ifood-btn-secondary"
                        disabled={busy}
                        onClick={() => void handleRejectProposal(prop.proposal_id)}
                      >
                        Rejeitar
                      </button>
                    </div>
                  )}
                  {isUncertain && (
                    <div className="ifood-guardrail-box">
                      <strong>Resultado incerto.</strong> A escrita não será repetida automaticamente.
                      Reconcilie o estado atual da loja antes de criar outra proposta.
                      <div className="ifood-row-actions">
                        <button type="button" className="ifood-btn-secondary" disabled={busy}
                          onClick={() => void handleReconcileProposal(prop.proposal_id)}>
                          Consultar e reconciliar
                        </button>
                      </div>
                    </div>
                  )}
                </article>
              );
            })
          )}
        </section>
      )}

      {tab === 'stores' && (
        <section className="ifood-stack">
          {snapshots.length === 0 ? (
            <div className="ifood-card">
              <p className="ifood-muted">Nenhum snapshot aplicado registrado ainda.</p>
            </div>
          ) : (
            snapshots.map((snap) => (
              <article key={`${snap.store_key}-${snap.domain}`} className="ifood-card">
                <div className="ifood-card-top">
                  <div>
                    <h3>
                      {snap.store_key} · {snap.domain}
                    </h3>
                    <span className="ifood-meta">
                      Executor: {snap.last_backend} · Atualizado: {snap.updated_at}
                    </span>
                  </div>
                  <span className={badgeClass(snap.last_outcome)}>{snap.last_outcome}</span>
                </div>
                <pre className="ifood-pre">{JSON.stringify(snap.last_state, null, 2)}</pre>
              </article>
            ))
          )}
        </section>
      )}

      {tab === 'setup' && (
        <section className="ifood-stack">
          <div className="ifood-card">
            <h2>Modelo das 4 Abas na Planilha Google Sheets</h2>
            <ul className="ifood-schema-list">
              <li>
                <strong>Lojas:</strong>{' '}
                <code>
                  store_key, channel (ANOTA_AI|IFOOD|99FOOD), merchant_id, store_name, execution_mode
                  (DISABLED|API_FIRST|API_ONLY|PLAYWRIGHT_ONLY), sync_mode (REVIEW; AUTO_APPLY ignorado), portal_url,
                  enabled, prep_time_minutes, status_action (OPEN|PAUSE), pause_reason,
                  pause_start_iso, pause_end_iso
                </code>
              </li>
              <li>
                <strong>Horarios:</strong>{' '}
                <code>store_key, day_of_week, start_time, end_time, duration_minutes, enabled</code>
              </li>
              <li>
                <strong>Cardapio:</strong>{' '}
                <code>
                  store_key, item_id, item_name, description, price_brl, original_price_brl,
                  available
                </code>
              </li>
              <li>
                <strong>Auditoria_Sync:</strong> preenchida automaticamente com timestamp, loja,
                domínio, executor e resultado.
              </li>
            </ul>
            <h3>Google Apps Script (Webhook HMAC em tempo real)</h3>
            <pre className="ifood-pre">{appsScript || 'Carregando apps_script/Code.gs…'}</pre>
          </div>

          <div className="ifood-card">
            <h2>Auditoria Recente</h2>
            <p className="ifood-muted">
              Runtime do poller: {readiness?.sheets_poller_runtime.state || 'não observado'} · Poller: {overview?.sheets_poller?.last_success_at
                ? `última reconciliação ${new Date(overview.sheets_poller.last_success_at).toLocaleString()}`
                : 'nenhuma reconciliação automática registrada'}
              {overview?.sheets_poller?.last_error
                ? ` · ${overview.sheets_poller.last_error} (${overview.sheets_poller.consecutive_failures} falhas)`
                : ''}
            </p>
            {auditEvents.length === 0 ? (
              <p className="ifood-muted">Nenhum evento de auditoria registrado.</p>
            ) : (
              <div className="ifood-table-wrap">
                <table className="ifood-table">
                  <thead>
                    <tr>
                      <th>Data/Hora UTC</th>
                      <th>Loja</th>
                      <th>Domínio</th>
                      <th>Executor</th>
                      <th>Resultado</th>
                      <th>Resumo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {auditEvents.map((ev) => (
                      <tr key={ev.id}>
                        <td>{ev.timestamp_utc}</td>
                        <td>{ev.store_key}</td>
                        <td>{ev.domain}</td>
                        <td>{ev.backend_used}</td>
                        <td>
                          <span className={badgeClass(ev.outcome)}>{ev.outcome}</span>
                        </td>
                        <td>{ev.summary}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>
      )}
    </main>
  );
}
