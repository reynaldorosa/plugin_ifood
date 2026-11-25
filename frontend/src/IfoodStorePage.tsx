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

const DOMAIN: Record<string, string> = {
  OPENING_HOURS: 'Horários',
  CATALOG: 'Cardápio',
  STORE_STATUS: 'Status da loja',
  PREP_TIME: 'Tempo de preparo',
};
const CHANNEL: Record<string, string> = { IFOOD: 'iFood', '99FOOD': '99Food', ANOTA_AI: 'Anota AI' };
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
const WEEKDAY = ['dom', 'seg', 'ter', 'qua', 'qui', 'sex', 'sáb'];

function badge(value: string): string {
  const v = String(value || '').toUpperCase();
  if (['CONFIRMED', 'APPLIED', 'EXECUTED', 'OK'].includes(v)) return 'ifood-badge is-ok';
  if (['BLOCKED', 'FAILED', 'DIVERGENT', 'ERROR'].includes(v) || v.includes('CAPTCHA')) return 'ifood-badge is-danger';
  if (v.includes('PENDING') || v.includes('LOGIN') || v === 'REVIEW' || v === 'UNCERTAIN') return 'ifood-badge is-warn';
  return 'ifood-badge is-neutral';
}

function when(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function changeLines(p: SyncProposalRecord): string[] {
  const cur = p.current as any;
  const pro = p.proposed as any;
  if (p.domain === 'OPENING_HOURS') {
    const fmt = (rows: any[]) => (rows || []).map((s: any) => `${WEEKDAY[Number(s.dayOfWeek)] ?? s.dayOfWeek} ${s.start}+${s.duration}min`).join(' · ') || 'sem turnos';
    return [`antes: ${fmt(cur)}`, `depois: ${fmt(pro)}`];
  }
  if (p.domain === 'CATALOG') {
    const before = new Map<string, any>((cur || []).map((i: any) => [String(i.item_id), i]));
    const out: string[] = [];
    for (const item of pro || []) {
      const old = before.get(String(item.item_id));
      const bits: string[] = [];
      if (!old) bits.push('novo no cardápio');
      if (old && old.price_cents !== item.price_cents) bits.push(`R$ ${(old.price_cents / 100).toFixed(2)} → R$ ${(item.price_cents / 100).toFixed(2)}`);
      if (old && old.available !== item.available) bits.push(item.available ? 'volta ao cardápio' : 'sai do cardápio');
      if (bits.length) out.push(`${item.item_name || item.item_id}: ${bits.join(' · ')}`);
      before.delete(String(item.item_id));
    }
    for (const rest of before.values()) out.push(`${rest.item_name || rest.item_id}: fora da proposta`);
    return out.length ? out : ['sem diferença em relação ao estado atual'];
  }
  if (p.domain === 'STORE_STATUS') {
    const acts: Record<string, string> = { PAUSE: 'pausar a loja', RESUME: 'reabrir a loja', OPEN: 'abrir a loja', CLOSE: 'fechar a loja' };
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
  const sessions = ((ov?.portal_sessions || {}) as any)[tenant] || {};
  const pending = proposals.filter((p) => p.status === 'PENDING' || p.status === 'BLOCKED');
  const applied = proposals.filter((p) => ['EXECUTED', 'APPLIED', 'CONFIRMED'].includes(p.status));
  const uncertain = proposals.filter((p) => p.status === 'UNCERTAIN');
  const policy = ready?.automation_policy || ready?.default_sync_mode || 'REVIEW';
  const caps = ready?.write_capabilities || [];

  const act = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setError('');
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

  return (
    <main className="ifood-sync-root">
      <header className="ifood-header">
        <div>
          <span className="ifood-eyebrow">HERMES / OPERAÇÃO DE LOJAS</span>
          <h1>Loja iFood / Sheets</h1>
          <p className="ifood-subtitle">
            A planilha é o comando. A divergência é aplicada e registrada com autor — o Telegram avisa,
            não autoriza.
          </p>
        </div>
        <div className="ifood-header-actions">
          {tenants.length > 1 ? (
            <select aria-label="Selecionar loja" value={tenant} onChange={(e) => { setTenant(e.target.value); setOv(null); void load(e.target.value); }}>
              {tenants.map((t) => <option key={t.tenant_key} value={t.tenant_key}>{t.tenant_key}</option>)}
            </select>
          ) : (
            <span className="ifood-pill is-neutral">{tenant || 'default'}</span>
          )}
          <span className={policy === 'AUTO_APPLY' ? 'ifood-badge is-ok' : 'ifood-badge is-warn'}>
            {policy === 'AUTO_APPLY' ? 'AUTO_APPLY · a planilha aplica' : 'REVIEW · revisão ativa'}
          </span>
          <button type="button" className="ifood-btn-secondary" disabled={loading} onClick={() => void load(tenant)}>Atualizar</button>
        </div>
      </header>

      <section className="ifood-summary-grid">
        <div className="ifood-summary-card"><span>Aguardando revisão</span><strong>{pending.length}</strong><small>bloqueadas por guardrail ou capacidade</small></div>
        <div className="ifood-summary-card"><span>Aplicadas</span><strong>{applied.length}</strong><small>mudanças que chegaram ao canal</small></div>
        <div className="ifood-summary-card"><span>Resultado incerto</span><strong>{uncertain.length}</strong><small>exigem reconciliação</small></div>
        <div className="ifood-summary-card"><span>Lojas monitoradas</span><strong>{tenants.length || 1}</strong><small>{snaps.length} estados registrados</small></div>
      </section>

      {loading ? <p className="ifood-banner is-info">Carregando painel de sincronização…</p> : null}
      {error ? <p role="alert" className="ifood-banner is-error">{error}</p> : null}
      {notice ? <p role="status" className="ifood-banner is-success">{notice}</p> : null}

      <nav className="ifood-tabs">
        {([
          ['overview', 'Visão geral'],
          ['changes', `Mudanças (${pending.length + applied.length})`],
          ['audit', 'Auditoria'],
          ['sheet', 'Planilha'],
        ] as Array<[Tab, string]>).map(([id, label]) => (
          <button key={id} type="button" className={tab === id ? 'ifood-tab is-active' : 'ifood-tab'} onClick={() => setTab(id)}>{label}</button>
        ))}
      </nav>

      {tab === 'overview' && ready ? (
        <section className="ifood-stack">
          <div className="ifood-card">
            <h2>Prontidão das integrações</h2>
            <ul className="ifood-schema-list">
              <li>Plugin: <strong>{ready.enabled ? 'habilitado' : 'desligado'}</strong></li>
              <li>Google Sheets API: <strong>{ready.sheets_api_configured ? 'configurado' : 'pendente'}</strong></li>
              <li>Planilha da loja: <strong>{ready.sheets_spreadsheet_id ? 'informada' : 'não informada'}</strong></li>
              <li>Webhook HMAC: <strong>{ready.webhook_configured ? 'configurado' : 'pendente'}</strong></li>
              <li>Poller: <strong>{ready.sheets_poller_enabled ? 'ativo' : `desligado (${(ready.sheets_poller_runtime as any)?.state || '—'})`}</strong></li>
              <li>iFood API: <strong>{ready.ifood_api_configured ? 'configurado' : 'pendente'}</strong></li>
              <li>Playwright (portais): <strong>{ready.playwright_configured ? 'configurado' : 'pendente'}</strong></li>
            </ul>
          </div>
          <div className="ifood-card">
            <h2>Política de escrita</h2>
            <ul className="ifood-schema-list">
              <li>Modo: <strong>{policy}</strong></li>
              <li>Backend preferido: <strong>{ready.default_execution_mode}</strong></li>
              <li>Capacidades liberadas: <strong>{caps.length ? caps.join(', ') : 'todas bloqueadas'}</strong></li>
              <li>Limite de variação de preço: <strong>{ready.max_price_change_pct}%</strong></li>
            </ul>
          </div>
          <div className="ifood-card">
            <h2>Sessões dos portais</h2>
            <div className="ifood-readiness-bar">
              {['IFOOD', '99FOOD', 'ANOTA_AI'].map((ch) => {
                const s = sessions[ch] || {};
                const st = s.state || 'não configurado';
                return <div key={ch} className={badge(st)}>{CHANNEL[ch]}: {st}{s.last_error ? ` · ${s.last_error}` : ''}</div>;
              })}
            </div>
          </div>
        </section>
      ) : null}

      {tab === 'changes' ? (
        <section className="ifood-stack">
          {proposals.length === 0 ? (
            <div className="ifood-card"><p className="ifood-muted">Nenhuma mudança registrada. Editar a planilha cria a mudança; ela aparece aqui aplicada ou com o motivo do bloqueio.</p></div>
          ) : (
            proposals.map((p) => (
              <article className="ifood-card" key={p.proposal_id}>
                <div className="ifood-card-top">
                  <div>
                    <h3>{DOMAIN[p.domain] || p.domain} · {p.store_key}</h3>
                    <span className="ifood-meta">{CHANNEL[p.channel] || p.channel} · autor <strong>{p.actor_id}</strong> · {when(p.created_at)} · backend {p.backend_planned}</span>
                  </div>
                  <span className={badge(p.status)}>{STATUS[p.status] || p.status}</span>
                </div>
                <ul className="ifood-schema-list">{changeLines(p).map((l, i) => <li key={i}>{l}</li>)}</ul>
                {(p.guardrail_warnings || []).length ? (
                  <div className="ifood-guardrail-box">Avisos: {p.guardrail_warnings.join(' | ')}</div>
                ) : null}
                <div className="ifood-row-actions">
                  {p.status === 'UNCERTAIN' ? (
                    <button type="button" className="ifood-btn-secondary" disabled={busy === `rec-${p.proposal_id}`}
                      onClick={() => void act(`rec-${p.proposal_id}`, async () => { const r = await reconcileSyncProposal(p.proposal_id, tenant); setNotice(`Reconciliação: ${r.outcome}`); await load(tenant); })}>
                      Reconciliar
                    </button>
                  ) : null}
                  {p.status === 'PENDING' || p.status === 'BLOCKED' ? (
                    <button type="button" className="ifood-btn-danger" disabled={busy === `rj-${p.proposal_id}`}
                      onClick={() => void act(`rj-${p.proposal_id}`, async () => { await rejectSyncProposal(p.proposal_id, 'descartada pelo operador', tenant); setNotice('Mudança descartada.'); await load(tenant); })}>
                      Descartar
                    </button>
                  ) : null}
                  <span className="ifood-meta">{p.proposal_id}</span>
                </div>
              </article>
            ))
          )}
        </section>
      ) : null}

      {tab === 'audit' ? (
        <section className="ifood-card">
          {events.length === 0 ? <p className="ifood-muted">Nenhum evento de auditoria registrado.</p> : (
            <div className="ifood-table-wrap">
              <table className="ifood-table">
                <thead><tr><th>Quando</th><th>Loja</th><th>Domínio</th><th>Backend</th><th>Resultado</th><th>Resumo</th></tr></thead>
                <tbody>
                  {events.map((ev) => (
                    <tr key={ev.id}>
                      <td>{when(ev.timestamp_utc)}</td><td>{ev.store_key}</td><td>{DOMAIN[ev.domain] || ev.domain}</td>
                      <td>{ev.backend_used}</td><td><span className={badge(ev.outcome)}>{ev.outcome}</span></td><td>{ev.summary}</td>
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
          <div className="ifood-card">
            <h2>Sincronizar a planilha</h2>
            <p className="ifood-muted">
              Lê a planilha (fonte da verdade), compara com o estado da loja e aplica quando a política é
              AUTO_APPLY e a capacidade está liberada. Sem capacidade, fica registrado com o motivo.
            </p>
            <div className="ifood-row-actions">
              <button type="button" className="ifood-btn-secondary" onClick={() => setDryRun((v) => !v)}>Modo: {dryRun ? 'simulação' : 'aplicar'}</button>
              <button type="button" className="ifood-btn-primary" disabled={busy === 'sync'} onClick={() => void sync()}>{busy === 'sync' ? 'Sincronizando…' : 'Sincronizar agora'}</button>
            </div>
          </div>
          <div className="ifood-card">
            <h2>Apps Script (webhook assinado)</h2>
            <pre className="ifood-pre">{appScript || 'Clique em atualizar para carregar apps_script/Code.gs'}</pre>
            <div className="ifood-row-actions">
              <button type="button" className="ifood-btn-secondary" disabled={busy === 'gs'}
                onClick={() => void act('gs', async () => { const r = await fetchAppsScript(); setAppScript(r.content || ''); })}>
                Carregar Apps Script
              </button>
            </div>
          </div>
        </section>
      ) : null}
    </main>
  );
}
