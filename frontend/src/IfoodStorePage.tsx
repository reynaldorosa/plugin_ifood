import {
  fetchAppsScript,
  fetchSyncOverview,
  fetchTenants,
  reconcileSyncProposal,
  rejectSyncProposal,
  SyncOverviewResponse,
  SyncProposalRecord,
  triggerSheetSync,
} from './api';
import { React, useCallback, useEffect, useState } from './hermesReact';

/**
 * Aba do painel web — MESMO modelo da página nativa do app desktop:
 * a planilha é o comando; nada de "Confirmar/Aplicar" no caminho principal.
 * Aprovação humana existe só onde o motor não pode decidir:
 * resultado incerto (reconciliar) e proposta bloqueada (descartar).
 */

type Tab = 'overview' | 'changes' | 'audit' | 'sheet';
type Tone = 'is-ok' | 'is-warn' | 'is-muted' | 'is-default';

const DOMAIN: Record<string, string> = {
  OPENING_HOURS: 'Horários',
  CATALOG: 'Cardápio',
  STORE_STATUS: 'Status da loja',
  PREP_TIME: 'Tempo de preparo',
};
const CHANNEL: Record<string, string> = {
  IFOOD: 'iFood',
  '99FOOD': '99Food',
  NINETY_NINE: '99Food',
  ANOTA_AI: 'Anota AI',
};
const STATUS: Record<string, string> = {
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
const WEEKDAY_NUM = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];
const WEEKDAY_NAME: Record<string, string> = {
  SUNDAY: 'dom',
  MONDAY: 'seg',
  TUESDAY: 'ter',
  WEDNESDAY: 'qua',
  THURSDAY: 'qui',
  FRIDAY: 'sex',
  SATURDAY: 'sáb',
};

function fmtWeekday(day: unknown): string {
  const raw = String(day ?? '');
  return WEEKDAY_NAME[raw.toUpperCase()] ?? WEEKDAY_NUM[Number(raw)] ?? raw;
}

function fmtPrice(cents: unknown): string {
  return typeof cents === 'number' && Number.isFinite(cents) ? `R$ ${(cents / 100).toFixed(2)}` : '—';
}

function fmtState(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'sem leitura';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    return String(obj.state || obj.status || obj.reason || obj.detail || 'registrado');
  }
  return String(value);
}

function badge(value: string): string {
  const v = String(value || '').toUpperCase();
  if (['CONFIRMED', 'APPLIED', 'EXECUTED', 'OK', 'CONFIGURED'].includes(v)) return 'ifood-badge is-ok';
  if (['BLOCKED', 'FAILED', 'DIVERGENT', 'ERROR'].includes(v) || v.includes('CAPTCHA')) return 'ifood-badge is-danger';
  if (v.includes('PENDING') || v.includes('LOGIN') || v === 'REVIEW' || v === 'UNCERTAIN') return 'ifood-badge is-warn';
  if (v === 'EXECUTING') return 'ifood-badge is-info';
  return 'ifood-badge is-neutral';
}

function when(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function changeLines(p: SyncProposalRecord): string[] {
  const cur = p.current as any;
  const pro = p.proposed as any;
  if (p.domain === 'OPENING_HOURS') {
    const fmt = (rows: any[]) =>
      (rows || []).map((s: any) => `${fmtWeekday(s.dayOfWeek)} ${s.start}+${s.duration}min`).join(' · ') || 'sem turnos';
    return [`antes: ${fmt(cur)}`, `depois: ${fmt(pro)}`];
  }
  if (p.domain === 'CATALOG') {
    const before = new Map<string, any>((cur || []).map((i: any) => [String(i.item_id), i]));
    const out: string[] = [];
    for (const item of pro || []) {
      const old = before.get(String(item.item_id));
      const bits: string[] = [];
      if (!old) bits.push('novo no cardápio');
      if (old && old.price_cents !== item.price_cents && (old.price_cents != null || item.price_cents != null)) {
        bits.push(`${fmtPrice(old.price_cents)} → ${fmtPrice(item.price_cents)}`);
      }
      if (old && old.available !== item.available && (old.available != null || item.available != null)) {
        bits.push(item.available ? 'volta ao cardápio' : 'sai do cardápio');
      }
      if (bits.length) out.push(`${item.item_name || item.item_id}: ${bits.join(' · ')}`);
      before.delete(String(item.item_id));
    }
    for (const rest of before.values()) out.push(`${rest.item_name || rest.item_id}: fora da proposta`);
    return out.length ? out : ['sem diferença em relação ao estado atual'];
  }
  if (p.domain === 'STORE_STATUS') {
    const acts: Record<string, string> = {
      PAUSE: 'pausar a loja',
      RESUME: 'reabrir a loja',
      OPEN: 'abrir a loja',
      CLOSE: 'fechar a loja',
    };
    const label = acts[String(pro?.status_action || '').toUpperCase()] || `ação ${pro?.status_action || 'não informada'}`;
    const bits = [label];
    if (pro?.pause_reason) bits.push(`motivo: ${pro.pause_reason}`);
    if (pro?.pause_end_iso) bits.push(`até ${when(pro.pause_end_iso)}`);
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

function KvRow({ label, value, tone = 'is-default' }: { label: string; value: string; tone?: Tone }) {
  return (
    <div className="ifood-kv-row">
      <span className="ifood-kv-label">{label}</span>
      <span className={`ifood-kv-value ${tone}`}>{value}</span>
    </div>
  );
}

export function IfoodStorePage() {
  const [tab, setTab] = useState<Tab>('overview');
  const [ov, setOv] = useState<SyncOverviewResponse | null>(null);
  const [tenants, setTenants] = useState<Array<{ tenant_key: string; sheet_configured: boolean }>>([]);
  const [tenant, setTenant] = useState('');
  const [appScript, setAppScript] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [dryRun, setDryRun] = useState(true);

  const load = useCallback(async (key: string) => {
    if (!key) return;
    setLoading(true);
    setError('');
    try {
      setOv(await fetchSyncOverview(key));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao carregar a sincronização.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchTenants()
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
  const allSessions = (ov?.portal_sessions || {}) as Record<string, Record<string, { state?: string; last_error?: string | null }>>;
  const sessions = allSessions[tenant] || (ov?.tenant_key ? allSessions[ov.tenant_key] : undefined) || Object.values(allSessions)[0] || {};
  const pending = proposals.filter((p) => p.status === 'PENDING' || p.status === 'BLOCKED');
  const uncertain = proposals.filter((p) => p.status === 'UNCERTAIN');
  const policy = ready?.automation_policy || ready?.default_sync_mode || 'REVIEW';
  const caps = ready?.write_capabilities || [];
  const pollerRuntime = fmtState(ready?.sheets_poller_runtime);

  const act = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setError('');
    setNotice('');
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha na operação.');
    } finally {
      setBusy('');
    }
  };

  const sync = () =>
    act('sync', async () => {
      const out = await triggerSheetSync({ tenant_key: tenant, dry_run: dryRun });
      setNotice(`${dryRun ? 'Simulação' : 'Sincronização'}: ${out.summary || 'concluída'}`);
      await load(tenant);
    });

  const copyScript = async () => {
    if (!appScript) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(appScript);
        setNotice('Code.gs copiado para a área de transferência.');
      }
    } catch {
      setError('Não foi possível copiar automaticamente.');
    }
  };

  const tabs: Array<[Tab, string]> = [
    ['overview', 'Visão geral'],
    ['changes', `Fila de mudanças (${proposals.length})`],
    ['audit', 'Auditoria'],
    ['sheet', 'Planilha'],
  ];

  return (
    <main className="ifood-sync-root">
      <header className="ifood-header">
        <div className="ifood-header-copy">
          <div className="ifood-eyebrow">Hermes · operação de lojas</div>
          <h1>Loja iFood / Sheets</h1>
          <p className="ifood-subtitle">
            A planilha é o comando. A divergência é aplicada e registrada com autor — o Telegram avisa, não autoriza.
          </p>
        </div>
        <div className="ifood-header-actions">
          {tenants.length > 1 ? (
            <select
              className="ifood-select"
              aria-label="Selecionar padaria"
              value={tenant}
              onChange={(e) => {
                setTenant(e.target.value);
                setOv(null);
                void load(e.target.value);
              }}
            >
              {tenants.map((t) => (
                <option key={t.tenant_key} value={t.tenant_key}>
                  Padaria: {t.tenant_key}
                </option>
              ))}
            </select>
          ) : (
            <span className="ifood-control-chip">Padaria: {tenant || tenants[0]?.tenant_key || 'default'}</span>
          )}
          <span className="ifood-policy-chip">
            {policy === 'AUTO_APPLY' ? 'AUTO_APPLY · a planilha aplica' : 'REVIEW · acesso restrito ao operador'}
          </span>
          <button
            type="button"
            className="ifood-btn"
            disabled={loading}
            onClick={() => void load(tenant)}
          >
            Atualizar
          </button>
        </div>
      </header>

      <nav className="ifood-tabs" aria-label="Seções do painel">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-current={tab === id ? 'page' : undefined}
            className={tab === id ? 'ifood-tab is-active' : 'ifood-tab'}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      <div className="ifood-body">
        {loading ? <p className="ifood-banner is-info">Carregando painel de sincronização…</p> : null}
        {error ? (
          <p role="alert" className="ifood-banner is-error">
            {error}
          </p>
        ) : null}
        {notice ? (
          <p role="status" className="ifood-banner is-success">
            {notice}
          </p>
        ) : null}

        <section className="ifood-summary-grid" aria-label="Resumo operacional">
          <div className="ifood-summary-card">
            <span className="ifood-summary-label">Aguardando revisão</span>
            <strong className="ifood-summary-value">{pending.length}</strong>
            <small className="ifood-summary-hint">bloqueadas por guardrail ou capacidade</small>
          </div>
          <div className="ifood-summary-card">
            <span className="ifood-summary-label">Resultado incerto</span>
            <strong className="ifood-summary-value">{uncertain.length}</strong>
            <small className="ifood-summary-hint">exigem reconciliação</small>
          </div>
          <div className="ifood-summary-card">
            <span className="ifood-summary-label">Lojas</span>
            <strong className="ifood-summary-value">{tenants.length || 1}</strong>
            <small className="ifood-summary-hint">isoladas por tenant</small>
          </div>
          <div className="ifood-summary-card">
            <span className="ifood-summary-label">Auditoria</span>
            <strong className="ifood-summary-value">{events.length}</strong>
            <small className="ifood-summary-hint">eventos mais recentes</small>
          </div>
        </section>

        {tab === 'overview' && ready ? (
          <section className="ifood-overview-grid">
            <div className="ifood-section-card">
              <h2 className="ifood-section-title">Prontidão das integrações</h2>
              <div className="ifood-kv-list">
                <KvRow
                  label="Plugin habilitado"
                  value={ready.enabled ? 'configurado' : 'pendente'}
                  tone={ready.enabled ? 'is-ok' : 'is-warn'}
                />
                <KvRow
                  label="Google Sheets API"
                  value={ready.sheets_api_configured ? 'configurado' : 'pendente'}
                  tone={ready.sheets_api_configured ? 'is-ok' : 'is-warn'}
                />
                <KvRow
                  label="Planilha da loja"
                  value={ready.sheets_spreadsheet_id ? 'informada' : 'não informada'}
                  tone={ready.sheets_spreadsheet_id ? 'is-ok' : 'is-warn'}
                />
                <KvRow
                  label="Webhook HMAC"
                  value={ready.webhook_configured ? 'configurado' : 'pendente'}
                  tone={ready.webhook_configured ? 'is-ok' : 'is-warn'}
                />
                <KvRow
                  label="Poller Google Sheets"
                  value={
                    ready.sheets_poller_enabled
                      ? `ativo (${pollerRuntime})`
                      : `desligado (${pollerRuntime})`
                  }
                  tone={ready.sheets_poller_enabled ? 'is-ok' : 'is-muted'}
                />
                <KvRow
                  label="iFood API"
                  value={ready.ifood_api_configured ? 'configurado' : 'pendente'}
                  tone={ready.ifood_api_configured ? 'is-ok' : 'is-warn'}
                />
                <KvRow
                  label="Playwright (portais)"
                  value={ready.playwright_configured ? 'configurado' : 'pendente'}
                  tone={ready.playwright_configured ? 'is-ok' : 'is-warn'}
                />
              </div>
            </div>

            <div className="ifood-section-card">
              <h2 className="ifood-section-title">Política de escrita</h2>
              <div className="ifood-kv-list">
                <KvRow label="Modo de sincronização" value={policy} />
                <KvRow label="Backend preferido" value={ready.default_execution_mode || '—'} />
                <KvRow
                  label="Capacidades liberadas"
                  value={caps.length ? caps.join(', ') : 'todas bloqueadas'}
                  tone={caps.length ? 'is-ok' : 'is-warn'}
                />
                <KvRow
                  label="Limite de variação de preço"
                  value={ready.max_price_change_pct != null ? `${ready.max_price_change_pct}%` : '—'}
                />
              </div>
            </div>

            <div className="ifood-section-card">
              <h2 className="ifood-section-title">Sessões dos portais</h2>
              <div className="ifood-kv-list">
                {(['IFOOD', '99FOOD', 'ANOTA_AI'] as const).map((ch) => {
                  const s = sessions[ch] || (ch === '99FOOD' ? sessions['NINETY_NINE'] : undefined) || {};
                  const st = s.state || 'não configurado';
                  const isOk = ['OK', 'CONFIGURED'].includes(String(st).toUpperCase());
                  return (
                    <KvRow
                      key={ch}
                      label={CHANNEL[ch] || ch}
                      value={`${st}${s.last_error ? ` · ${s.last_error}` : ''}`}
                      tone={isOk ? 'is-ok' : 'is-warn'}
                    />
                  );
                })}
              </div>
            </div>

            <div className="ifood-section-card">
              <h2 className="ifood-section-title">Último lote</h2>
              <div className="ifood-kv-list">
                <KvRow label="Lotes registrados" value={String(snaps.length)} />
                <KvRow
                  label="Poller"
                  value={ov?.sheets_poller?.last_summary || pollerRuntime}
                  tone={ov?.sheets_poller?.last_error ? 'is-warn' : 'is-muted'}
                />
                <KvRow label="Eventos de auditoria" value={String(events.length)} />
              </div>
            </div>
          </section>
        ) : null}

        {tab === 'changes' ? (
          <section className="ifood-stack">
            {proposals.length === 0 ? (
              <div className="ifood-section-card">
                <p className="ifood-empty">
                  Nenhuma mudança registrada. Editar a planilha cria a mudança; ela aparece aqui aplicada ou com o motivo do bloqueio.
                </p>
              </div>
            ) : (
              proposals.map((p) => (
                <article className="ifood-section-card ifood-proposal-card" key={p.proposal_id}>
                  <div className="ifood-proposal-head">
                    <span className={badge(p.status)}>{STATUS[p.status] || p.status}</span>
                    <span className="ifood-proposal-title">
                      {DOMAIN[p.domain] || p.domain} · {p.store_key}
                    </span>
                    <span className="ifood-meta">
                      {CHANNEL[p.channel] || p.channel} · autor {p.actor_id} · {when(p.created_at)} · backend {p.backend_planned}
                    </span>
                  </div>

                  <div className="ifood-diff-lines">
                    {changeLines(p).map((line, idx) => (
                      <div key={idx}>{line}</div>
                    ))}
                  </div>

                  {(p.guardrail_warnings || []).length ? (
                    <div className="ifood-guardrail-box">Avisos: {p.guardrail_warnings.join(' | ')}</div>
                  ) : null}

                  <div className="ifood-row-actions">
                    {p.status === 'UNCERTAIN' ? (
                      <button
                        type="button"
                        className="ifood-btn is-primary"
                        disabled={busy === `rec-${p.proposal_id}`}
                        onClick={() =>
                          void act(`rec-${p.proposal_id}`, async () => {
                            const r = await reconcileSyncProposal(p.proposal_id, tenant);
                            setNotice(`Reconciliação: ${r.outcome}`);
                            await load(tenant);
                          })
                        }
                      >
                        Reconciliar
                      </button>
                    ) : null}
                    {p.status === 'PENDING' || p.status === 'BLOCKED' ? (
                      <button
                        type="button"
                        className="ifood-btn is-danger"
                        disabled={busy === `rj-${p.proposal_id}`}
                        onClick={() =>
                          void act(`rj-${p.proposal_id}`, async () => {
                            await rejectSyncProposal(p.proposal_id, 'descartada pelo operador', tenant);
                            setNotice('Mudança descartada.');
                            await load(tenant);
                          })
                        }
                      >
                        Descartar
                      </button>
                    ) : null}
                    <span className="ifood-id-chip">{p.proposal_id}</span>
                  </div>
                </article>
              ))
            )}
          </section>
        ) : null}

        {tab === 'audit' ? (
          <section className="ifood-section-card">
            <h2 className="ifood-section-title">Eventos de auditoria</h2>
            {events.length === 0 ? (
              <p className="ifood-empty">Nenhum evento de auditoria registrado.</p>
            ) : (
              <div className="ifood-table-wrap">
                <table className="ifood-table">
                  <thead>
                    <tr>
                      <th>Quando</th>
                      <th>Loja</th>
                      <th>Domínio</th>
                      <th>Backend</th>
                      <th>Resultado</th>
                      <th>Resumo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((ev) => (
                      <tr key={ev.id}>
                        <td className="ifood-tabular">{when(ev.timestamp_utc)}</td>
                        <td>{ev.store_key}</td>
                        <td>{DOMAIN[ev.domain] || ev.domain}</td>
                        <td>{ev.backend_used}</td>
                        <td>
                          <span className={badge(ev.outcome)}>{ev.outcome}</span>
                        </td>
                        <td>{ev.summary}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        ) : null}

        {tab === 'sheet' ? (
          <section className="ifood-stack">
            <div className="ifood-section-card">
              <h2 className="ifood-section-title">Sincronizar a planilha</h2>
              <p className="ifood-muted">
                Lê a planilha (fonte da verdade), compara com o estado da loja e aplica quando a política é AUTO_APPLY e a capacidade está liberada. Sem capacidade, fica registrado com o motivo.
              </p>
              <div className="ifood-row-actions">
                <button
                  type="button"
                  className="ifood-btn"
                  disabled={busy === 'sync'}
                  onClick={() => setDryRun((v) => !v)}
                >
                  Modo: {dryRun ? 'simulação' : 'aplicar'}
                </button>
                <button
                  type="button"
                  className="ifood-btn is-primary"
                  disabled={busy === 'sync'}
                  onClick={() => void sync()}
                >
                  {busy === 'sync' ? 'Sincronizando…' : 'Sincronizar agora'}
                </button>
                <button
                  type="button"
                  className="ifood-btn"
                  disabled={busy === 'gs'}
                  onClick={() =>
                    void act('gs', async () => {
                      const r = await fetchAppsScript();
                      setAppScript(r.content || '');
                    })
                  }
                >
                  {appScript ? 'Recarregar Apps Script' : 'Carregar Apps Script'}
                </button>
              </div>
            </div>

            <div className="ifood-section-card">
              <div className="ifood-section-head">
                <h2 className="ifood-section-title">Apps Script (webhook assinado)</h2>
                {appScript ? (
                  <button type="button" className="ifood-btn" onClick={() => void copyScript()}>
                    Copiar
                  </button>
                ) : null}
              </div>
              <pre className="ifood-pre">{appScript || 'Clique em "Carregar Apps Script" para visualizar apps_script/Code.gs'}</pre>
            </div>
          </section>
        ) : null}

        <footer className="ifood-footer">
          A planilha é a fonte da verdade. Mudanças sem capacidade liberada ou bloqueadas por guardrail ficam registradas na fila para auditoria.
        </footer>
      </div>
    </main>
  );
}
